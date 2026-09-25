"""Transcripción con marcas de tiempo por palabra.

Motores:
- openai: whisper-1 por API (rápido, requiere OPENAI_API_KEY, ~0,006 $/min).
- mlx: mlx-whisper en Mac con Apple Silicon (gratis, usa la GPU).
- faster: faster-whisper en CPU (gratis, funciona en cualquier ordenador).
"""

from __future__ import annotations

import bisect
import difflib
import functools
import json
import os
import platform
import re
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import media
from .config import env
from .util import JobContext, UserError

OPENAI_API = "https://api.openai.com/v1"
OPENAI_CHUNK_SECONDS = 1200  # 20 min a 48 kbps ≈ 7 MB (límite de la API: 25 MB)

MLX_REPOS = {
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


@functools.lru_cache(maxsize=1)
def has_mlx() -> bool:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


@functools.lru_cache(maxsize=1)
def has_faster_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def local_engine() -> str:
    if has_mlx():
        return "mlx"
    if has_faster_whisper():
        return "faster"
    return ""


def engines_status() -> dict:
    return {
        "openai": bool(env("OPENAI_API_KEY")),
        "mlx": has_mlx(),
        "faster": has_faster_whisper(),
    }


def pick_engine(settings: dict) -> tuple[str, str]:
    """Devuelve (motor, modelo)."""
    preference = settings.get("transcriber", "auto")
    local = local_engine()
    model = settings.get("local_model", "auto")
    if model == "auto":
        model = "large-v3-turbo" if local == "mlx" else "small"
    if preference == "openai" or (preference == "auto" and env("OPENAI_API_KEY")):
        if not env("OPENAI_API_KEY"):
            raise UserError("Has elegido transcribir con OpenAI pero falta OPENAI_API_KEY.",
                            "Añádela en Ajustes → Conexiones, o cambia la transcripción a «Local (gratis)».")
        return "openai", "whisper-1"
    if local:
        return local, model
    raise UserError(
        "No hay ningún motor de transcripción disponible.",
        "Opción gratis: abre iniciar.command (instala la transcripción local). "
        "Opción rápida: añade tu OPENAI_API_KEY en Ajustes → Conexiones.",
    )


def transcribe(source: Path, duration: float, ctx: JobContext, settings: dict) -> dict:
    engine, model = pick_engine(settings)
    language = (settings.get("language") or "").strip().lower() or None
    ctx.report(stage="transcribe", progress=0.0, message="Preparando el audio…")
    audio = ctx.folder / "audio_16k.mp3"
    if not audio.is_file() or audio.stat().st_size == 0:
        media.extract_audio(source, audio, ctx)
    if engine == "openai":
        result = _openai(audio, duration, ctx, language)
    elif engine == "mlx":
        result = _mlx(audio, duration, ctx, language, model)
    else:
        result = _faster(audio, duration, ctx, language, model)
    result = normalize(result)
    if not result["words"]:
        raise UserError("No se detectó voz en el audio.", "Corta Clips elige momentos a partir de lo que se dice. Prueba con un vídeo o podcast hablado.")
    result.update(engine=engine, model=model)
    return result


# ---------------------------------------------------------------- OpenAI

def _openai_request(path: Path, language: str | None) -> dict:
    boundary = "----cortaclips" + uuid.uuid4().hex
    fields = [
        ("model", "whisper-1"),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "word"),
        ("timestamp_granularities[]", "segment"),
        ("temperature", "0"),
    ]
    if language:
        fields.append(("language", language))
    body = bytearray()
    for name, value in fields:
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.mp3"\r\nContent-Type: audio/mpeg\r\n\r\n'.encode()
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    last_error = None
    for attempt in range(4):
        request = urllib.request.Request(
            OPENAI_API + "/audio/transcriptions", data=bytes(body),
            headers={"Authorization": "Bearer " + env("OPENAI_API_KEY"), "Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:800]
            if exc.code == 401:
                raise UserError("OpenAI rechazó la clave (401).", "Revisa OPENAI_API_KEY en Ajustes → Conexiones.", detail) from exc
            if exc.code == 429 and "quota" in detail.lower():
                raise UserError("Tu cuenta de OpenAI no tiene saldo (429).",
                                "Añade crédito en platform.openai.com o cambia la transcripción a «Local (gratis)» en Ajustes.", detail) from exc
            last_error = f"OpenAI transcripción ({exc.code}): {detail}"
            if exc.code not in (408, 409, 429, 500, 502, 503, 504):
                raise UserError("Error de OpenAI al transcribir.", detail[:300], detail) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = f"Conexión con OpenAI: {exc}"
        time.sleep(2 ** attempt * 2)
    raise UserError("No se pudo transcribir con OpenAI.", "Comprueba tu conexión o usa la transcripción local.", last_error or "")


def _openai(audio: Path, duration: float, ctx: JobContext, language: str | None) -> dict:
    points = [0.0, duration]
    if duration > OPENAI_CHUNK_SECONDS + 60:
        ctx.report(message="Buscando pausas para dividir el audio…")
        points = media.split_points(duration, OPENAI_CHUNK_SECONDS, media.silences(audio, ctx))
    pieces = list(zip(points[:-1], points[1:]))
    ctx.report(message=f"Transcribiendo con OpenAI Whisper ({len(pieces)} parte{'s' if len(pieces) > 1 else ''})…")

    def work(index: int, start: float, end: float) -> tuple[float, dict]:
        ctx.check()
        piece = ctx.folder / f"chunk_{index:03d}.mp3"
        if len(pieces) == 1:
            piece = audio
        else:
            media.extract_audio(audio, piece, ctx, start=start, length=end - start)
        try:
            return start, _openai_request(piece, language)
        finally:
            if piece != audio:
                piece.unlink(missing_ok=True)

    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(work, i, a, b) for i, (a, b) in enumerate(pieces)]
        for done, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            ctx.report(progress=done / len(pieces), message=f"Transcribiendo… {done}/{len(pieces)} partes listas")
    words, segments, detected = [], [], None
    for offset, data in sorted(results, key=lambda item: item[0]):
        detected = detected or data.get("language")
        for w in data.get("words", []) or []:
            words.append({"start": offset + float(w["start"]), "end": offset + float(w["end"]), "text": str(w.get("word", ""))})
        for s in data.get("segments", []) or []:
            segments.append({"start": offset + float(s["start"]), "end": offset + float(s["end"]), "text": str(s.get("text", ""))})
    return {"words": words, "segments": segments, "language": detected or language or ""}


# ---------------------------------------------------------------- Local

def _download_note(ctx: JobContext, name: str) -> None:
    ctx.report(message=f"Cargando el modelo {name} (la primera vez se descarga, puede tardar unos minutos)…")


def _faster(audio: Path, duration: float, ctx: JobContext, language: str | None, model_name: str) -> dict:
    from faster_whisper import WhisperModel

    _download_note(ctx, model_name)
    threads = max(1, min(8, os.cpu_count() or 4))
    try:
        model = WhisperModel(model_name, device="auto", compute_type="int8", cpu_threads=threads)
    except Exception as exc:  # noqa: BLE001
        raise UserError("No se pudo cargar el modelo de transcripción local.",
                        "Comprueba tu conexión (la primera vez se descarga) o elige otro modelo en Ajustes.", str(exc)) from exc
    ctx.report(message=f"Transcribiendo en tu ordenador (faster-whisper {model_name})…")
    segments_iter, info = model.transcribe(
        str(audio), language=language, word_timestamps=True, vad_filter=True,
        condition_on_previous_text=False, beam_size=5,
    )
    words, segments = [], []
    started = time.monotonic()
    for segment in segments_iter:
        ctx.check()
        segments.append({"start": segment.start, "end": segment.end, "text": segment.text})
        for w in segment.words or []:
            words.append({"start": w.start, "end": w.end, "text": w.word})
        fraction = float(min(0.999, segment.end / max(duration, 1)))
        elapsed = time.monotonic() - started
        eta = elapsed / fraction - elapsed if fraction > 0.02 else 0
        ctx.report(progress=fraction, message=f"Transcribiendo en local… {fraction:.0%}" + (f" · quedan ~{_minutes(eta)}" if eta else ""))
    return {"words": words, "segments": segments, "language": getattr(info, "language", language or "")}


def _mlx(audio: Path, duration: float, ctx: JobContext, language: str | None, model_name: str) -> dict:
    import mlx_whisper

    repo = MLX_REPOS.get(model_name, MLX_REPOS["large-v3-turbo"])
    _download_note(ctx, model_name)
    points = [0.0, duration]
    if duration > 900:
        points = media.split_points(duration, 600, media.silences(audio, ctx))
    pieces = list(zip(points[:-1], points[1:]))
    words, segments, detected = [], [], None
    started = time.monotonic()
    for index, (start, end) in enumerate(pieces):
        ctx.check()
        piece = audio if len(pieces) == 1 else media.extract_audio(audio, ctx.folder / f"chunk_{index:03d}.wav", ctx, start=start, length=end - start, codec="wav")
        try:
            data = mlx_whisper.transcribe(str(piece), path_or_hf_repo=repo, word_timestamps=True, language=language or detected,
                                          condition_on_previous_text=False, verbose=None)
        finally:
            if piece != audio:
                piece.unlink(missing_ok=True)
        detected = detected or data.get("language")
        for s in data.get("segments", []):
            segments.append({"start": start + float(s["start"]), "end": start + float(s["end"]), "text": str(s.get("text", ""))})
            for w in s.get("words", []) or []:
                words.append({"start": start + float(w["start"]), "end": start + float(w["end"]), "text": str(w.get("word", ""))})
        fraction = end / max(duration, 1)
        elapsed = time.monotonic() - started
        eta = elapsed / fraction - elapsed if fraction > 0 else 0
        ctx.report(progress=min(0.999, fraction), message=f"Transcribiendo con la GPU de tu Mac… {fraction:.0%} · quedan ~{_minutes(eta)}")
    return {"words": words, "segments": segments, "language": detected or language or ""}


def _minutes(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60} min {seconds % 60:02d} s" if seconds >= 60 else f"{seconds} s"


# ---------------------------------------------------------------- Normalización

def normalize(result: dict) -> dict:
    words = []
    last_end = 0.0
    for w in sorted(result.get("words", []), key=lambda item: item["start"]):
        text = " ".join(str(w.get("text", "")).split())
        if not text:
            continue
        start = max(float(w["start"]), last_end - 0.02, 0.0)
        end = max(float(w["end"]), start + 0.06)
        words.append({"start": round(start, 3), "end": round(end, 3), "text": text})
        last_end = end
    segments = [
        {"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2), "text": " ".join(str(s.get("text", "")).split())}
        for s in result.get("segments", []) if str(s.get("text", "")).strip()
    ]
    words = punctuate(words, segments)
    # Para la IA usamos frases completas: los segmentos de Whisper cortan frases por la mitad.
    if words and (_punctuation_ratio(words) >= 0.02 or not segments):
        segments = sentences_from_words(words)
    return {"words": words, "segments": segments, "language": str(result.get("language", "") or "")}


PUNCT_END = re.compile(r"[.!?…]$")


def _punctuation_ratio(words: list[dict]) -> float:
    return sum(1 for w in words if PUNCT_END.search(w["text"])) / max(1, len(words))


def _norm(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())


def punctuate(words: list[dict], segments: list[dict]) -> list[dict]:
    """whisper-1 devuelve palabras sin puntuación: la copiamos del texto de cada segmento."""
    if not words or not segments or _punctuation_ratio(words) >= 0.02:
        return words
    starts = [w["start"] for w in words]
    for segment in segments:
        tokens = segment["text"].split()
        a = bisect.bisect_left(starts, segment["start"] - 0.3)
        b = bisect.bisect_left(starts, segment["end"] + 0.05)
        index = list(range(a, b))
        if not index or not tokens:
            continue
        matcher = difflib.SequenceMatcher(a=[_norm(words[k]["text"]) for k in index], b=[_norm(t) for t in tokens], autojunk=False)
        for block in matcher.get_matching_blocks():
            for n in range(block.size):
                words[index[block.a + n]]["text"] = tokens[block.b + n]
    return words


def sentences_from_words(words: list[dict], max_words: int = 45, pause: float = 1.2) -> list[dict]:
    out, current = [], []
    for index, word in enumerate(words):
        current.append(word)
        next_gap = words[index + 1]["start"] - word["end"] if index + 1 < len(words) else 99
        if word["text"][-1:] in ".?!…" or next_gap > pause or len(current) >= max_words:
            out.append({"start": round(current[0]["start"], 2), "end": round(current[-1]["end"], 2), "text": " ".join(w["text"] for w in current)})
            current = []
    return out
