"""Render de cada clip vertical 1080×1920 con ffmpeg."""

from __future__ import annotations

import re
from pathlib import Path

from . import captions, framing, media
from .util import JobContext, UserError, run

W, H = 1080, 1920
CAPTION_CENTER = {"bottom": 1370, "middle": 1000, "top": 560}
AUDIOGRAM_CAPTION_CENTER = 1300


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def decide_layout(settings: dict, info: dict) -> str:
    if not info.get("has_video"):
        return "audiogram"
    choice = settings.get("layout", "auto")
    width, height = info.get("width", 0), info.get("height", 0)
    if width and height and width / height < 0.75:
        return "blur"  # ya es vertical: se ajusta sin recortar
    if choice in ("auto", "faces"):
        return "faces"
    return choice


def video_chain(layout: str, info: dict, plan: dict | None) -> str:
    """Filtros desde [0:v] hasta [base]."""
    width, height = info["width"], info["height"]
    if layout in ("crop", "faces"):
        if width / height >= 9 / 16:
            cw = plan["crop_w"] if plan else framing.crop_width(width, height)
            x = framing.x_expression(plan["keyframes"]) if plan else "(iw-ow)/2"
            crop = f"crop=w={cw}:h={_even(height)}:x='{x}':y=0"
        else:
            ch = _even(width * 16 / 9)
            crop = f"crop=w={_even(width)}:h={ch}:x=0:y=(ih-oh)/2"
        return f"[0:v]{crop},scale={W}:{H}:flags=lanczos,setsar=1,fps=30[base]"
    # Fondo desenfocado: el vídeo completo sobre una versión ampliada y difuminada.
    return (
        "[0:v]fps=30,split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,scale={W}:{H},eq=brightness=-0.07:saturation=1.15[bg];"
        f"[fgsrc]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
    )


def audiogram_chain(cover_index: int | None, card_index: int | None, header_index: int | None, duration: float) -> str:
    """Filtros para podcasts sin vídeo: fondo con la carátula, tarjeta, onda animada y cabecera."""
    parts = []
    if cover_index is not None:
        parts.append(
            f"[{cover_index}:v]fps=30,scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            "boxblur=40:5,eq=brightness=-0.32:saturation=1.25[bg]"
        )
    else:
        parts.append(f"color=c=0x15181e:s={W}x{H}:r=30:d={duration:.3f}[bg]")
    if card_index is not None:
        parts.append(f"[bg][{card_index}:v]overlay=(W-w)/2:240[b1]")
    else:
        parts.append("[bg]null[b1]")
    parts.append(
        "[0:a]asplit=2[aw][ao];"
        "[aw]showwaves=s=920x180:mode=cline:rate=30:colors=0xD5F878:scale=sqrt:draw=full,format=rgba[wave];"
        "[b1][wave]overlay=(W-w)/2:1600[b2]"
    )
    if header_index is not None:
        parts.append(f"[b2][{header_index}:v]overlay=0:40[base]")
    else:
        parts.append("[b2]null[base]")
    return ";".join(parts)


def render_clip(source: Path, info: dict, meta: dict, words: list[dict], clip: dict, number: int,
                settings: dict, ctx: JobContext, on_progress=None) -> dict:
    folder = ctx.folder
    source = Path(source).resolve()
    prefix = f"clip_{number:02d}"
    start, end = float(clip["start"]), float(clip["end"])
    duration = end - start
    if duration <= 0.5:
        raise UserError(f"El clip {number} es demasiado corto.")
    family = settings.get("caption_font", captions.DEFAULT_FONT)
    concat, blocks = captions.build_track(
        words, start, end, folder, prefix, settings.get("caption_style", "karaoke"),
        settings.get("caption_uppercase", True), family, settings.get("caption_color", "yellow"),
    )
    captions.write_srt(blocks, folder / f"{prefix}.srt")
    layout = decide_layout(settings, info)
    plan = None
    if layout == "faces":
        ctx.report(message=f"Clip {number}: buscando caras para encuadrar…")
        try:
            plan = framing.track(source, start, end, info, ctx)
        except Exception as exc:  # noqa: BLE001 - el encuadre es opcional
            ctx.log(f"Encuadre por caras no disponible: {exc}")
        layout = "faces" if plan else ("crop" if settings.get("layout") == "faces" else "blur")

    inputs = ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source), "-f", "concat", "-safe", "0", "-i", str(concat)]
    next_index = 2
    graph = []
    if layout == "audiogram":
        cover_index = card_index = header_index = None
        cover = folder / meta["cover"] if meta.get("cover") else None
        if cover and cover.is_file():
            inputs += ["-loop", "1", "-framerate", "30", "-t", f"{duration:.3f}", "-i", str(cover)]
            cover_index, next_index = next_index, next_index + 1
            card = folder / "cover_card.png"
            if card.is_file() or captions.render_cover(cover, card):
                inputs += ["-loop", "1", "-framerate", "30", "-t", f"{duration:.3f}", "-i", str(card)]
                card_index, next_index = next_index, next_index + 1
        header = folder / f"{prefix}_header.png"
        captions.render_header(meta.get("title", ""), meta.get("uploader", ""), header, family)
        inputs += ["-loop", "1", "-framerate", "30", "-t", f"{duration:.3f}", "-i", str(header)]
        header_index, next_index = next_index, next_index + 1
        graph.append(audiogram_chain(cover_index, card_index, header_index, duration))
        center = AUDIOGRAM_CAPTION_CENTER
        audio_label = "[ao]"
    else:
        graph.append(video_chain(layout, info, plan))
        center = CAPTION_CENTER.get(settings.get("caption_position", "bottom"), 1370)
        audio_label = "[0:a]"
    caption_y = int(center - captions.BAND_HEIGHT / 2)
    graph.append(f"[base][1:v]overlay=0:{caption_y}:format=auto[v1]")
    current = "[v1]"
    hook = folder / f"{prefix}_hook.png"
    if settings.get("hook_overlay", True) and layout != "audiogram" and captions.render_hook(clip.get("hook", ""), hook, family):
        inputs += ["-loop", "1", "-framerate", "30", "-t", "3.6", "-i", str(hook)]
        # Con recorte la cara ocupa la parte alta: el gancho va debajo, a la altura del pecho.
        landscape_blur = layout == "blur" and info.get("width", 0) > info.get("height", 1)
        hook_y = 170 if landscape_blur else (860 if settings.get("caption_position", "bottom") == "bottom" else 1480)
        graph.append(f"[{next_index}:v]format=rgba,fade=t=in:st=0:d=0.2:alpha=1,fade=t=out:st=3.0:d=0.5:alpha=1[hk];"
                     f"{current}[hk]overlay=0:{hook_y}:eof_action=pass[v2]")
        next_index += 1
        current = "[v2]"
    if settings.get("progress_bar", False):
        graph.append(f"color=c=0xD5F878:s={W}x10:r=30:d={duration:.3f}[pb];{current}[pb]overlay=x='-w+w*t/{duration:.3f}':y=H-10:eof_action=pass[v3]")
        current = "[v3]"
    graph.append(f"{current}format=yuv420p[vout]")
    audio_filters = "aresample=48000"
    if settings.get("loudnorm", True):
        audio_filters += ",loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
    graph.append(f"{audio_label}{audio_filters}[aout]")

    target = folder / f"{prefix}.mp4"
    temp = folder / f"{prefix}.tmp.mp4"
    args = [
        media.ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *inputs,
        "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
        *media.video_codec_args(), "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-t", f"{duration:.3f}", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(temp),
    ]

    def progress_line(line: str) -> None:
        match = re.match(r"out_time_(?:us|ms)=(\d+)", line)
        if match and on_progress:
            on_progress(min(1.0, int(match.group(1)) / 1_000_000 / duration))

    try:
        run(args, ctx=ctx, timeout=3 * 3600, cwd=folder, on_line=progress_line, what="ffmpeg (render)")
    except RuntimeError as exc:
        temp.unlink(missing_ok=True)
        raise UserError(f"ffmpeg no pudo renderizar el clip {number}.", "Revisa el registro técnico; si falta un códec, reinstala ffmpeg con brew reinstall ffmpeg.", str(exc)) from exc
    temp.replace(target)
    thumb = folder / f"{prefix}.jpg"
    try:
        run([media.ffmpeg(), "-nostdin", "-loglevel", "error", "-y", "-ss", f"{min(1.5, duration / 3):.2f}", "-i", str(target),
             "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", str(thumb)], timeout=60)
    except RuntimeError:
        pass
    frames = folder / f"{prefix}_frames"
    for png in frames.glob("*.png"):
        png.unlink(missing_ok=True)
    try:
        frames.rmdir()
    except OSError:
        pass
    concat.unlink(missing_ok=True)
    return {"file": target.name, "srt": f"{prefix}.srt", "thumb": thumb.name if thumb.is_file() else "", "layout": layout,
            "face_coverage": plan["coverage"] if plan else None}
