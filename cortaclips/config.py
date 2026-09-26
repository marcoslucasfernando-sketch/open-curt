"""Rutas, variables de entorno (.env) y preferencias persistentes."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
DATA = ROOT / ".data"
ENV_FILE = ROOT / ".env"
SETTINGS_FILE = DATA / "settings.json"
TOKENS_FILE = DATA / "tokens.json"
ASSETS = ROOT / "assets"
WEB = ROOT / "web"

HOST = "127.0.0.1"


def port() -> int:
    try:
        return int(os.environ.get("CORTACLIPS_PORT", "8766"))
    except ValueError:
        return 8766


def base_url() -> str:
    return f"http://{HOST}:{port()}"


# Claves que la interfaz puede escribir en .env. "secret" nunca se devuelve al navegador.
ENV_KEYS: dict[str, str] = {
    "OPENAI_API_KEY": "secret",
    "YOUTUBE_CLIENT_ID": "plain",
    "YOUTUBE_CLIENT_SECRET": "secret",
}

_lock = threading.Lock()


def _parse_env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    name, value = line.split("=", 1)
    name = name.strip()
    if name.startswith("export "):
        name = name[7:].strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return name, value


def load_env(path: Path = ENV_FILE) -> None:
    """Carga .env sin pisar variables ya exportadas en la terminal."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(raw)
        if parsed and parsed[1]:
            os.environ.setdefault(*parsed)


def save_env(updates: dict[str, str], path: Path | None = None) -> None:
    """Escribe claves permitidas en .env conservando el resto del archivo."""
    path = path or ENV_FILE
    clean = {}
    for name, value in updates.items():
        if name not in ENV_KEYS:
            raise ValueError(f"Clave no permitida: {name}")
        value = str(value).strip()
        if "\n" in value or "\r" in value:
            raise ValueError(f"Valor inválido para {name}")
        clean[name] = value
    with _lock:
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        seen = set()
        out = []
        for raw in lines:
            parsed = _parse_env_line(raw)
            if parsed and parsed[0] in clean:
                seen.add(parsed[0])
                out.append(f"{parsed[0]}={clean[parsed[0]]}")
            else:
                out.append(raw)
        for name, value in clean.items():
            if name not in seen:
                out.append(f"{name}={value}")
        path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        for name, value in clean.items():
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def credential_flags() -> dict[str, dict]:
    """Estado de cada credencial sin revelar secretos."""
    flags = {}
    for name, kind in ENV_KEYS.items():
        value = env(name)
        if kind == "secret":
            flags[name] = {"set": bool(value), "preview": ("…" + value[-4:]) if len(value) > 8 else ""}
        else:
            flags[name] = {"set": bool(value), "value": value}
    return flags


DEFAULT_SETTINGS: dict = {
    "ai_provider": "auto",          # auto | openai | codex | claude
    "ai_model": "gpt-6-luna",
    "ai_effort": "medium",           # none | low | medium | high | xhigh | max
    "claude_model": "",              # vacío = modelo por defecto de tu cuenta
    "transcriber": "auto",          # auto | openai | local
    "local_model": "auto",          # auto | small | medium | large-v3-turbo
    "language": "",                 # vacío = detección automática
    "layout": "auto",               # auto | faces | blur | crop
    "caption_style": "karaoke",     # karaoke | box | clean | none
    "caption_position": "bottom",   # bottom | middle | top
    "caption_uppercase": True,
    "caption_font": "bricolage",     # bricolage | poppins | anton | montserrat | unbounded | bebas | archivo
    "caption_color": "yellow",       # yellow | lime | cyan | pink | orange
    "hook_overlay": True,
    "progress_bar": True,
    "loudnorm": True,
    "cookies_browser": "",          # "" | chrome | safari | firefox | edge | brave
    "clip_count": 5,
    "min_seconds": 20,
    "max_seconds": 60,
    "youtube_privacy": "public",    # public | unlisted | private
    "youtube_channel": "",          # canal por defecto (ID) cuando hay varios conectados
}

CHOICES: dict[str, tuple] = {
    "ai_provider": ("auto", "openai", "codex", "claude"),
    "ai_effort": ("none", "low", "medium", "high", "xhigh", "max"),
    "transcriber": ("auto", "openai", "local"),
    "local_model": ("auto", "small", "medium", "large-v3-turbo"),
    "layout": ("auto", "faces", "blur", "crop"),
    "caption_style": ("karaoke", "box", "clean", "none"),
    "caption_position": ("bottom", "middle", "top"),
    "caption_font": ("bricolage", "poppins", "anton", "montserrat", "unbounded", "bebas", "archivo"),
    "caption_color": ("yellow", "lime", "cyan", "pink", "orange"),
    "cookies_browser": ("", "chrome", "safari", "firefox", "edge", "brave", "chromium", "opera", "vivaldi"),
    "youtube_privacy": ("public", "unlisted", "private"),
}


def get_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            settings.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    except (OSError, json.JSONDecodeError):
        pass
    return settings


def validate_settings(values: dict) -> dict:
    clean = {}
    for key, value in values.items():
        if key not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[key]
        if key in CHOICES:
            if value not in CHOICES[key]:
                raise ValueError(f"Valor no válido para {key}: {value}")
        elif isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int):
            value = int(value)
        else:
            value = str(value).strip()[:120]
        clean[key] = value
    merged = {**get_settings(), **clean}
    if not 1 <= merged["clip_count"] <= 12:
        raise ValueError("El número de clips debe estar entre 1 y 12.")
    if not 5 <= merged["min_seconds"] <= merged["max_seconds"] <= 180:
        raise ValueError("Duración de clips: mínimo 5 s, máximo 180 s y mínimo ≤ máximo.")
    return clean


def update_settings(values: dict) -> dict:
    clean = validate_settings(values)
    with _lock:
        DATA.mkdir(parents=True, exist_ok=True)
        settings = get_settings()
        settings.update(clean)
        SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings
