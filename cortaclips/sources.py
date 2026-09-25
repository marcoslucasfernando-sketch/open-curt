"""Obtención del material: enlaces (yt-dlp), Spotify y archivos locales."""

from __future__ import annotations

import functools
import ipaddress
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from . import media
from .util import JobContext, UserError, run, which


def validate_url(value: str) -> str:
    url = (value or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UserError("Introduce un enlace http o https válido.", "Copia la dirección completa del vídeo o episodio, empezando por https://")
    host = parsed.hostname.lower().rstrip(".")
    if parsed.username or parsed.password:
        raise UserError("El enlace no debe contener usuario ni contraseña.")
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
        raise UserError("Solo se admiten enlaces públicos.", "Para archivos de tu ordenador, arrástralos a la zona de subida.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast):
        raise UserError("Solo se admiten enlaces públicos.", "Para archivos de tu ordenador, arrástralos a la zona de subida.")
    return url


@functools.lru_cache(maxsize=1)
def ytdlp_command() -> tuple[str, ...]:
    try:
        import yt_dlp  # noqa: F401

        return (sys.executable, "-m", "yt_dlp")
    except ImportError:
        exe = which("yt-dlp")
        return (exe,) if exe else ()


def ytdlp_version() -> str:
    cmd = ytdlp_command()
    if not cmd:
        return ""
    try:
        return run([*cmd, "--version"], timeout=60).strip()
    except Exception:  # noqa: BLE001
        return ""


def js_runtime_args() -> list[str]:
    """YouTube exige un intérprete de JavaScript a yt-dlp. deno viene activado por defecto."""
    if which("deno"):
        return []
    for runtime in ("node", "bun"):
        path = which(runtime)
        if path:
            return ["--js-runtimes", f"{runtime}:{path}"]
    return []


def is_spotify(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "").endswith("spotify.com")


def spotify_title(url: str) -> str:
    endpoint = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe="")
    try:
        with urllib.request.urlopen(urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as response:
            return str(json.load(response).get("title", "")).strip()
    except Exception as exc:  # noqa: BLE001
        raise UserError(
            "Spotify protege el audio de sus podcasts y no se puede descargar.",
            "Busca el mismo episodio en YouTube, Apple Podcasts o el RSS del podcast y pega ese enlace, o sube el archivo.",
            str(exc),
        ) from exc


YTDLP_HINTS = [
    (r"confirm you.?re not a bot|Sign in to confirm|429|Too Many Requests",
     "YouTube está pidiendo verificación.",
     "En Ajustes → Descargas elige el navegador donde tienes YouTube abierto (Chrome o Safari) para usar sus cookies, y reintenta."),
    (r"DRM|protected by", "El contenido está protegido con DRM y no se puede descargar.",
     "Busca el episodio en YouTube o en el RSS del podcast, o sube el archivo directamente."),
    (r"Unsupported URL", "Ese enlace no es compatible.",
     "Usa el enlace directo del vídeo o episodio (YouTube, Apple Podcasts, RSS, Twitch, Vimeo, X…) o sube el archivo."),
    (r"Private video|members-only|Join this channel|This video is private|login required|requires authentication|age",
     "El vídeo es privado, para miembros o con restricción de edad.",
     "En Ajustes → Descargas elige tu navegador para usar tu sesión, o sube el archivo."),
    (r"Video unavailable|not available in your country|removed|has been terminated",
     "El vídeo no está disponible (eliminado o bloqueado en tu país).", "Prueba con otro enlace o sube el archivo."),
    (r"larger than max-filesize|File is larger", "El archivo supera el tamaño máximo (6 GB).", "Recorta el vídeo o súbelo en menor calidad."),
    (r"JavaScript runtime|js runtime|n challenge|nsig|Signature extraction|Requested format is not available|HTTP Error 403|Precondition check failed|Unable to extract",
     "yt-dlp no pudo leer YouTube (suele ser por una versión antigua).",
     "Cierra la app y vuelve a abrir iniciar.command: actualiza yt-dlp automáticamente. Si persiste, instala deno: brew install deno"),
    (r"timed out|Connection reset|Temporary failure|Name or service not known|nodename nor servname",
     "Fallo de conexión al descargar.", "Comprueba tu conexión a Internet y vuelve a intentarlo."),
]


def explain_ytdlp_error(text: str) -> UserError:
    for pattern, message, hint in YTDLP_HINTS:
        if re.search(pattern, text, re.IGNORECASE):
            return UserError(message, hint, text[-2500:])
    last = next((line for line in reversed(text.splitlines()) if "ERROR" in line), text.strip().splitlines()[-1] if text.strip() else "")
    return UserError("No se pudo descargar el enlace.", last.replace("ERROR:", "").strip()[:300] or "Prueba otro enlace o sube el archivo.", text[-2500:])


def download(url: str, ctx: JobContext, settings: dict) -> dict:
    cmd = ytdlp_command()
    if not cmd:
        raise UserError("Falta yt-dlp.", "Abre iniciar.command para instalar las dependencias, o ejecuta: brew install yt-dlp")
    folder = ctx.folder
    note = ""
    target = url
    if is_spotify(url):
        title = spotify_title(url)
        if not title:
            raise UserError("No pude leer el título del episodio de Spotify.", "Pega el enlace del episodio en YouTube o sube el archivo.")
        target = f"ytsearch1:{title}"
        note = f"Spotify no permite descargar; uso el primer resultado de YouTube para «{title}»."
        ctx.report(message=note)

    args = [
        *cmd, "--no-playlist", "--newline", "--no-colors", "--ignore-config",
        "--progress-template", "download:[dl] %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b/ba/b*",
        "--merge-output-format", "mp4",
        "-P", str(folder),
        "-o", "source.%(ext)s",
        "-o", "thumbnail:cover.%(ext)s",
        "-o", "infojson:info",
        "--write-thumbnail", "--convert-thumbnails", "jpg",
        "--write-info-json", "--no-write-comments",
        "--no-mtime", "--retries", "10", "--fragment-retries", "10", "--concurrent-fragments", "4",
        "--max-filesize", "6G",
        "--ffmpeg-location", str(Path(media.ffmpeg()).parent),
        *js_runtime_args(),
    ]
    if settings.get("cookies_browser"):
        args += ["--cookies-from-browser", settings["cookies_browser"]]
    args.append(target)

    part = {"n": 0}

    def on_line(line: str) -> None:
        if "[download] Destination:" in line:
            part["n"] += 1
        if line.startswith("[dl]"):
            try:
                percent, speed, eta = (x.strip() for x in line[4:].split("|"))
                value = float(percent.rstrip("%")) / 100
            except ValueError:
                return
            # Los vídeos de YouTube bajan en dos partes: primero la imagen (grande) y luego el sonido.
            overall = 0.85 * value if part["n"] <= 1 else 0.85 + 0.15 * value
            ctx.report(progress=min(0.99, overall), message=f"Descargando… {percent} · {speed} · quedan {eta}")

    ctx.report(stage="download", progress=0.0, message="Conectando con la fuente…")
    try:
        run(args, ctx=ctx, timeout=4 * 3600, on_line=on_line, what="yt-dlp")
    except RuntimeError as exc:
        raise explain_ytdlp_error(str(exc)) from exc

    source = find_source(folder)
    if not source:
        raise UserError("La descarga terminó sin un archivo de vídeo o audio.", "Prueba con otro enlace o sube el archivo.")
    info = {}
    info_file = next(folder.glob("*.info.json"), None)
    if info_file:
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
    meta = describe(source, info)
    meta["url"] = url
    meta["note"] = note
    return meta


def find_source(folder: Path) -> Path | None:
    candidates = [p for p in folder.glob("source.*") if p.is_file() and p.suffix.lower() in media.MEDIA_EXTENSIONS]
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None


def describe(source: Path, info: dict | None = None) -> dict:
    info = info or {}
    details = media.probe(source)
    if details["duration"] <= 0:
        raise UserError("No pude saber la duración del archivo.", "El archivo puede estar incompleto. Vuelve a descargarlo o súbelo de nuevo.")
    if not details["has_audio"]:
        raise UserError("El archivo no tiene sonido.", "Corta Clips necesita voz para elegir los mejores momentos.")
    cover = next((p for p in source.parent.glob("cover.*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")), None)
    if not cover and not details["has_video"]:
        cover = extract_cover(source)
    chapters = [
        {"start": float(c.get("start_time", 0)), "end": float(c.get("end_time", 0)), "title": str(c.get("title", ""))[:120]}
        for c in (info.get("chapters") or []) if c.get("title")
    ]
    return {
        "file": source.name,
        "title": str(info.get("title") or info.get("fulltitle") or details.get("tag_title") or source.stem)[:200],
        "uploader": str(info.get("uploader") or info.get("channel") or info.get("artist") or details.get("tag_artist") or "")[:120],
        "description": str(info.get("description") or "")[:1500],
        "chapters": chapters[:80],
        "cover": cover.name if cover else "",
        **details,
    }


def extract_cover(source: Path) -> Path | None:
    """Saca la carátula incrustada en MP3/M4A, si existe."""
    target = source.parent / "cover.jpg"
    try:
        run([media.ffmpeg(), "-nostdin", "-loglevel", "error", "-y", "-i", str(source), "-an", "-map", "0:v:0", "-frames:v", "1", str(target)], timeout=60)
        return target if target.is_file() and target.stat().st_size > 0 else None
    except Exception:  # noqa: BLE001
        return None


SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def upload_extension(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in media.MEDIA_EXTENSIONS:
        raise UserError("Formato de archivo no compatible.", "Sube un vídeo (MP4, MOV, MKV, WEBM) o audio (MP3, M4A, WAV, FLAC, OGG).")
    return suffix
