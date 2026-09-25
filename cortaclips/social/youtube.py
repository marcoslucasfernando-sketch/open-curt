"""YouTube Shorts vía YouTube Data API v3 (OAuth 2.0 de Google para apps de escritorio)."""

from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

from .. import config
from .oauth import SocialError, expires_at, http, put_file

NAME = "youtube"
LABEL = "YouTube Shorts"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
PKCE_HEX = False


def configured() -> bool:
    return bool(config.env("YOUTUBE_CLIENT_ID") and config.env("YOUTUBE_CLIENT_SECRET"))


def redirect_uri() -> str:
    return config.base_url() + "/oauth/callback"


def setup() -> dict:
    return {
        "console": "https://console.cloud.google.com/apis/credentials",
        "redirect_uri": redirect_uri(),
        "fields": ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"],
        "steps": [
            "Crea un proyecto en Google Cloud Console y activa «YouTube Data API v3».",
            "Pantalla de consentimiento OAuth: tipo Externo, añade tu cuenta como usuario de prueba y publícala («En producción») para que el acceso no caduque a los 7 días.",
            "Credenciales → Crear ID de cliente OAuth → tipo «App de escritorio». Copia el ID y el secreto aquí.",
            "Mientras Google no audite tu proyecto, YouTube deja los vídeos subidos por API como privados.",
        ],
    }


def auth_url(state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": config.env("YOUTUBE_CLIENT_ID"), "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _token_error(body) -> SocialError:
    detail = str(body)[:800]
    if isinstance(body, dict) and body.get("error") == "invalid_grant":
        return SocialError("Google ha revocado o caducado el acceso.", "Vuelve a conectar YouTube. Si tu app está en modo «Prueba», el acceso caduca cada 7 días.", detail)
    return SocialError("Google rechazó la autorización.", "Revisa el ID y el secreto de cliente de YouTube.", detail)


def exchange(code: str, verifier: str, redirect: str) -> dict:
    status, body, _ = http("POST", TOKEN_URL, form={
        "code": code, "client_id": config.env("YOUTUBE_CLIENT_ID"), "client_secret": config.env("YOUTUBE_CLIENT_SECRET"),
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    if status != 200 or "access_token" not in body:
        raise _token_error(body)
    token = {"access_token": body["access_token"], "refresh_token": body.get("refresh_token", ""), "expires_at": expires_at(body.get("expires_in"))}
    token["account"] = account(token)
    return token


def refresh(token: dict) -> dict:
    if not token.get("refresh_token"):
        raise SocialError("Falta el permiso de acceso continuo de YouTube.", "Desconecta y vuelve a conectar YouTube.")
    status, body, _ = http("POST", TOKEN_URL, form={
        "client_id": config.env("YOUTUBE_CLIENT_ID"), "client_secret": config.env("YOUTUBE_CLIENT_SECRET"),
        "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
    })
    if status != 200 or "access_token" not in body:
        raise _token_error(body)
    return {**token, "access_token": body["access_token"], "expires_at": expires_at(body.get("expires_in")),
            "refresh_token": body.get("refresh_token") or token["refresh_token"]}


def account(token: dict) -> dict:
    status, body, _ = http("GET", "https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                           headers={"Authorization": "Bearer " + token["access_token"]})
    if status != 200:
        raise SocialError("No se pudo leer tu canal de YouTube.", "Comprueba que la cuenta tiene un canal creado.", str(body)[:800])
    items = body.get("items") or []
    if not items:
        raise SocialError("Esta cuenta de Google no tiene canal de YouTube.", "Crea el canal en youtube.com y vuelve a conectar.")
    snippet = items[0]["snippet"]
    return {"id": items[0]["id"], "name": snippet.get("title", ""), "avatar": ((snippet.get("thumbnails") or {}).get("default") or {}).get("url", "")}


def publish(token: dict, video: Path, clip: dict, options: dict, progress) -> dict:
    title = clip.get("title") or "Clip"
    tags = [t for t in clip.get("hashtags", []) if t][:12]
    description = (clip.get("caption") or "").strip()
    description += "\n\n" + " ".join("#" + t for t in tags + ["Shorts"])
    privacy = options.get("youtube_privacy", "public")
    metadata = {
        "snippet": {"title": title[:100], "description": description[:4900], "tags": tags, "categoryId": "22"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False},
    }
    size = video.stat().st_size
    status, body, headers = http(
        "POST", "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "true"},
        json_body=metadata,
        headers={"Authorization": "Bearer " + token["access_token"], "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
    )
    location = headers.get("Location") or headers.get("location")
    if status != 200 or not location:
        raise _upload_error(status, body)
    progress(0.1, "Subiendo a YouTube…")
    uploaded = None
    for attempt in range(3):
        status, uploaded, _ = put_file(location, video, {"Content-Type": "video/mp4", "Authorization": "Bearer " + token["access_token"]})
        if status in (200, 201):
            break
        if status not in (500, 502, 503, 504):
            raise _upload_error(status, uploaded)
        time.sleep(3 * (attempt + 1))
    else:
        raise _upload_error(status, uploaded)
    video_id = uploaded.get("id", "")
    final_privacy = (uploaded.get("status") or {}).get("privacyStatus", privacy)
    note = ""
    if privacy != "private" and final_privacy == "private":
        note = "YouTube lo ha dejado privado: tu proyecto de Google aún no está auditado."
    return {"status": "published" if final_privacy != "private" else "private", "id": video_id,
            "url": f"https://youtube.com/shorts/{video_id}" if video_id else "", "message": note or f"Subido ({final_privacy})."}


def _upload_error(status: int, body) -> SocialError:
    detail = str(body)[:1000]
    reason = ""
    if isinstance(body, dict):
        errors = (body.get("error") or {}).get("errors") or []
        reason = errors[0].get("reason", "") if errors else ""
    if status == 401:
        return SocialError("YouTube rechazó el acceso (401).", "Vuelve a conectar YouTube.", detail, status, "auth")
    if reason in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded"):
        return SocialError("Has alcanzado el límite diario de subidas de YouTube.", "Inténtalo mañana o pide más cuota en Google Cloud.", detail, status)
    if reason == "youtubeSignupRequired":
        return SocialError("La cuenta no tiene canal de YouTube.", "Crea un canal y vuelve a conectar.", detail, status)
    return SocialError(f"YouTube devolvió un error ({status}).", reason or "Revisa los detalles.", detail, status)
