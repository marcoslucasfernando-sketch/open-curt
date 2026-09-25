"""Upload-Post connector for queued distribution to three social networks."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


BASE = "https://api.upload-post.com"
PLATFORMS = ("youtube", "tiktok", "instagram")


def configured() -> bool:
    return bool(os.getenv("UPLOAD_POST_API_KEY") and os.getenv("UPLOAD_POST_USER"))


def check_connected_accounts() -> dict:
    if not configured():
        raise RuntimeError("Configura UPLOAD_POST_API_KEY y UPLOAD_POST_USER para publicar.")
    user = urllib.parse.quote(os.environ["UPLOAD_POST_USER"], safe="")
    req = urllib.request.Request(BASE + "/api/uploadposts/users/" + user, headers={"Authorization": "Apikey " + os.environ["UPLOAD_POST_API_KEY"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            profile = json.load(response).get("profile", {})
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"No se pudo verificar el perfil Upload-Post ({exc.code}).") from exc
    accounts = profile.get("social_accounts", {})
    missing = [platform for platform in PLATFORMS if not accounts.get(platform)]
    if missing:
        raise RuntimeError("Conecta estas cuentas en Upload-Post antes de publicar: " + ", ".join(missing))
    return {platform: accounts[platform] for platform in PLATFORMS}


def publish(path: Path, title: str, caption: str, job_id: str, number: int) -> dict:
    if not configured():
        raise RuntimeError("Configura UPLOAD_POST_API_KEY y UPLOAD_POST_USER para publicar.")
    if not path.is_file():
        raise FileNotFoundError(path)
    boundary = "----cortaclips" + uuid.uuid4().hex
    request_id = f"cortaclips-{job_id}-{number}"
    fields = [
        ("user", os.environ["UPLOAD_POST_USER"]),
        ("title", title[:100]),
        ("description", caption[:2000]),
        ("media_type", "REELS"),
        ("add_to_queue", "true"),
        ("async_upload", "true"),
        ("request_id", request_id),
        ("disable_inbox_fallback", "true"),
    ] + [("platform[]", platform) for platform in PLATFORMS]
    body = bytearray()
    for key, value in fields:
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="{path.name}"\r\nContent-Type: video/mp4\r\n\r\n'.encode())
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        BASE + "/api/upload", data=bytes(body),
        headers={
            "Authorization": "Apikey " + os.environ["UPLOAD_POST_API_KEY"],
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Idempotency-Key": request_id,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:800]
        raise RuntimeError(f"Upload-Post ({exc.code}): {detail}") from exc
    if result.get("success") is False:
        raise RuntimeError("Upload-Post rechazó el envío: " + str(result.get("message") or result.get("error") or result)[:500])
    result["client_request_id"] = request_id
    return result


def status(request_id: str | None = None, job_id: str | None = None) -> dict:
    if not configured():
        raise RuntimeError("Upload-Post no está configurado.")
    if not request_id and not job_id:
        raise ValueError("Falta request_id o job_id.")
    query = urllib.parse.urlencode({"request_id": request_id} if request_id else {"job_id": job_id})
    req = urllib.request.Request(BASE + "/api/uploadposts/status?" + query, headers={"Authorization": "Apikey " + os.environ["UPLOAD_POST_API_KEY"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Upload-Post status ({exc.code}): {exc.read().decode(errors='replace')[:500]}") from exc
