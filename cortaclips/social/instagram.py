"""Instagram Reels vía «Instagram API with Instagram Login» (cuentas profesionales)."""

from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

from .. import config
from .oauth import SocialError, expires_at, http

NAME = "instagram"
LABEL = "Instagram Reels"
AUTH_URL = "https://www.instagram.com/oauth/authorize"
SCOPES = "instagram_business_basic,instagram_business_content_publish"
PKCE_HEX = False
USES_PKCE = False  # Instagram Login no admite PKCE; la protección es el parámetro state.


def version() -> str:
    return config.env("INSTAGRAM_API_VERSION") or "v26.0"


def graph(path: str) -> str:
    return f"https://graph.instagram.com/{version()}/{path.lstrip('/')}"


def configured() -> bool:
    return bool(config.env("INSTAGRAM_APP_ID") and config.env("INSTAGRAM_APP_SECRET"))


def redirect_uri() -> str:
    # Instagram exige HTTPS: una página estática (Netlify) reenvía el código a 127.0.0.1.
    return config.https_redirect()


def setup() -> dict:
    return {
        "console": "https://developers.facebook.com/apps/",
        "redirect_uri": redirect_uri(),
        "fields": ["INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"],
        "steps": [
            "Tu cuenta de Instagram debe ser profesional (Creador o Empresa): Ajustes → Tipo de cuenta.",
            "En developers.facebook.com crea una app de tipo «Empresa» y añade el producto «Instagram» → «API con inicio de sesión de Instagram».",
            "En «Configurar inicio de sesión para empresas» añade esta URL de redirección (es HTTPS: reenvía a tu Mac).",
            "Añade tu cuenta de Instagram como «Probador de Instagram» en Roles de la app y acepta la invitación en Instagram (Ajustes → Apps y sitios web).",
            "Copia aquí el «Identificador de la app de Instagram» y su clave secreta (no los de Facebook).",
        ],
    }


def auth_url(state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": config.env("INSTAGRAM_APP_ID"), "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": SCOPES, "state": state, "enable_fb_login": "0",
    })


def _fail(what: str, status: int, body, hint: str = "") -> SocialError:
    error = body.get("error", {}) if isinstance(body, dict) else {}
    message = error.get("error_user_msg") or error.get("message") or str(body)[:300]
    code = str(error.get("code", ""))
    if code == "190" or status == 401:
        hint = hint or "El acceso caducó: vuelve a conectar Instagram."
    elif code in ("4", "17", "32", "613") or "limit" in message.lower():
        hint = hint or "Instagram limita las publicaciones por API (100 al día). Espera un poco."
    return SocialError(f"Instagram: {what}.", hint or message[:300], str(body)[:1000], status, code)


def exchange(code: str, verifier: str, redirect: str) -> dict:
    code = code.split("#")[0]
    status, body, _ = http("POST", "https://api.instagram.com/oauth/access_token", form={
        "client_id": config.env("INSTAGRAM_APP_ID"), "client_secret": config.env("INSTAGRAM_APP_SECRET"),
        "grant_type": "authorization_code", "redirect_uri": redirect, "code": code,
    })
    if isinstance(body, dict) and isinstance(body.get("data"), list) and body["data"]:
        body = body["data"][0]
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise _fail("no se pudo completar la autorización", status, body,
                    "Revisa el ID/secreto de la app de Instagram y que la URL de redirección esté añadida tal cual.")
    short = body["access_token"]
    status, long_lived, _ = http("GET", "https://graph.instagram.com/access_token", params={
        "grant_type": "ig_exchange_token", "client_secret": config.env("INSTAGRAM_APP_SECRET"), "access_token": short,
    })
    if status != 200 or "access_token" not in long_lived:
        raise _fail("no se pudo obtener el acceso de larga duración", status, long_lived)
    token = {"access_token": long_lived["access_token"], "expires_at": expires_at(long_lived.get("expires_in", 5184000)),
             "obtained_at": time.time(), "user_id": str(body.get("user_id", ""))}
    token["account"] = account(token)
    return token


def refresh(token: dict) -> dict:
    if time.time() - token.get("obtained_at", 0) < 24 * 3600:
        return token
    status, body, _ = http("GET", "https://graph.instagram.com/refresh_access_token", params={
        "grant_type": "ig_refresh_token", "access_token": token["access_token"],
    })
    if status != 200 or "access_token" not in body:
        raise _fail("no se pudo renovar el acceso", status, body)
    return {**token, "access_token": body["access_token"], "expires_at": expires_at(body.get("expires_in", 5184000)), "obtained_at": time.time()}


def needs_refresh(token: dict) -> bool:
    # Los tokens de Instagram duran 60 días; los renovamos cuando quedan menos de 10.
    return token.get("expires_at", 0) - time.time() < 10 * 86400


def account(token: dict) -> dict:
    status, body, _ = http("GET", graph("me"), params={"fields": "user_id,username,profile_picture_url,account_type", "access_token": token["access_token"]})
    if status != 200:
        raise _fail("no se pudo leer tu cuenta", status, body)
    return {"id": str(body.get("user_id") or body.get("id") or token.get("user_id", "")), "name": body.get("username", ""),
            "avatar": body.get("profile_picture_url", ""), "type": body.get("account_type", "")}


def publish(token: dict, video: Path, clip: dict, options: dict, progress) -> dict:
    ig_id = token.get("account", {}).get("id") or token.get("user_id")
    access = token["access_token"]
    caption = ((clip.get("caption") or clip.get("title") or "").strip() + "\n\n" + " ".join("#" + t for t in clip.get("hashtags", []))).strip()
    status, body, _ = http("POST", graph(f"{ig_id}/media"), form={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200], "share_to_feed": "true",
        "thumb_offset": "1500", "access_token": access,
    })
    if status != 200 or "id" not in body:
        raise _fail("no se pudo crear la publicación", status, body)
    container = body["id"]
    progress(0.15, "Subiendo a Instagram…")
    size = video.stat().st_size
    with video.open("rb") as handle:
        data = handle.read()
    status, result, _ = http("POST", f"https://rupload.facebook.com/ig-api-upload/{version()}/{container}", data=data, timeout=900,
                             headers={"Authorization": "OAuth " + access, "offset": "0", "file_size": str(size), "Content-Type": "application/octet-stream"})
    if status != 200 or (isinstance(result, dict) and result.get("success") is False):
        raise _fail("se rechazó la subida del vídeo", status, result)
    progress(0.6, "Instagram está procesando el Reel…")
    deadline = time.time() + 600
    while True:
        status, state, _ = http("GET", graph(container), params={"fields": "status_code,status", "access_token": access})
        code = state.get("status_code", "") if isinstance(state, dict) else ""
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise _fail("no pudo procesar el vídeo", status, state, str(state.get("status", ""))[:300])
        if time.time() > deadline:
            return {"status": "processing", "id": container, "url": "", "message": "Instagram sigue procesando; revisa el estado más tarde."}
        time.sleep(6)
    status, published, _ = http("POST", graph(f"{ig_id}/media_publish"), form={"creation_id": container, "access_token": access})
    if status != 200 or "id" not in published:
        raise _fail("no se pudo publicar", status, published)
    media_id = published["id"]
    status, info, _ = http("GET", graph(media_id), params={"fields": "permalink", "access_token": access})
    url = info.get("permalink", "") if isinstance(info, dict) else ""
    return {"status": "published", "id": media_id, "url": url, "message": "Publicado en Instagram."}
