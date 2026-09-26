"""Conexiones con redes sociales y publicación de clips.

Cada red puede conectarse de dos formas:
- Conexión rápida (Upload-Post): inicias sesión en la red y listo, sin crear apps de desarrollador.
- Conexión directa (avanzado): OAuth con tu propia app de desarrollador de cada red.
Al publicar se usa la directa si existe; si no, la rápida.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .. import config
from ..util import format_exception
from . import instagram, oauth, tiktok, uploadpost, youtube
from .oauth import SocialError

PLATFORMS = {"youtube": youtube, "tiktok": tiktok, "instagram": instagram}
TARGETS = ("youtube", "tiktok", "instagram")


def status() -> dict:
    quick = uploadpost.accounts()
    out = {}
    for name, module in PLATFORMS.items():
        token = oauth.get_token(name)
        via = "direct" if token else ("quick" if name in quick else "")
        account = (token or {}).get("account", {}) if token else quick.get(name, {})
        out[name] = {
            "label": module.LABEL,
            "configured": module.configured(),
            "connected": bool(via),
            "via": via,
            "direct": bool(token),
            "quick": name in quick,
            "reauth": bool(quick.get(name, {}).get("reauth")) and not token,
            "account": account,
            "expires_at": (token or {}).get("refresh_expires_at") or ((token or {}).get("expires_at") if name == "instagram" else None),
            "setup": module.setup(),
        }
    out["quick"] = {"configured": uploadpost.configured(), "profile": config.env("UPLOAD_POST_USER"), "signup": uploadpost.SIGNUP_URL}
    return out


def route(platform: str) -> str:
    """'direct', 'quick' o '' según cómo se puede publicar en esa red ahora mismo."""
    if oauth.get_token(platform):
        return "direct"
    if platform in uploadpost.accounts(max_age=5):
        return "quick"
    return ""


def start(platform: str) -> str:
    module = PLATFORMS.get(platform)
    if not module:
        raise SocialError("Red social desconocida.")
    if not module.configured():
        raise SocialError(f"Falta configurar la app de {module.LABEL}.", "Rellena el ID y el secreto en Ajustes → Redes → Avanzado.")
    verifier, challenge = oauth.pkce_pair(hex_challenge=getattr(module, "PKCE_HEX", False))
    state = oauth.new_state(platform, verifier, module.redirect_uri())
    return module.auth_url(state, challenge)


def complete(params: dict) -> str:
    """Termina el OAuth directo con los parámetros que devuelve la red social. Devuelve la plataforma."""
    state = params.get("state", "")
    entry = oauth.pop_state(state)
    platform = entry["platform"]
    module = PLATFORMS[platform]
    if params.get("error"):
        description = params.get("error_description") or params.get("error_reason") or params["error"]
        raise SocialError(f"{module.LABEL}: autorización cancelada.", str(description)[:300])
    code = params.get("code", "")
    if not code:
        raise SocialError(f"{module.LABEL} no devolvió el código de autorización.", "Vuelve a intentarlo.")
    token = module.exchange(code, entry["verifier"], entry["redirect_uri"])
    token["connected_at"] = time.time()
    oauth.set_token(platform, token)
    return platform


def disconnect(platform: str) -> None:
    oauth.delete_token(platform)


def valid_token(platform: str) -> dict:
    module = PLATFORMS[platform]
    token = oauth.get_token(platform)
    if not token:
        raise SocialError(f"{module.LABEL} no está conectado.", "Conéctalo en Ajustes → Redes.")
    stale = module.needs_refresh(token) if hasattr(module, "needs_refresh") else token.get("expires_at", 0) < time.time() + 120
    if stale:
        token = module.refresh(token)
        oauth.set_token(platform, token)
    return token


class Publisher:
    """Publica clips en segundo plano y guarda el estado en el propio clip."""

    def __init__(self, manager):
        self.manager = manager
        self.lock = threading.Lock()

    def publish(self, job_id: str, number: int, targets: list[str], options: dict) -> None:
        job = self.manager.get(job_id)
        if not job:
            raise SocialError("Trabajo no encontrado.")
        clip = next((c for c in job["clips"] if c["number"] == number), None)
        if not clip or not clip.get("file"):
            raise SocialError("El clip aún no está listo.")
        if clip.get("status") == "rendering":
            raise SocialError("Espera a que termine de renderizarse.")
        if "uploadpost" in targets:  # compatibilidad: «todas las de la conexión rápida»
            targets = [t for t in targets if t != "uploadpost"] + list(uploadpost.accounts(max_age=0))
        targets = [t for t in dict.fromkeys(targets) if t in TARGETS]
        if not targets:
            raise SocialError("Elige al menos una red.")
        routes = {}
        for target in targets:
            routes[target] = route(target)
            if not routes[target]:
                raise SocialError(f"{PLATFORMS[target].LABEL} no está conectado.", "Conéctalo en Ajustes → Redes.")
            current = (clip.get("publications") or {}).get(target, {})
            if current.get("status") in ("uploading", "processing"):
                raise SocialError(f"Ya se está enviando a {PLATFORMS[target].LABEL}.")
        settings = {**config.get_settings(), **options}
        for target in targets:
            self._record(job_id, number, target, status="uploading", progress=0.0, message="Preparando…", url="", id="", error="",
                         via=routes[target])
        for target in [t for t in targets if routes[t] == "direct"]:
            threading.Thread(target=self._run_direct, args=(job_id, number, target, settings), daemon=True).start()
        quick = [t for t in targets if routes[t] == "quick"]
        if quick:
            threading.Thread(target=self._run_quick, args=(job_id, number, quick), daemon=True).start()

    def _record(self, job_id: str, number: int, target: str, **values) -> None:
        with self.lock:
            job = self.manager.get(job_id)
            clip = next(c for c in job["clips"] if c["number"] == number)
            publications = dict(clip.get("publications") or {})
            publications[target] = {**publications.get(target, {}), **values, "at": time.time()}
            self.manager.update_clip(job_id, number, publications=publications)

    def _clip(self, job_id: str, number: int) -> tuple[Path, dict]:
        job = self.manager.get(job_id)
        clip = next(c for c in job["clips"] if c["number"] == number)
        return self.manager.outputs / job_id / clip["file"], clip

    def _run_direct(self, job_id: str, number: int, target: str, settings: dict) -> None:
        video, clip = self._clip(job_id, number)

        def progress(fraction: float, message: str) -> None:
            self._record(job_id, number, target, progress=round(fraction, 3), message=message)

        try:
            token = valid_token(target)
            try:
                result = PLATFORMS[target].publish(token, video, clip, settings, progress)
            except SocialError as exc:
                if exc.status != 401 and exc.code not in ("auth", "access_token_invalid", "190"):
                    raise
                token = PLATFORMS[target].refresh(token)
                oauth.set_token(target, token)
                result = PLATFORMS[target].publish(token, video, clip, settings, progress)
            self._record(job_id, number, target, progress=1.0, error="", **result)
        except Exception as exc:  # noqa: BLE001
            self._fail(job_id, number, [target], exc)

    def _run_quick(self, job_id: str, number: int, targets: list[str]) -> None:
        video, clip = self._clip(job_id, number)

        def progress(fraction: float, message: str) -> None:
            for target in targets:
                self._record(job_id, number, target, progress=round(fraction, 3), message=message)

        try:
            result = uploadpost.publish(video, clip, job_id, progress, targets)
            for target in targets:
                self._record(job_id, number, target, progress=1.0, error="", **result)
        except Exception as exc:  # noqa: BLE001
            self._fail(job_id, number, targets, exc)

    def _fail(self, job_id: str, number: int, targets: list[str], exc: BaseException) -> None:
        self._log(self.manager.outputs / job_id, ",".join(targets), exc)
        if isinstance(exc, SocialError):
            error, hint = exc.message, exc.hint
        else:
            error, hint = f"Error inesperado: {exc}"[:300], "Revisa job.log"
        for target in targets:
            self._record(job_id, number, target, status="error", error=error, message=hint, progress=0.0)

    def refresh_status(self, job_id: str, number: int, target: str) -> None:
        job = self.manager.get(job_id)
        clip = next((c for c in job["clips"] if c["number"] == number), None) if job else None
        record = ((clip or {}).get("publications") or {}).get(target)
        if not record or record.get("status") != "processing" or not record.get("id"):
            return
        try:
            if record.get("via") == "quick":
                updated = uploadpost.check(record, target)
            elif target == "tiktok":
                updated = {**record, **tiktok.wait(valid_token("tiktok"), record["id"], settings_mode(), lambda *_: None, timeout=1)}
            else:
                return
            self._record(job_id, number, target, **{k: v for k, v in updated.items() if k != "at"})
        except SocialError as exc:
            self._record(job_id, number, target, message=exc.message)

    @staticmethod
    def _log(folder: Path, target: str, exc: BaseException) -> None:
        try:
            with (folder / "job.log").open("a", encoding="utf-8") as handle:
                detail = getattr(exc, "detail", "")
                handle.write(f"[publicación {target}] {exc}\n{detail}\n{format_exception(exc)}\n")
        except OSError:
            pass


def settings_mode() -> str:
    return config.get_settings().get("tiktok_mode", "draft")
