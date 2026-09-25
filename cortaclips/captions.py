"""Subtítulos animados palabra a palabra (estilo karaoke) renderizados con Pillow.

No depende de libass ni de drawtext, que faltan en el ffmpeg de Homebrew.
Cada estado del subtítulo es un PNG transparente; ffmpeg los superpone con el
demuxer concat usando la duración exacta de cada palabra.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from .config import ASSETS
from .util import UserError

WIDTH = 1080
BAND_HEIGHT = 560
MAX_TEXT_WIDTH = 930
ENDING = re.compile(r"[.!?…]$")

STYLES = {
    "karaoke": {"weight": 900, "size": 86, "fill": (255, 255, 255, 255), "active": (255, 225, 77, 255), "stroke": 10,
                "max_words": 3, "shadow": True, "upper": True, "box_active": False, "line_box": False},
    "box": {"weight": 800, "size": 80, "fill": (255, 255, 255, 255), "active": (17, 20, 12, 255), "stroke": 9,
            "max_words": 3, "shadow": True, "upper": True, "box_active": True, "active_bg": (213, 248, 120, 255), "line_box": False},
    "clean": {"weight": 700, "size": 62, "fill": (255, 255, 255, 255), "active": None, "stroke": 0,
              "max_words": 8, "shadow": False, "upper": False, "box_active": False, "line_box": True},
}


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont

        return Image, ImageDraw, ImageFilter, ImageFont
    except ImportError as exc:
        raise UserError("Falta la librería Pillow para dibujar los subtítulos.",
                        "Abre la app con iniciar.command: instala todo lo necesario automáticamente.") from exc


FONT_DIR = ASSETS / "fonts"

# Tipografías incluidas (todas con licencia libre SIL OFL, de Google Fonts).
# "axes" fija los ejes de las fuentes variables; "scale" compensa tamaños ópticos.
FONTS: dict[str, dict] = {
    "bricolage": {"label": "Bricolage", "file": "BricolageGrotesque[opsz,wdth,wght].ttf", "axes": {"opsz": 96, "wdth": 100}, "scale": 1.0},
    "poppins": {"label": "Poppins", "file": "Poppins-Black.ttf", "bold_file": "Poppins-ExtraBold.ttf", "scale": 0.92},
    "anton": {"label": "Anton", "file": "Anton-Regular.ttf", "scale": 1.12, "fixed": True},
    "montserrat": {"label": "Montserrat", "file": "Montserrat[wght].ttf", "scale": 0.95},
    "unbounded": {"label": "Unbounded", "file": "Unbounded[wght].ttf", "scale": 0.8},
    "bebas": {"label": "Bebas Neue", "file": "BebasNeue-Regular.ttf", "scale": 1.25, "fixed": True, "caps": True},
    "archivo": {"label": "Archivo Black", "file": "ArchivoBlack-Regular.ttf", "scale": 0.9, "fixed": True},
}
DEFAULT_FONT = "bricolage"
ACCENTS = {
    "yellow": (255, 225, 77, 255),
    "lime": (213, 248, 120, 255),
    "cyan": (94, 234, 255, 255),
    "pink": (255, 110, 199, 255),
    "orange": (255, 150, 50, 255),
}
UI_FONT = "DMSans[opsz,wght].ttf"

SYSTEM_FALLBACKS = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _apply_axes(loaded, weight: int, axes: dict) -> None:
    try:
        described = loaded.get_variation_axes()
    except Exception:  # noqa: BLE001 - fuente estática o FreeType sin soporte de variables
        return
    values = []
    for axis in described:
        name = axis.get("name", b"")
        name = name.decode(errors="ignore") if isinstance(name, bytes) else str(name)
        tag = {"Weight": "wght", "Width": "wdth", "Optical size": "opsz", "Optical Size": "opsz"}.get(name, name.lower()[:4])
        wanted = weight if tag == "wght" else axes.get(tag, axis.get("default", axis["minimum"]))
        values.append(max(axis["minimum"], min(axis["maximum"], wanted)))
    try:
        loaded.set_variation_by_axes(values)
    except Exception:  # noqa: BLE001
        pass


@functools.lru_cache(maxsize=64)
def font(size: int, weight: int = 800, family: str = DEFAULT_FONT):
    _, _, _, ImageFont = _pil()
    spec = FONTS.get(family) or FONTS[DEFAULT_FONT]
    scaled = max(8, int(round(size * spec.get("scale", 1.0))))
    filename = spec.get("bold_file") if weight < 850 and spec.get("bold_file") else spec["file"]
    candidates = [str(FONT_DIR / filename), str(FONT_DIR / FONTS[DEFAULT_FONT]["file"]), *SYSTEM_FALLBACKS]
    for path in candidates:
        if not Path(path).is_file():
            continue
        try:
            loaded = ImageFont.truetype(path, scaled if path.startswith(str(FONT_DIR / filename)) else size)
        except OSError:
            continue
        if "[" in Path(path).name:
            _apply_axes(loaded, weight, spec.get("axes", {}) if path.endswith(filename) else {})
        return loaded
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u20E3\U000E0000-\U000E007F]+")


def strip_emoji(text: str) -> str:
    """Las tipografías de subtítulos no tienen emojis: los quitamos para no dibujar cuadros vacíos."""
    return " ".join(EMOJI.sub("", text or "").split())


def display_word(text: str, style: dict, uppercase: bool, family: str = DEFAULT_FONT) -> str:
    word = strip_emoji(text)
    if (style["upper"] and uppercase) or FONTS.get(family, {}).get("caps"):
        word = word.upper()
        word = re.sub(r"[,.;:]+$", "", word)
    return word


def paginate(words: list[dict], max_words: int, pause: float = 0.55, max_chars: int = 26) -> list[list[dict]]:
    pages, current = [], []
    for index, word in enumerate(words):
        current.append(word)
        gap = words[index + 1]["start"] - word["end"] if index + 1 < len(words) else 99
        chars = sum(len(w["text"]) + 1 for w in current)
        if len(current) >= max_words or ENDING.search(word["text"]) or gap > pause or chars >= max_chars or word["text"].endswith(","):
            pages.append(current)
            current = []
    if current:
        pages.append(current)
    return pages


def _wrap(tokens: list[str], fnt, space: float) -> list[list[int]]:
    lines, current, width = [], [], 0.0
    for index, token in enumerate(tokens):
        w = fnt.getlength(token)
        extra = w if not current else width + space + w
        if current and extra > MAX_TEXT_WIDTH:
            lines.append(current)
            current, width = [index], w
        else:
            current.append(index)
            width = extra
    if current:
        lines.append(current)
    return lines


class CaptionRenderer:
    def __init__(self, style_name: str, uppercase: bool = True, family: str = DEFAULT_FONT, accent: str = "yellow"):
        self.style = dict(STYLES.get(style_name, STYLES["karaoke"]))
        color = ACCENTS.get(accent, ACCENTS["yellow"])
        if self.style["box_active"]:
            self.style["active_bg"] = color
        elif self.style["active"]:
            self.style["active"] = color
        self.uppercase = uppercase
        self.family = family if family in FONTS else DEFAULT_FONT
        self.font = font(self.style["size"], self.style["weight"], self.family)
        self._shadow_cache: dict = {}

    def _space(self, fnt) -> float:
        extra = self.style["stroke"] * 0.9 + (22 if self.style["box_active"] else 0)
        return fnt.getlength(" ") + extra

    def layout(self, tokens: list[str]):
        fnt = self.font
        space = self._space(fnt)
        lines = _wrap(tokens, fnt, space)
        # Si hay más de 2 líneas reducimos el tamaño para que quepa.
        size = self.style["size"]
        while len(lines) > 2 and size > 40:
            size -= 8
            fnt = font(size, self.style["weight"], self.family)
            space = self._space(fnt)
            lines = _wrap(tokens, fnt, space)
        ascent, descent = fnt.getmetrics()
        line_height = int((ascent + descent) * 1.12)
        total = line_height * len(lines)
        top = (BAND_HEIGHT - total) // 2
        boxes = {}
        for row, line in enumerate(lines):
            widths = [fnt.getlength(tokens[i]) for i in line]
            line_width = sum(widths) + space * (len(line) - 1)
            x = (WIDTH - line_width) / 2
            y = top + row * line_height
            for i, w in zip(line, widths):
                boxes[i] = (x, y, w, ascent + descent)
                x += w + space
        return fnt, boxes, lines, line_height

    def render(self, tokens: list[str], active: int | None, path: Path, page_key=None) -> None:
        Image, ImageDraw, ImageFilter, _ = _pil()
        style = self.style
        fnt, boxes, lines, line_height = self.layout(tokens)
        image = Image.new("RGBA", (WIDTH, BAND_HEIGHT), (0, 0, 0, 0))
        if style["shadow"]:
            key = (page_key, tuple(tokens))
            shadow = self._shadow_cache.get(key)
            if shadow is None:
                shadow = Image.new("RGBA", (WIDTH, BAND_HEIGHT), (0, 0, 0, 0))
                sd = ImageDraw.Draw(shadow)
                for i, (x, y, _, _) in boxes.items():
                    sd.text((x, y + 8), tokens[i], font=fnt, fill=(0, 0, 0, 170), stroke_width=style["stroke"] + 4, stroke_fill=(0, 0, 0, 170))
                shadow = shadow.filter(ImageFilter.GaussianBlur(10))
                self._shadow_cache = {key: shadow}
            image.alpha_composite(shadow)
        draw = ImageDraw.Draw(image)
        if style["line_box"]:
            for line in lines:
                x0 = min(boxes[i][0] for i in line) - 26
                x1 = max(boxes[i][0] + boxes[i][2] for i in line) + 26
                y0 = boxes[line[0]][1] - 10
                draw.rounded_rectangle((x0, y0, x1, y0 + line_height + 4), radius=18, fill=(0, 0, 0, 165))
        for i, (x, y, w, h) in boxes.items():
            fill = style["fill"]
            if active is not None and i == active and style["active"]:
                if style["box_active"]:
                    pad_x, pad_y = 16, 8
                    draw.rounded_rectangle((x - pad_x, y - pad_y + 6, x + w + pad_x, y + h + pad_y - 4), radius=16, fill=style["active_bg"])
                    fill = style["active"]
                    draw.text((x, y), tokens[i], font=fnt, fill=fill)
                    continue
                fill = style["active"]
            if style["stroke"]:
                draw.text((x, y), tokens[i], font=fnt, fill=fill, stroke_width=style["stroke"], stroke_fill=(0, 0, 0, 255))
            else:
                draw.text((x, y), tokens[i], font=fnt, fill=fill)
        image.save(path, compress_level=1)


def build_track(words: list[dict], clip_start: float, clip_end: float, folder: Path, prefix: str,
                style_name: str, uppercase: bool = True, family: str = DEFAULT_FONT, accent: str = "yellow") -> tuple[Path, list[dict]]:
    """Genera los PNG y el archivo .ffconcat. Devuelve (ruta ffconcat, bloques para SRT)."""
    Image, _, _, _ = _pil()
    duration = clip_end - clip_start
    local = [
        {"start": max(0.0, w["start"] - clip_start), "end": min(duration, w["end"] - clip_start), "text": w["text"]}
        for w in words if w["end"] > clip_start + 0.05 and w["start"] < clip_end - 0.05
    ]
    frames_dir = folder / f"{prefix}_frames"
    frames_dir.mkdir(exist_ok=True)
    for old in frames_dir.glob("*.png"):
        old.unlink()
    blank = frames_dir / "blank.png"
    Image.new("RGBA", (WIDTH, BAND_HEIGHT), (0, 0, 0, 0)).save(blank, compress_level=1)
    timeline: list[tuple[str, float]] = []
    subtitles: list[dict] = []
    cursor = 0.0

    def push(name: str, until: float) -> None:
        nonlocal cursor
        length = until - cursor
        if length <= 0.001:
            return
        if timeline and length < 1 / 30:
            prev_name, prev_len = timeline[-1]
            timeline[-1] = (prev_name, prev_len + length)
        else:
            timeline.append((name, length))
        cursor = until

    if style_name != "none" and local:
        renderer = CaptionRenderer(style_name, uppercase, family, accent)
        style = renderer.style
        pages = paginate(local, style["max_words"])
        for p, page in enumerate(pages):
            tokens = [display_word(w["text"], style, uppercase, renderer.family) for w in page]
            if not any(tokens):
                continue
            next_start = pages[p + 1][0]["start"] if p + 1 < len(pages) else duration
            page_start = max(cursor, page[0]["start"])
            page_end = min(next_start, page[-1]["end"] + 0.6, duration)
            if page_end <= page_start:
                continue
            push("blank.png", page_start)
            subtitles.append({"start": page_start, "end": page_end, "text": " ".join(w["text"].strip() for w in page)})
            if style["active"]:
                for k, word in enumerate(page):
                    name = f"p{p:04d}_{k:02d}.png"
                    renderer.render(tokens, k, frames_dir / name, page_key=p)
                    until = page[k + 1]["start"] if k + 1 < len(page) else page_end
                    push(name, max(cursor, min(until, page_end)))
            else:
                name = f"p{p:04d}.png"
                renderer.render(tokens, None, frames_dir / name, page_key=p)
                push(name, page_end)
    push("blank.png", duration)
    if not timeline:
        timeline.append(("blank.png", max(duration, 0.1)))
    concat = folder / f"{prefix}.ffconcat"
    lines = ["ffconcat version 1.0\n"]
    for name, length in timeline:
        lines += [f"file '{frames_dir.name}/{name}'\n", f"duration {length:.4f}\n"]
    lines.append(f"file '{frames_dir.name}/{timeline[-1][0]}'\n")
    concat.write_text("".join(lines), encoding="utf-8")
    return concat, subtitles


def write_srt(blocks: list[dict], path: Path) -> None:
    def stamp(value: float) -> str:
        ms = int(round(max(0.0, value) * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    out = []
    for index, block in enumerate(blocks, 1):
        out.append(f"{index}\n{stamp(block['start'])} --> {stamp(block['end'])}\n{block['text']}\n")
    path.write_text("\n".join(out), encoding="utf-8")


def render_hook(text: str, path: Path, family: str = DEFAULT_FONT, width: int = WIDTH) -> bool:
    """Tarjeta blanca con el gancho del clip (se muestra los primeros segundos)."""
    text = strip_emoji(text)
    if not text:
        return False
    Image, ImageDraw, _, _ = _pil()
    size = 66
    fnt = font(size, 800, family)
    words = text.split()
    while True:
        space = fnt.getlength(" ")
        lines, current, current_w = [], [], 0.0
        for word in words:
            w = fnt.getlength(word)
            if current and current_w + space + w > 840:
                lines.append(" ".join(current))
                current, current_w = [word], w
            else:
                current_w = w if not current else current_w + space + w
                current.append(word)
        if current:
            lines.append(" ".join(current))
        if len(lines) <= 3 or size <= 40:
            break
        size -= 6
        fnt = font(size, 800, family)
    ascent, descent = fnt.getmetrics()
    line_h = int((ascent + descent) * 1.08)
    box_h = line_h * len(lines) + 56
    box_w = max(fnt.getlength(line) for line in lines) + 80
    image = Image.new("RGBA", (width, int(box_h) + 40), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    x0 = (width - box_w) / 2
    draw.rounded_rectangle((x0 + 6, 26, x0 + box_w + 6, 26 + box_h), radius=28, fill=(0, 0, 0, 90))
    draw.rounded_rectangle((x0, 20, x0 + box_w, 20 + box_h), radius=28, fill=(255, 255, 255, 250))
    for row, line in enumerate(lines):
        lw = fnt.getlength(line)
        draw.text(((width - lw) / 2, 20 + 28 + row * line_h), line, font=fnt, fill=(12, 14, 18, 255))
    image.save(path, compress_level=1)
    return True


def render_header(title: str, subtitle: str, path: Path, family: str = DEFAULT_FONT) -> None:
    """Cabecera fija para audiogramas (podcasts sin vídeo)."""
    Image, ImageDraw, _, _ = _pil()
    image = Image.new("RGBA", (WIDTH, 260), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    big = font(58, 800, family)
    small = font(38, 600, family)
    title = strip_emoji(title)
    subtitle = strip_emoji(subtitle)
    while big.getlength(title) > 960 and len(title) > 10:
        title = title.rstrip("…")[:-1].rstrip() + "…"
    draw.text(((WIDTH - big.getlength(title)) / 2, 70), title, font=big, fill=(255, 255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0, 200))
    if subtitle:
        subtitle = subtitle[:60]
        draw.text(((WIDTH - small.getlength(subtitle)) / 2, 150), subtitle, font=small, fill=(213, 248, 120, 255))
    image.save(path, compress_level=1)


def render_cover(source: Path, path: Path, size: int = 760, radius: int = 44) -> bool:
    """Carátula cuadrada con esquinas redondeadas y sombra suave para audiogramas."""
    Image, ImageDraw, ImageFilter, _ = _pil()
    try:
        with Image.open(source) as original:
            cover = original.convert("RGB")
    except OSError:
        return False
    side = min(cover.size)
    left, top = (cover.width - side) // 2, (cover.height - side) // 2
    cover = cover.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    pad = 60
    canvas = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((pad, pad + 18, pad + size, pad + size + 18), radius=radius, fill=(0, 0, 0, 170))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(24)))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    canvas.paste(cover, (pad, pad), mask)
    canvas.save(path, compress_level=1)
    return True
