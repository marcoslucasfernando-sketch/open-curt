"""Infraestructura OAuth 2.0 común: PKCE, estados anti-CSRF, tokens y HTTP."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import config

_lock = threading.Lock()
_pending: dict[str, dict] = {}
STATE_TTL = 20 * 60


class SocialError(RuntimeError):
    def __init__(self, message: str, hint: str = "", detail: str = "", status: int = 0, code: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail
        self.status = status
        self.code = code


# ---------------------------------------------------------------- tokens

def _read_tokens() -> dict:
    try:
        data = json.loads(config.TOKENS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_tokens(data: dict) -> None:
    config.DATA.mkdir(parents=True, exist_ok=True)
    tmp = config.TOKENS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(config.TOKENS_FILE)


def all_tokens() -> dict:
    with _lock:
        return _read_tokens()


def get_token(platform: str) -> dict | None:
    with _lock:
        return _read_tokens().get(platform)


def set_token(platform: str, token: dict) -> None:
    with _lock:
        data = _read_tokens()
        data[platform] = token
        _write_tokens(data)


def delete_token(platform: str) -> None:
    with _lock:
        data = _read_tokens()
        data.pop(platform, None)
        _write_tokens(data)


# ---------------------------------------------------------------- PKCE y estado

def pkce_pair(hex_challenge: bool = False) -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    # TikTok (escritorio) pide el SHA-256 en hexadecimal; el resto, base64url sin relleno.
    challenge = digest.hex() if hex_challenge else base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def new_state(platform: str, verifier: str, redirect_uri: str) -> str:
    # El puerto va delante para que la página HTTPS de retorno sepa a qué 127.0.0.1 volver.
    state = f"{config.port()}.{platform}.{secrets.token_urlsafe(24)}"
    now = time.time()
    with _lock:
        for key in [k for k, v in _pending.items() if now - v["created"] > STATE_TTL]:
            _pending.pop(key, None)
        _pending[state] = {"platform": platform, "verifier": verifier, "redirect_uri": redirect_uri, "created": now}
    return state


def pop_state(state: str) -> dict:
    with _lock:
        entry = _pending.pop(state or "", None)
    if not entry or time.time() - entry["created"] > STATE_TTL:
        raise SocialError("La autorización caducó o no se inició desde esta app.", "Vuelve a pulsar «Conectar» y completa el proceso en menos de 20 minutos.")
    return entry


# ---------------------------------------------------------------- HTTP

def http(method: str, url: str, *, params: dict | None = None, form: dict | None = None, json_body=None,
         headers: dict | None = None, data: bytes | None = None, timeout: float = 60, raw: bool = False):
    """Petición HTTP que devuelve (status, cuerpo JSON o texto, cabeceras)."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = dict(headers or {})
    body = data
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        body = json.dumps(json_body).encode()
        headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            status, response_headers = response.status, dict(response.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status, response_headers = exc.code, dict(exc.headers or {})
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise SocialError("No hay conexión con la red social.", "Comprueba tu conexión a Internet.", str(exc)) from exc
    if raw:
        return status, payload, response_headers
    text = payload.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        parsed = text
    return status, parsed, response_headers


def put_file(url: str, path: Path, headers: dict, offset: int = 0, length: int | None = None, timeout: float = 600):
    """Sube un fragmento de archivo sin cargarlo entero en memoria."""
    size = path.stat().st_size
    length = size - offset if length is None else length
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(length)
    return http("PUT", url, data=chunk, headers={**headers, "Content-Length": str(len(chunk))}, timeout=timeout)


def expires_at(seconds) -> float:
    try:
        return time.time() + float(seconds) - 60
    except (TypeError, ValueError):
        return time.time() + 3000
