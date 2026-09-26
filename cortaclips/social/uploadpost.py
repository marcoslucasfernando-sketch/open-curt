"""Conexión rápida vía Upload-Post: sus apps ya están aprobadas por YouTube, TikTok e Instagram.

El usuario solo pega una API key una vez; después conecta cada red con su propio inicio de
sesión (YouTube y TikTok permiten «Continuar con Google»). Corta Clips crea el perfil solo.
"""

from __future__ import annotations

import time
import urllib.parse
import uuid
from pathlib import Path

from .. import config
from .oauth import SocialError, http

NAME = "uploadpost"
LABEL = "Conexión rápida"
BASE = "https://api.upload-post.com"
PLATFORMS = ("youtube", "tiktok", "instagram")
DEFAULT_PROFILE = "cortaclips"
SIGNUP_URL = "https://app.upload-post.com/"

_cache: dict = {"at": 0.0, "accounts": None}


def configured() -> bool:
    return bool(config.env("UPLOAD_POST_API_KEY"))


def _headers() -> dict:
    return {"Authorization": "Apikey " + config.env("UPLOAD_POST_API_KEY")}


def _fail(what: str, status: int, body) -> SocialError:
    message = body.get("message") or body.get("error") if isinstance(body, dict) else ""
    if status in (401, 403):
        return SocialError("Upload-Post rechazó la API key.", "Copia de nuevo la clave desde app.upload-post.com → API Keys.", str(body)[:600], status)
    return SocialError(f"Upload-Post: {what} ({status}).", str(message or "")[:300], str(body)[:800], status)


def invalidate() -> None:
    _cache.update(at=0.0, accounts=None)


def ensure_profile() -> str:
    """Devuelve el perfil a usar; si no hay ninguno lo crea y lo guarda en .env."""
    if not configured():
        raise SocialError("Falta la API key de la conexión rápida.", "Pégala en Ajustes → Redes → Conexión rápida.")
    current = config.env("UPLOAD_POST_USER")
    if current:
        return current
    status, body, _ = http("GET", BASE + "/api/uploadposts/users", headers=_headers(), timeout=30)
    if status != 200 or not isinstance(body, dict):
        raise _fail("no se pudieron leer tus perfiles", status, body)
    profiles = body.get("profiles") or []
    name = profiles[0].get("username") if profiles else ""
    if not name:
        status, created, _ = http("POST", BASE + "/api/uploadposts/users", json_body={"username": DEFAULT_PROFILE}, headers=_headers(), timeout=30)
        if status not in (200, 201):
            raise _fail("no se pudo crear el perfil", status, created)
        name = (created.get("profile") or {}).get("username") or DEFAULT_PROFILE if isinstance(created, dict) else DEFAULT_PROFILE
    config.save_env({"UPLOAD_POST_USER": name})
    return name


def accounts(max_age: float = 30.0) -> dict:
    """Cuentas conectadas en el perfil: {plataforma: {name, avatar, reauth}}. Con caché corta."""
    if not configured():
        return {}
    if _cache["accounts"] is not None and time.time() - _cache["at"] < max_age:
        return _cache["accounts"]
    try:
        profile = ensure_profile()
        status, body, _ = http("GET", BASE + "/api/uploadposts/users/" + urllib.parse.quote(profile, safe=""), headers=_headers(), timeout=30)
    except SocialError:
        return _cache["accounts"] or {}
    if status != 200 or not isinstance(body, dict):
        return _cache["accounts"] or {}
    social = (body.get("profile") or {}).get("social_accounts") or {}
    found = {}
    for platform in PLATFORMS:
        info = social.get(platform)
        if isinstance(info, dict) and info:
            found[platform] = {
                "name": info.get("display_name") or info.get("handle") or info.get("username") or "",
                "avatar": info.get("social_images") or "",
                "reauth": bool(info.get("reauth_required")),
            }
    _cache.update(at=time.time(), accounts=found)
    return found


def connect_url(platform: str) -> str:
    """Enlace al inicio de sesión oficial de la red (vía Upload-Post)."""
    if platform not in PLATFORMS:
        raise SocialError("Red social desconocida.")
    profile = ensure_profile()
    invalidate()
    status, body, _ = http("POST", f"{BASE}/api/uploadposts/oauth/{platform}/start", headers=_headers(), timeout=30,
                           json_body={"profile": profile, "redirect_url": f"{config.base_url()}/?conectado={platform}"})
    if status != 200 or not isinstance(body, dict) or not body.get("authorize_url"):
        raise _fail("no se pudo iniciar la conexión", status, body)
    return body["authorize_url"]


def publish(video: Path, clip: dict, job_id: str, progress, platforms: list[str] | None = None) -> dict:
    connected = list(accounts(max_age=0).keys())
    platforms = [p for p in (platforms or connected) if p in connected]
    if not platforms:
        raise SocialError("No hay redes conectadas en la conexión rápida.", "Conéctalas en Ajustes → Redes.")
    request_id = f"cortaclips-{job_id}-{clip['number']}-{int(time.time())}"
    caption = ((clip.get("caption") or "") + " " + " ".join("#" + t for t in clip.get("hashtags", []))).strip()
    fields = [
        ("user", ensure_profile()), ("title", (clip.get("title") or "Clip")[:100]),
        ("description", caption[:2000]), ("media_type", "REELS"), ("async_upload", "true"),
        ("request_id", request_id), ("disable_inbox_fallback", "true"),
    ] + [("platform[]", p) for p in platforms]
    boundary = "----cortaclips" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields:
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="{video.name}"\r\nContent-Type: video/mp4\r\n\r\n'.encode()
    body += video.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    progress(0.2, "Subiendo…")
    status, result, _ = http("POST", BASE + "/api/upload", data=bytes(body), timeout=900, headers={
        **_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}", "Idempotency-Key": request_id,
    })
    if status >= 300 or (isinstance(result, dict) and result.get("success") is False):
        if status == 402 or "plan" in str(result).lower() and "tiktok" in platforms:
            raise SocialError("Tu plan de Upload-Post no permite esta publicación.", "El plan gratuito no incluye TikTok y admite 10 subidas al mes.", str(result)[:800], status)
        raise _fail("rechazó el envío", status, result)
    remote = (result.get("request_id") if isinstance(result, dict) else None) or request_id
    return {"status": "processing", "id": remote, "url": "", "message": "Enviado. Pulsa ↻ en unos minutos para ver si ya está publicado."}


def check(record: dict, platform: str | None = None) -> dict:
    status, body, _ = http("GET", BASE + "/api/uploadposts/status", params={"request_id": record.get("id", "")}, headers=_headers(), timeout=30)
    if status != 200 or not isinstance(body, dict):
        return record
    results = body.get("results") or []
    mine = [r for r in results if not platform or r.get("platform") == platform]
    for item in mine:
        if item.get("success") is False:
            return {**record, "status": "error", "error": str(item.get("error") or item.get("message") or "Error")[:300]}
        url = item.get("url") or item.get("post_url") or item.get("permalink") or ""
        if item.get("success") or url:
            return {**record, "status": "published", "url": url, "message": "Publicado."}
    done = str(body.get("status", "")).lower() in ("completed", "finished", "success", "done")
    return {**record, "status": "published" if done else "processing", "message": str(body.get("status", "")) or record.get("message", "")}
