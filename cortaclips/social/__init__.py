"""Conexiones OAuth con redes sociales y publicación de clips."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .. import config
from ..util import format_exception
from . import instagram, oauth, tiktok, uploadpost, youtube
from .oauth import SocialError

PLATFORMS = {"youtube": youtube, "tiktok": tiktok, "instagram": instagram}
TARGETS = ("youtube", "tiktok", "instagram", "uploadpost")


def status() -> dict:
    out = {}
    for name, module in PLATFORMS.items():
        token = oauth.get_token(name)
        out[name] = {
            "label": module.LABEL,
            "configured": module.configured(),
            "connected": bool(token),
            "account": (token or {}).get("account", {}),
            "expires_at": (token or {}).get("refresh_expires_at") or ((token or {}).get("expires_at") if name == "instagram" else None),
            "setup": module.setup(),
        }
    out["uploadpost"] = {"label": uploadpost.LABEL, "configured": uploadpost.configured(), "connected": uploadpost.configured(), "account": {"name": config.env("UPLOAD_POST_USER")}}
    return out


def start(platform: str) -> str:
    module = PLATFORMS.get(platform)
    if not module:
        raise SocialError("Red social desconocida.")
    if not module.configured():
        raise SocialError(f"Falta configurar la app de {module.LABEL}.", "Rellena el ID y el secreto en Ajustes → Redes.")
    verifier, challenge = oauth.pkce_pair(hex_challenge=getattr(module, "PKCE_HEX", False))
    state = oauth.new_state(platform, verifier, module.redirect_uri())
    return module.auth_url(state, challenge)


def complete(params: dict) -> str:
    """Termina el OAuth con los parámetros que devuelve la red social. Devuelve la plataforma."""
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
        targets = [t for t in dict.fromkeys(targets) if t in TARGETS]
        if not targets:
            raise SocialError("Elige al menos una red.")
        for target in targets:
            if target == "uploadpost":
                if not uploadpost.configured():
                    raise SocialError("Upload-Post no está configurado.", "Añade UPLOAD_POST_API_KEY y UPLOAD_POST_USER en Ajustes → Redes.")
            elif not oauth.get_token(target):
                raise SocialError(f"{PLATFORMS[target].LABEL} no está conectado.", "Conéctalo en Ajustes → Redes.")
            current = (clip.get("publications") or {}).get(target, {})
            if current.get("status") in ("uploading", "processing"):
                raise SocialError(f"Ya se está enviando a {target}.")
        settings = {**config.get_settings(), **options}
        for target in targets:
            self._record(job_id, number, target, status="uploading", progress=0.0, message="Preparando…", url="", id="", error="")
            threading.Thread(target=self._run, args=(job_id, number, target, settings), daemon=True).start()

    def _record(self, job_id: str, number: int, target: str, **values) -> None:
        with self.lock:
            job = self.manager.get(job_id)
            clip = next(c for c in job["clips"] if c["number"] == number)
            publications = dict(clip.get("publications") or {})
            publications[target] = {**publications.get(target, {}), **values, "at": time.time()}
            self.manager.update_clip(job_id, number, publications=publications)

    def _run(self, job_id: str, number: int, target: str, settings: dict) -> None:
        folder = self.manager.outputs / job_id
        job = self.manager.get(job_id)
        clip = next(c for c in job["clips"] if c["number"] == number)
        video = folder / clip["file"]

        def progress(fraction: float, message: str) -> None:
            self._record(job_id, number, target, progress=round(fraction, 3), message=message)

        try:
            if target == "uploadpost":
                result = uploadpost.publish(video, clip, job_id, progress)
            else:
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
        except SocialError as exc:
            self._log(folder, target, exc)
            self._record(job_id, number, target, status="error", error=exc.message, message=exc.hint, progress=0.0)
        except Exception as exc:  # noqa: BLE001
            self._log(folder, target, exc)
            self._record(job_id, number, target, status="error", error=f"Error inesperado: {exc}"[:300], message="Revisa job.log", progress=0.0)

    def refresh_status(self, job_id: str, number: int, target: str) -> None:
        job = self.manager.get(job_id)
        clip = next((c for c in job["clips"] if c["number"] == number), None) if job else None
        record = ((clip or {}).get("publications") or {}).get(target)
        if not record or record.get("status") != "processing" or not record.get("id"):
            return
        try:
            if target == "uploadpost":
                updated = uploadpost.check(record)
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
