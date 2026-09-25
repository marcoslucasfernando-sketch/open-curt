"""TikTok vía Content Posting API (Login Kit para escritorio con PKCE)."""

from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

from .. import config
from .oauth import SocialError, expires_at, http, put_file

NAME = "tiktok"
LABEL = "TikTok"
API = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.upload,video.publish"
PKCE_HEX = True  # TikTok escritorio: code_challenge = SHA-256 en hexadecimal
MIN_CHUNK = 5 * 1024 * 1024
CHUNK = 10 * 1024 * 1024

HINTS = {
    "unaudited_client_can_only_post_to_private_accounts":
        "TikTok solo deja publicar en «Solo yo» hasta que auditen tu app. Elige «Solo yo» o el modo «Borrador» en Ajustes → Redes.",
    "privacy_level_option_mismatch": "Esa privacidad no está disponible para tu cuenta. Cambia la privacidad de TikTok en Ajustes → Redes.",
    "spam_risk_too_many_posts": "TikTok limita las publicaciones diarias. Espera unas horas.",
    "spam_risk_too_many_pending_share": "Tienes demasiados borradores pendientes en TikTok. Publica o borra algunos desde la app.",
    "access_token_invalid": "Vuelve a conectar TikTok.",
    "scope_not_authorized": "Vuelve a conectar TikTok y acepta todos los permisos.",
    "url_ownership_unverified": "Usa la subida de archivo (la app ya lo hace); revisa la configuración de tu app de TikTok.",
}


def configured() -> bool:
    return bool(config.env("TIKTOK_CLIENT_KEY") and config.env("TIKTOK_CLIENT_SECRET"))


def redirect_uri() -> str:
    return config.base_url() + "/oauth/callback"


def setup() -> dict:
    return {
        "console": "https://developers.tiktok.com/apps",
        "redirect_uri": redirect_uri(),
        "fields": ["TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"],
        "steps": [
            "Crea una app en developers.tiktok.com y añade los productos «Login Kit» y «Content Posting API».",
            "En Login Kit elige la plataforma «Desktop» y registra esta URL de redirección exacta.",
            "Pide los permisos user.info.basic, video.upload y video.publish. Usa las URLs de la web pública (privacidad y términos) al rellenar la ficha.",
            "Sin auditoría, TikTok solo permite borradores o publicaciones en «Solo yo». El modo «Borrador» envía el vídeo a tu bandeja de TikTok para publicarlo desde el móvil.",
        ],
    }


def auth_url(state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": config.env("TIKTOK_CLIENT_KEY"), "response_type": "code", "scope": SCOPES,
        "redirect_uri": redirect_uri(), "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _check(status: int, body, what: str) -> dict:
    error = (body.get("error") or {}) if isinstance(body, dict) else {}
    code = error.get("code", "")
    if status == 200 and code in ("", "ok"):
        return body.get("data") or {}
    message = error.get("message") or str(body)[:300]
    raise SocialError(f"TikTok: {what} ({code or status}).", HINTS.get(code, message[:300]), str(body)[:1000], status, code)


def _token(form: dict) -> dict:
    status, body, _ = http("POST", API + "/v2/oauth/token/", form={
        "client_key": config.env("TIKTOK_CLIENT_KEY"), "client_secret": config.env("TIKTOK_CLIENT_SECRET"), **form,
    })
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise SocialError("TikTok rechazó la autorización.", "Revisa el Client Key/Secret y que la URL de redirección esté registrada.", str(body)[:800], status)
    return {
        "access_token": body["access_token"], "refresh_token": body.get("refresh_token", ""),
        "expires_at": expires_at(body.get("expires_in", 86400)),
        "refresh_expires_at": expires_at(body.get("refresh_expires_in", 31536000)),
        "open_id": body.get("open_id", ""), "scope": body.get("scope", ""),
    }


def exchange(code: str, verifier: str, redirect: str) -> dict:
    token = _token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect, "code_verifier": verifier})
    token["account"] = account(token)
    return token


def refresh(token: dict) -> dict:
    fresh = _token({"grant_type": "refresh_token", "refresh_token": token.get("refresh_token", "")})
    return {**token, **fresh}


def _auth(token: dict) -> dict:
    return {"Authorization": "Bearer " + token["access_token"]}


def account(token: dict) -> dict:
    status, body, _ = http("GET", API + "/v2/user/info/", params={"fields": "open_id,avatar_url,display_name"}, headers=_auth(token))
    user = _check(status, body, "no se pudo leer tu perfil").get("user", {})
    return {"id": user.get("open_id", token.get("open_id", "")), "name": user.get("display_name", "TikTok"), "avatar": user.get("avatar_url", "")}


def creator_info(token: dict) -> dict:
    status, body, _ = http("POST", API + "/v2/post/publish/creator_info/query/", json_body={}, headers=_auth(token))
    return _check(status, body, "no se pudo consultar tu cuenta")


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) según las reglas de TikTok."""
    # Hasta 64 MB se sube de una vez (por debajo de 5 MB es obligatorio hacerlo así).
    if size <= 64 * 1024 * 1024:
        return size, 1
    # Trozos de 10 MB; el último absorbe el resto (TikTok admite hasta 128 MB en el último).
    return CHUNK, size // CHUNK


def publish(token: dict, video: Path, clip: dict, options: dict, progress) -> dict:
    size = video.stat().st_size
    chunk_size, count = chunk_plan(size)
    source_info = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": count}
    mode = options.get("tiktok_mode", "draft")
    caption = ((clip.get("caption") or clip.get("title") or "").strip() + " " + " ".join("#" + t for t in clip.get("hashtags", []))).strip()
    if mode == "direct":
        info = creator_info(token)
        privacy = options.get("tiktok_privacy", "SELF_ONLY")
        allowed = info.get("privacy_level_options") or []
        if allowed and privacy not in allowed:
            privacy = "SELF_ONLY" if "SELF_ONLY" in allowed else allowed[0]
        limit = info.get("max_video_post_duration_sec")
        if limit and float(clip["end"]) - float(clip["start"]) > float(limit):
            raise SocialError(f"TikTok solo permite vídeos de hasta {limit} s en tu cuenta.", "Recorta el clip.")
        body = {
            "post_info": {
                "title": caption[:2200], "privacy_level": privacy,
                "disable_duet": bool(info.get("duet_disabled")), "disable_comment": bool(info.get("comment_disabled")),
                "disable_stitch": bool(info.get("stitch_disabled")), "video_cover_timestamp_ms": 1000,
            },
            "source_info": source_info,
        }
        endpoint = "/v2/post/publish/video/init/"
    else:
        body = {"source_info": source_info}
        endpoint = "/v2/post/publish/inbox/video/init/"
    status, response, _ = http("POST", API + endpoint, json_body=body, headers=_auth(token))
    data = _check(status, response, "no se pudo iniciar la subida")
    upload_url, publish_id = data.get("upload_url"), data.get("publish_id")
    if not upload_url:
        raise SocialError("TikTok no devolvió dirección de subida.", "", str(response)[:800])
    for index in range(count):
        start = index * chunk_size
        end = size - 1 if index == count - 1 else start + chunk_size - 1
        status, result, _ = put_file(upload_url, video, {"Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end}/{size}"},
                                     offset=start, length=end - start + 1)
        if status not in (200, 201, 206):
            raise SocialError(f"TikTok rechazó la subida ({status}).", "Vuelve a intentarlo.", str(result)[:800], status)
        progress(0.1 + 0.8 * (index + 1) / count, f"Subiendo a TikTok… {index + 1}/{count}")
    return wait(token, publish_id, mode, progress)


def status(token: dict, publish_id: str) -> dict:
    code, body, _ = http("POST", API + "/v2/post/publish/status/fetch/", json_body={"publish_id": publish_id}, headers=_auth(token))
    return _check(code, body, "no se pudo consultar el estado")


def wait(token: dict, publish_id: str, mode: str, progress, timeout: float = 240) -> dict:
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        state = status(token, publish_id)
        value = state.get("status", "")
        if value == "FAILED":
            reason = state.get("fail_reason", "")
            raise SocialError("TikTok no pudo procesar el vídeo.", HINTS.get(reason, reason), str(state)[:800], code=reason)
        if value == "SEND_TO_USER_INBOX":
            return {"status": "draft", "id": publish_id, "url": "", "message": "Enviado a tu bandeja de TikTok: ábrelo en la app para publicarlo."}
        if value == "PUBLISH_COMPLETE":
            ids = state.get("publicaly_available_post_id") or []
            url = f"https://www.tiktok.com/video/{ids[0]}" if ids else ""
            return {"status": "published", "id": str(ids[0]) if ids else publish_id, "url": url, "message": "Publicado en TikTok."}
        progress(0.92, "TikTok está procesando el vídeo…")
        time.sleep(5)
    return {"status": "processing", "id": publish_id, "url": "", "message": "TikTok sigue procesando; revisa el estado más tarde."}
