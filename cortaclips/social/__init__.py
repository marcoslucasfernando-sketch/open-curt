"""Publicación en YouTube con la API oficial (OAuth con tu ID de cliente y secreto de Google).

Admite varios canales de la misma cuenta de Google (personal y de marca): cada canal se
autoriza por separado («Añadir canal») y al subir eliges a cuál va.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .. import config
from ..util import format_exception
from . import oauth, youtube
from .oauth import SocialError

PLATFORMS = {"youtube": youtube}
PREFIX = "youtube:"


def _migrate() -> None:
    """Versiones anteriores guardaban un solo canal con la clave «youtube»."""
    old = oauth.get_token("youtube")
    if old:
        channel = (old.get("account") or {}).get("id")
        if channel and not oauth.get_token(PREFIX + channel):
            oauth.set_token(PREFIX + channel, old)
        oauth.delete_token("youtube")


def channels() -> list[dict]:
    _migrate()
    default = config.get_settings().get("youtube_channel", "")
    found = []
    for key, token in sorted(oauth.all_tokens().items()):
        if key.startswith(PREFIX):
            account = token.get("account") or {}
            found.append({"id": key[len(PREFIX):], "name": account.get("name") or "Canal", "avatar": account.get("avatar", "")})
    if found and default not in {c["id"] for c in found}:
        default = found[0]["id"]
    for channel in found:
        channel["default"] = channel["id"] == default
    return found


def default_channel() -> str:
    return next((c["id"] for c in channels() if c["default"]), "")


def status() -> dict:
    listed = channels()
    return {
        "youtube": {
            "label": youtube.LABEL,
            "configured": youtube.configured(),
            "connected": bool(listed),
            "channels": listed,
            "default": next((c["id"] for c in listed if c["default"]), ""),
            "setup": youtube.setup(),
        }
    }


def start(platform: str = "youtube") -> str:
    if platform != "youtube":
        raise SocialError("Solo se puede conectar YouTube.")
    if not youtube.configured():
        raise SocialError("Falta el ID de cliente y el secreto de Google.", "Pégalos en Ajustes → YouTube.")
    verifier, challenge = oauth.pkce_pair()
    state = oauth.new_state("youtube", verifier, youtube.redirect_uri())
    return youtube.auth_url(state, challenge)


def complete(params: dict) -> dict:
    """Termina el inicio de sesión con Google y guarda el canal elegido. Devuelve el canal."""
    entry = oauth.pop_state(params.get("state", ""))
    if params.get("error"):
        if params["error"] == "access_denied":
            raise SocialError("Has cancelado el inicio de sesión con Google.", "Vuelve a pulsar «Iniciar sesión con Google» y acepta los permisos.")
        raise SocialError("Google no completó el inicio de sesión.", str(params.get("error_description") or params["error"])[:300])
    code = params.get("code", "")
    if not code:
        raise SocialError("Google no devolvió el código de autorización.", "Vuelve a intentarlo.")
    token = youtube.exchange(code, entry["verifier"], entry["redirect_uri"])
    token["connected_at"] = time.time()
    channel = token["account"]["id"]
    oauth.set_token(PREFIX + channel, token)
    if not default_channel() or len(channels()) == 1:
        config.update_settings({"youtube_channel": channel})
    return token["account"]


def disconnect(channel: str) -> None:
    oauth.delete_token(PREFIX + channel)
    if config.get_settings().get("youtube_channel") == channel:
        remaining = channels()
        config.update_settings({"youtube_channel": remaining[0]["id"] if remaining else ""})


def set_default(channel: str) -> None:
    if not oauth.get_token(PREFIX + channel):
        raise SocialError("Ese canal no está conectado.")
    config.update_settings({"youtube_channel": channel})


def valid_token(channel: str) -> dict:
    token = oauth.get_token(PREFIX + channel)
    if not token:
        raise SocialError("Ese canal de YouTube no está conectado.", "Ajustes → YouTube → Añadir otro canal.")
    if token.get("expires_at", 0) < time.time() + 120:
        token = youtube.refresh(token)
        oauth.set_token(PREFIX + channel, token)
    return token


class Publisher:
    """Sube clips a YouTube en segundo plano y guarda el estado en el propio clip (uno por canal)."""

    def __init__(self, manager):
        self.manager = manager
        self.lock = threading.Lock()

    def publish(self, job_id: str, number: int, channel: str = "", options: dict | None = None) -> None:
        job = self.manager.get(job_id)
        if not job:
            raise SocialError("Trabajo no encontrado.")
        clip = next((c for c in job["clips"] if c["number"] == number), None)
        if not clip or not clip.get("file"):
            raise SocialError("El clip aún no está listo.")
        if clip.get("status") == "rendering":
            raise SocialError("Espera a que termine de renderizarse.")
        channel = channel or default_channel()
        info = next((c for c in channels() if c["id"] == channel), None)
        if not info:
            raise SocialError("Ese canal de YouTube no está conectado.", "Ajustes → YouTube → Añadir otro canal.")
        key = PREFIX + channel
        if ((clip.get("publications") or {}).get(key) or {}).get("status") == "uploading":
            raise SocialError(f"Este clip ya se está subiendo a {info['name']}.")
        settings = {**config.get_settings(), **(options or {})}
        self._record(job_id, number, key, status="uploading", progress=0.0, message="Preparando…", url="", id="", error="",
                     channel=info["name"], privacy=settings.get("youtube_privacy", "public"))
        threading.Thread(target=self._run, args=(job_id, number, channel, settings), daemon=True).start()

    def _record(self, job_id: str, number: int, key: str, **values) -> None:
        with self.lock:
            job = self.manager.get(job_id)
            clip = next(c for c in job["clips"] if c["number"] == number)
            publications = dict(clip.get("publications") or {})
            publications[key] = {**publications.get(key, {}), **values, "at": time.time()}
            self.manager.update_clip(job_id, number, publications=publications)

    def _run(self, job_id: str, number: int, channel: str, settings: dict) -> None:
        key = PREFIX + channel
        job = self.manager.get(job_id)
        clip = next(c for c in job["clips"] if c["number"] == number)
        video = self.manager.outputs / job_id / clip["file"]

        def progress(fraction: float, message: str) -> None:
            self._record(job_id, number, key, progress=round(fraction, 3), message=message)

        try:
            token = valid_token(channel)
            try:
                result = youtube.publish(token, video, clip, settings, progress)
            except SocialError as exc:
                if exc.status != 401 and exc.code != "auth":
                    raise
                token = youtube.refresh(token)
                oauth.set_token(key, token)
                result = youtube.publish(token, video, clip, settings, progress)
            self._record(job_id, number, key, progress=1.0, error="", **result)
        except Exception as exc:  # noqa: BLE001
            self._log(self.manager.outputs / job_id, exc)
            if isinstance(exc, SocialError):
                error, hint = exc.message, exc.hint
            else:
                error, hint = f"Error inesperado: {exc}"[:300], "Revisa job.log"
            self._record(job_id, number, key, status="error", error=error, message=hint, progress=0.0)

    @staticmethod
    def _log(folder: Path, exc: BaseException) -> None:
        try:
            with (folder / "job.log").open("a", encoding="utf-8") as handle:
                handle.write(f"[YouTube] {exc}\n{getattr(exc, 'detail', '')}\n{format_exception(exc)}\n")
        except OSError:
            pass
