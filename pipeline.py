"""Download, transcribe, select and render short clips."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


MODEL = "gpt-6-luna"
API = "https://api.openai.com/v1"


def run(*args: str, timeout: int = 600, cwd: Path | None = None, env: dict | None = None) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-1800:].strip())
    return result.stdout


def check_dependencies() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe", "yt-dlp", "swift") if not shutil.which(name)]
    if missing:
        raise RuntimeError("Faltan programas: " + ", ".join(missing) + ". Instálalos con brew install ffmpeg yt-dlp")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Falta OPENAI_API_KEY. Consulta README.md para configurarla.")


def validate_url(value: str) -> str:
    url = value.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Introduce un enlace http o https válido.")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local") or host.startswith(("127.", "10.", "192.168.", "169.254.")):
        raise ValueError("Solo se admiten enlaces públicos.")
    if parsed.username or parsed.password:
        raise ValueError("El enlace no debe contener credenciales.")
    return url


def api_json(path: str, payload: dict, timeout: int = 180) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        API + path,
        data=data,
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:800]
        raise RuntimeError(f"OpenAI API ({exc.code}): {detail}") from exc


def transcribe_file(path: Path) -> dict:
    boundary = "----clips" + uuid.uuid4().hex
    fields = {"model": "whisper-1", "response_format": "verbose_json", "timestamp_granularities[]": "segment"}
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n".encode())
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        API + "/audio/transcriptions",
        data=bytes(body),
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:800]
        raise RuntimeError(f"Transcripción ({exc.code}): {detail}") from exc


def probe_duration(path: Path) -> float:
    data = json.loads(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)))
    return float(data["format"]["duration"])


def download(url: str, folder: Path) -> tuple[Path, str]:
    template = str(folder / "source.%(ext)s")
    raw = run("yt-dlp", "--no-playlist", "--no-progress", "--max-filesize", "3G", "--merge-output-format", "mp4", "-f", "bv*[height<=1080]+ba/b[height<=1080]/best", "-o", template, "--print", "after_move:filepath", "--print", "video:title", url, timeout=1800)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    files = sorted(folder.glob("source.*"))
    media = next((p for p in files if p.is_file() and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")), None)
    if not media:
        raise RuntimeError("La descarga terminó sin un archivo de vídeo legible.")
    title = next((line for line in lines if line != str(media) and not line.startswith(str(folder))), "Vídeo")
    return media, title


def transcribe(media: Path, folder: Path, duration: float, progress) -> list[dict]:
    segments = []
    chunk_length = 540
    for offset in range(0, int(duration) + 1, chunk_length):
        length = min(chunk_length, duration - offset)
        if length <= 0.1:
            break
        progress(f"Transcribiendo audio {offset // 60 + 1}–{int((offset + length) / 60) + 1} min…")
        audio = folder / f"audio_{offset:06d}.mp3"
        run("ffmpeg", "-nostdin", "-y", "-ss", str(offset), "-i", str(media), "-t", str(length), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio), timeout=600)
        result = transcribe_file(audio)
        audio.unlink(missing_ok=True)
        for item in result.get("segments", []):
            text = str(item.get("text", "")).strip()
            if text:
                segments.append({"start": round(offset + float(item["start"]), 2), "end": round(offset + float(item["end"]), 2), "text": text})
    if not segments:
        raise RuntimeError("No se detectó voz. Esta versión necesita contenido hablado para seleccionar clips.")
    return segments


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"clips": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "start": {"type": "number"}, "end": {"type": "number"},
            "title": {"type": "string"}, "reason": {"type": "string"},
            "score": {"type": "integer"}, "hook": {"type": "string"},
        },
        "required": ["start", "end", "title", "reason", "score", "hook"],
    }}}, "required": ["clips"],
}


def choose_clips(segments: list[dict], duration: float, count: int, min_seconds: int, max_seconds: int) -> list[dict]:
    transcript = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments)
    if len(transcript) > 160000:
        raise RuntimeError("La transcripción supera el límite de análisis de esta versión (160.000 caracteres).")
    prompt = (
        f"Selecciona exactamente {count} fragmentos independientes de {min_seconds} a {max_seconds} segundos de esta transcripción de un vídeo de {duration:.1f} segundos. "
        "Cada clip debe comenzar cerca del inicio de una frase, tener gancho, contexto suficiente y cierre natural. "
        "Prioriza ideas concretas, sorpresa, conflicto o información útil. Evita intros, saludos, silencios y redundancia. "
        "No inventes contenido. Evita solapamientos. score es una estimación editorial 0-100, no una predicción de viralidad. "
        "title y hook deben estar en el idioma predominante de la transcripción; reason explica por qué se eligió ese momento. "
        "Devuelve tiempos en segundos exactos basados en las marcas disponibles.\n\n" + transcript
    )
    response = api_json("/responses", {
        "model": MODEL,
        "reasoning": {"effort": "low"},
        "input": [{"role": "system", "content": "Eres un editor experto en clips cortos. Selecciona cortes verificables por transcripción."}, {"role": "user", "content": prompt}],
        "text": {"format": {"type": "json_schema", "name": "clip_selection", "strict": True, "schema": SCHEMA}},
    })
    output = "".join(part.get("text", "") for item in response.get("output", []) if item.get("type") == "message" for part in item.get("content", []) if part.get("type") == "output_text")
    if not output:
        raise RuntimeError("GPT-6 Luna no devolvió una selección de clips.")
    raw = json.loads(output).get("clips", [])
    selected = []
    for clip in raw:
        start, end = float(clip["start"]), float(clip["end"])
        if start < 0 or end > duration + 0.3 or end <= start:
            continue
        if not min_seconds - 3 <= end - start <= max_seconds + 3:
            continue
        if any(start < prev["end"] and end > prev["start"] for prev in selected):
            continue
        selected.append({
            "start": round(start, 2), "end": round(min(end, duration), 2),
            "title": str(clip["title"]).strip()[:90],
            "hook": str(clip["hook"]).strip()[:120],
            "reason": str(clip["reason"]).strip()[:300],
            "score": max(0, min(100, int(clip["score"]))),
        })
        if len(selected) == count:
            break
    if not selected:
        raise RuntimeError("La selección de Luna no contenía cortes válidos. Prueba otra duración.")
    return selected


def ass_time(seconds: float) -> str:
    centis = round(max(0, seconds) * 100)
    return f"{centis // 360000}:{centis // 6000 % 60:02d}:{centis // 100 % 60:02d}.{centis % 100:02d}"


def ass_escape(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().replace("\\", "").replace("{", "(").replace("}", ")")


def write_subtitles(path: Path, segments: list[dict], start: float, end: float) -> None:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,68,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,80,80,280,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for segment in segments:
        left = max(start, segment["start"])
        right = min(end, segment["end"])
        if right <= left:
            continue
        words = ass_escape(segment["text"]).split()
        # Split lengthy transcript segments into short readable caption cards.
        groups = [words[i:i + 7] for i in range(0, len(words), 7)]
        for index, group in enumerate(groups):
            a = left + (right - left) * index / len(groups)
            b = left + (right - left) * (index + 1) / len(groups)
            lines.append(f"Dialogue: 0,{ass_time(a-start)},{ass_time(b-start)},Default,,0,0,0,,{' '.join(group)}\n")
    path.write_text("".join(lines), encoding="utf-8")


def caption_events(segments: list[dict], start: float, end: float) -> list[dict]:
    events = []
    for segment in segments:
        left, right = max(start, segment["start"]), min(end, segment["end"])
        if right <= left:
            continue
        words = ass_escape(segment["text"]).split()
        groups = [words[i:i + 7] for i in range(0, len(words), 7)]
        for index, group in enumerate(groups):
            events.append({"start": left - start + (right - left) * index / len(groups), "end": left - start + (right - left) * (index + 1) / len(groups), "text": " ".join(group)})
    return events


def build_caption_track(folder: Path, segments: list[dict], start: float, end: float, index: int) -> Path:
    events = caption_events(segments, start, end)
    cards = []
    cursor = 0.0
    for event in events:
        if event["start"] > cursor + 0.03:
            cards.append({"duration": event["start"] - cursor, "text": ""})
        cards.append({"duration": max(0.04, event["end"] - max(cursor, event["start"])), "text": event["text"]})
        cursor = max(cursor, event["end"])
    if cursor < end - start:
        cards.append({"duration": end - start - cursor, "text": ""})
    if not cards:
        cards = [{"duration": end - start, "text": ""}]
    files = [{"text": card["text"], "file": f"caption_{index:02d}_{i:04d}.png"} for i, card in enumerate(cards)]
    json_path = folder / f"caption_{index:02d}.json"
    json_path.write_text(json.dumps(files, ensure_ascii=False), encoding="utf-8")
    cache = folder / ".swift-cache"
    cache.mkdir(exist_ok=True)
    env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(cache), SWIFT_MODULECACHE_PATH=str(cache))
    run("swift", str(Path(__file__).resolve().parent / "caption_images.swift"), str(json_path), str(folder), timeout=600, cwd=folder, env=env)
    concat = folder / f"caption_{index:02d}.ffconcat"
    lines = ["ffconcat version 1.0\n"]
    for card, item in zip(cards, files):
        lines += [f"file '{item['file']}'\n", f"duration {card['duration']:.4f}\n"]
    lines.append(f"file '{files[-1]['file']}'\n")
    concat.write_text("".join(lines), encoding="utf-8")
    return concat


def render_clip(media: Path, segments: list[dict], clip: dict, output: Path, index: int) -> Path:
    subtitle = output / f"clip_{index:02d}.ass"
    write_subtitles(subtitle, segments, clip["start"], clip["end"])
    caption_track = build_caption_track(output, segments, clip["start"], clip["end"], index)
    target = output / f"clip_{index:02d}.mp4"
    # The original frame remains visible, while a blurred enlargement fills 9:16.
    graph = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:10[blur];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[front];"
        "[blur][front]overlay=(W-w)/2:(H-h)/2[base];"
        "[base][1:v]overlay=0:1330:shortest=1:format=auto,format=yuv420p[v]"
    )
    run("ffmpeg", "-nostdin", "-y", "-ss", str(clip["start"]), "-i", str(media), "-f", "concat", "-safe", "0", "-i", str(caption_track), "-t", str(clip["end"] - clip["start"]),
        "-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(target), timeout=1800, cwd=output)
    return target


def process(url: str, folder: Path, count: int, min_seconds: int, max_seconds: int, progress) -> dict:
    check_dependencies()
    url = validate_url(url)
    folder.mkdir(parents=True, exist_ok=True)
    progress("Descargando vídeo…")
    media, title = download(url, folder)
    duration = probe_duration(media)
    if duration > 7200:
        raise RuntimeError("El vídeo supera el límite actual de 2 horas.")
    if duration < min_seconds:
        raise RuntimeError("El vídeo es más corto que la duración mínima pedida para un clip.")
    segments = transcribe(media, folder, duration, progress)
    (folder / "transcript.json").write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
    progress("GPT-6 Luna está eligiendo los mejores momentos…")
    clips = choose_clips(segments, duration, count, min_seconds, max_seconds)
    for index, clip in enumerate(clips, 1):
        progress(f"Renderizando clip {index} de {len(clips)}…")
        target = render_clip(media, segments, clip, folder, index)
        clip["file"] = target.name
    manifest = {"source_url": url, "source_title": title, "source_duration": duration, "selection_model": MODEL, "transcription_model": "whisper-1", "clips": clips}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
