"""Ayudas de ffmpeg/ffprobe: detección, análisis de archivos, audio y códecs."""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

from .util import JobContext, UserError, run, which

MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts", ".mts", ".3gp",
    ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".oga", ".wav", ".flac", ".mka", ".weba", ".aif", ".aiff",
}


def ffmpeg() -> str:
    path = which("ffmpeg")
    if not path:
        raise UserError("No encuentro ffmpeg.", "Instálalo con: brew install ffmpeg (o abre iniciar.command, que lo instala solo).")
    return path


def ffprobe() -> str:
    path = which("ffprobe")
    if not path:
        raise UserError("No encuentro ffprobe.", "Se instala junto a ffmpeg: brew install ffmpeg.")
    return path


@functools.lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    try:
        first = run([ffmpeg(), "-hide_banner", "-version"], timeout=20).splitlines()[0]
        return first.replace("ffmpeg version", "").split(" Copyright")[0].strip()
    except Exception:  # noqa: BLE001
        return ""


@functools.lru_cache(maxsize=1)
def encoders() -> frozenset[str]:
    try:
        out = run([ffmpeg(), "-hide_banner", "-encoders"], timeout=20)
    except Exception:  # noqa: BLE001
        return frozenset()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def video_codec_args() -> list[str]:
    available = encoders()
    if "libx264" in available or not available:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high", "-pix_fmt", "yuv420p"]
    if "h264_videotoolbox" in available:
        return ["-c:v", "h264_videotoolbox", "-b:v", "9M", "-maxrate", "12M", "-allow_sw", "1", "-pix_fmt", "yuv420p"]
    if "libopenh264" in available:
        return ["-c:v", "libopenh264", "-b:v", "8M", "-pix_fmt", "yuv420p"]
    return ["-c:v", "mpeg4", "-q:v", "3", "-pix_fmt", "yuv420p"]


def probe(path: Path) -> dict:
    """Devuelve duración, si tiene vídeo real (no carátula), resolución y fps."""
    try:
        raw = run([ffprobe(), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], timeout=120)
        data = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError) as exc:
        raise UserError("No se pudo leer el archivo de vídeo/audio.", "Puede estar dañado o en un formato raro. Prueba con MP4 o MP3.", str(exc)) from exc
    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
         and s.get("codec_name") not in ("mjpeg", "png", "bmp", "gif")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = 0.0
    for candidate in (data.get("format", {}).get("duration"), (video or {}).get("duration"), (audio or {}).get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    tags = {str(k).lower(): str(v) for k, v in (data.get("format", {}).get("tags") or {}).items()}
    info = {"duration": duration, "has_video": bool(video), "has_audio": bool(audio), "width": 0, "height": 0, "fps": 30.0, "rotation": 0,
            "tag_title": tags.get("title", "")[:200], "tag_artist": (tags.get("artist") or tags.get("album_artist") or tags.get("album") or "")[:120]}
    if video:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        rotation = 0
        for side in video.get("side_data_list", []) or []:
            if "rotation" in side:
                rotation = int(side["rotation"])
        rotation = rotation or int((video.get("tags") or {}).get("rotate", 0) or 0)
        if abs(rotation) % 180 == 90:
            width, height = height, width
        info.update(width=width, height=height, rotation=rotation)
        rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1"
        try:
            num, den = rate.split("/")
            fps = float(num) / float(den)
            if 5 <= fps <= 120:
                info["fps"] = fps
        except (ValueError, ZeroDivisionError):
            pass
    return info


def extract_audio(src: Path, dest: Path, ctx: JobContext | None = None, start: float = 0, length: float | None = None,
                  codec: str = "mp3") -> Path:
    args = [ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if start:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(src)]
    if length:
        args += ["-t", f"{length:.3f}"]
    args += ["-vn", "-ac", "1", "-ar", "16000"]
    if codec == "mp3":
        args += ["-c:a", "libmp3lame", "-b:a", "48k"] if "libmp3lame" in encoders() or not encoders() else ["-c:a", "aac", "-b:a", "48k"]
    else:
        args += ["-c:a", "pcm_s16le"]
    args.append(str(dest))
    run(args, ctx=ctx, timeout=3600, what="ffmpeg (audio)")
    return dest


def silences(src: Path, ctx: JobContext | None = None, noise: str = "-32dB", minimum: float = 0.35) -> list[tuple[float, float]]:
    """Detecta silencios para cortar el audio en fragmentos sin partir palabras."""
    lines: list[str] = []
    try:
        run([ffmpeg(), "-nostdin", "-hide_banner", "-nostats", "-i", str(src), "-vn", "-af", f"silencedetect=noise={noise}:d={minimum}", "-f", "null", "-"],
            ctx=ctx, timeout=3600, what="ffmpeg (silencios)", on_line=lines.append)
    except RuntimeError:
        pass
    result, start = [], None
    for line in lines:
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = float(m.group(1))
            continue
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            result.append((max(0.0, start), float(m.group(1))))
            start = None
    return result


def split_points(duration: float, chunk: float, quiet: list[tuple[float, float]], window: float = 45.0) -> list[float]:
    """Puntos de corte cada ~chunk segundos, desplazados al silencio más cercano."""
    points = [0.0]
    target = chunk
    while target < duration - 5:
        best = None
        for a, b in quiet:
            middle = (a + b) / 2
            if abs(middle - target) <= window and middle > points[-1] + 30:
                if best is None or abs(middle - target) < abs(best - target):
                    best = middle
        cut = best if best is not None else target
        points.append(round(cut, 3))
        target = cut + chunk
    points.append(duration)
    return points
