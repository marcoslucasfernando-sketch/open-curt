"""Alternativa sin apps de desarrollador: Upload-Post (servicio de terceros, de pago)."""

from __future__ import annotations

import time
import urllib.parse
import uuid
from pathlib import Path

from .. import config
from .oauth import SocialError, http

NAME = "uploadpost"
LABEL = "Upload-Post"
BASE = "https://api.upload-post.com"
PLATFORMS = ("youtube", "tiktok", "instagram")


def configured() -> bool:
    return bool(config.env("UPLOAD_POST_API_KEY") and config.env("UPLOAD_POST_USER"))


def _headers() -> dict:
    return {"Authorization": "Apikey " + config.env("UPLOAD_POST_API_KEY")}


def connected_accounts() -> list[str]:
    user = urllib.parse.quote(config.env("UPLOAD_POST_USER"), safe="")
    status, body, _ = http("GET", f"{BASE}/api/uploadposts/users/{user}", headers=_headers(), timeout=30)
    if status != 200 or not isinstance(body, dict):
        raise SocialError(f"No se pudo verificar el perfil de Upload-Post ({status}).", "Revisa UPLOAD_POST_API_KEY y UPLOAD_POST_USER.", str(body)[:600], status)
    accounts = (body.get("profile") or {}).get("social_accounts") or {}
    return [p for p in PLATFORMS if accounts.get(p)]


def publish(video: Path, clip: dict, job_id: str, progress) -> dict:
    platforms = connected_accounts()
    if not platforms:
        raise SocialError("Tu perfil de Upload-Post no tiene redes conectadas.", "Conéctalas en upload-post.com.")
    request_id = f"cortaclips-{job_id}-{clip['number']}-{int(time.time())}"
    caption = ((clip.get("caption") or "") + " " + " ".join("#" + t for t in clip.get("hashtags", []))).strip()
    fields = [
        ("user", config.env("UPLOAD_POST_USER")), ("title", (clip.get("title") or "Clip")[:100]),
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
    progress(0.2, "Enviando a Upload-Post…")
    status, result, _ = http("POST", BASE + "/api/upload", data=bytes(body), timeout=600, headers={
        **_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}", "Idempotency-Key": request_id,
    })
    if status >= 300 or (isinstance(result, dict) and result.get("success") is False):
        raise SocialError(f"Upload-Post rechazó el envío ({status}).", str((result or {}).get("message", ""))[:300] if isinstance(result, dict) else "", str(result)[:800], status)
    remote = (result or {}).get("request_id") or request_id if isinstance(result, dict) else request_id
    return {"status": "processing", "id": remote, "url": "", "message": "Enviado a Upload-Post para " + ", ".join(platforms) + "."}


def check(record: dict) -> dict:
    status, body, _ = http("GET", BASE + "/api/uploadposts/status", params={"request_id": record.get("id", "")}, headers=_headers(), timeout=30)
    if status != 200 or not isinstance(body, dict):
        return record
    results = body.get("results") or []
    parts = []
    for item in results:
        state = "error" if item.get("success") is False else (item.get("status") or "ok")
        parts.append(f"{item.get('platform')}: {state}")
    done = str(body.get("status", "")).lower() in ("completed", "finished", "success", "done")
    return {**record, "status": "published" if done else record.get("status", "processing"),
            "message": " · ".join(parts) or str(body.get("status", ""))}
