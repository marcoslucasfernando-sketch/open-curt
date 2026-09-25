"""Encuadre vertical inteligente: sigue las caras al pasar de 16:9 a 9:16.

Usa el detector YuNet de OpenCV (modelo incluido en assets/models, licencia MIT).
Si OpenCV no está instalado o no hay caras suficientes, devuelve None y el
render usa el modo de fondo desenfocado.
"""

from __future__ import annotations

import functools
import statistics
import subprocess
from pathlib import Path

from . import media
from .config import ASSETS
from .util import JobContext

MODEL = ASSETS / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 3
SAMPLE_WIDTH = 640


@functools.lru_cache(maxsize=1)
def available() -> bool:
    try:
        import cv2
        import numpy  # noqa: F401

        return hasattr(cv2, "FaceDetectorYN") and MODEL.is_file()
    except Exception:  # noqa: BLE001
        return False


def crop_width(width: int, height: int) -> int:
    return min(width, int(round(height * 9 / 16 / 2)) * 2)


def detect(source: Path, start: float, end: float, info: dict, ctx: JobContext | None = None) -> list[tuple[float, list[tuple[float, float, float]]]]:
    """Muestras (t, [(centro_x, ancho, puntuación)]) en fracciones del ancho del vídeo."""
    import cv2
    import numpy as np

    width, height = info["width"], info["height"]
    sw = SAMPLE_WIDTH
    sh = max(2, int(round(height * sw / width / 2)) * 2)
    proc = subprocess.Popen(
        [media.ffmpeg(), "-nostdin", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
         "-vf", f"fps={SAMPLE_FPS},scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if ctx:
        ctx.track(proc)
    detector = cv2.FaceDetectorYN.create(str(MODEL), "", (sw, sh), 0.72, 0.3, 20)
    size = sw * sh * 3
    samples = []
    index = 0
    try:
        while True:
            if ctx and ctx.cancel_event.is_set():
                proc.kill()
                break
            buffer = proc.stdout.read(size)
            if len(buffer) < size:
                break
            frame = np.frombuffer(buffer, np.uint8).reshape(sh, sw, 3)
            _, faces = detector.detect(frame)
            found = []
            if faces is not None:
                for face in faces:
                    x, _, w, _ = (float(v) for v in face[:4])
                    if w < sw * 0.035:
                        continue
                    found.append(((x + w / 2) / sw, w / sw, float(face[-1])))
            samples.append((index / SAMPLE_FPS, found))
            index += 1
    finally:
        proc.stdout.close()
        proc.wait()
        if ctx:
            ctx.untrack(proc)
    return samples


def plan(samples: list[tuple[float, list[tuple[float, float, float]]]], window: float) -> tuple[list[tuple[float, float]], float]:
    """Convierte detecciones en planos fijos: [(t, centro_x)] y cobertura (0-1).

    window: ancho del recorte como fracción del ancho del vídeo.
    Evita el temblor: solo cambia de plano si la cara se aleja de forma sostenida.
    """
    if not samples:
        return [], 0.0
    raw: list[float | None] = []
    for _, faces in samples:
        if not faces:
            raw.append(None)
            continue
        faces = sorted(faces, key=lambda f: f[1] * (0.5 + f[2]), reverse=True)
        left = min(f[0] - f[1] / 2 for f in faces[:3])
        right = max(f[0] + f[1] / 2 for f in faces[:3])
        # Si caben varias caras en el recorte, las centramos juntas; si no, la principal.
        raw.append((left + right) / 2 if len(faces) > 1 and right - left <= window * 0.92 else faces[0][0])
    coverage = sum(value is not None for value in raw) / len(raw)
    # Rellenamos huecos con el valor conocido más cercano.
    last = next((v for v in raw if v is not None), 0.5)
    filled = []
    for value in raw:
        last = value if value is not None else last
        filled.append(last)
    smooth = [statistics.median(filled[max(0, i - 2):i + 3]) for i in range(len(filled))]
    keyframes = [(0.0, smooth[0])]
    current = smooth[0]
    threshold = window * 0.22
    i = 0
    while i < len(smooth):
        if abs(smooth[i] - current) > threshold:
            ahead = smooth[i:i + 3]
            if len(ahead) >= 2 and all(abs(v - current) > threshold for v in ahead):
                current = statistics.median(smooth[i:i + 6])
                keyframes.append((samples[i][0], current))
                i += 2
                continue
        i += 1
    return keyframes, coverage


def track(source: Path, start: float, end: float, info: dict, ctx: JobContext | None = None) -> dict | None:
    """Plan de recorte en píxeles o None si no compensa (sin caras o vídeo ya vertical)."""
    width, height = info.get("width", 0), info.get("height", 0)
    if not (available() and width and height) or width / height < 0.75:
        return None
    cw = crop_width(width, height)
    samples = detect(source, start, end, info, ctx)
    keyframes, coverage = plan(samples, cw / width)
    if coverage < 0.3 or not keyframes:
        return None
    pixels = [(t, int(max(0, min(width - cw, round(x * width - cw / 2))))) for t, x in keyframes]
    return {"crop_w": cw, "keyframes": pixels, "coverage": round(coverage, 2)}


def x_expression(keyframes: list[tuple[float, int]]) -> str:
    """Expresión de ffmpeg por tramos para la coordenada x del recorte."""
    if not keyframes:
        return "(iw-ow)/2"
    expr = str(keyframes[-1][1])
    for (t_next, _), (_, x) in reversed(list(zip(keyframes[1:], keyframes[:-1]))):
        expr = f"if(lt(t,{t_next:.3f}),{x},{expr})"
    return expr
