"""Pruebas de subtítulos: paginación, línea de tiempo y SRT."""

import re
import tempfile
import unittest
from pathlib import Path

from cortaclips import captions

try:
    import PIL  # noqa: F401

    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def words(text, start=0.0, step=0.5):
    return [{"start": start + i * step, "end": start + i * step + 0.4, "text": w} for i, w in enumerate(text.split())]


class CaptionTests(unittest.TestCase):
    def test_paginate_breaks_on_sentences_and_size(self):
        pages = captions.paginate(words("Hola. Esto es una frase bastante larga de verdad"), 3)
        self.assertEqual([len(p) for p in pages], [1, 3, 3, 2])

    def test_display_word(self):
        style = captions.STYLES["karaoke"]
        self.assertEqual(captions.display_word("increíble,", style, True), "INCREÍBLE")
        self.assertEqual(captions.display_word("¿qué?", style, True), "¿QUÉ?")
        self.assertEqual(captions.display_word("hola,", captions.STYLES["clean"], True), "hola,")

    @unittest.skipUnless(HAS_PIL, "Pillow no instalado")
    def test_track_covers_whole_clip(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            concat, blocks = captions.build_track(words("uno dos tres. cuatro cinco seis siete", start=10.2), 10.0, 16.0, folder, "clip_01", "karaoke")
            text = concat.read_text(encoding="utf-8")
            durations = [float(x) for x in re.findall(r"duration ([\d.]+)", text)]
            self.assertAlmostEqual(sum(durations), 6.0, places=2)
            for name in re.findall(r"file '([^']+)'", text):
                self.assertTrue((folder / name).is_file(), name)
            self.assertEqual(blocks[0]["text"], "uno dos tres.")
            srt = folder / "clip_01.srt"
            captions.write_srt(blocks, srt)
            self.assertTrue(srt.read_text(encoding="utf-8").startswith("1\n00:00:00,200 --> "))

    @unittest.skipUnless(HAS_PIL, "Pillow no instalado")
    def test_all_fonts_and_styles_render(self):
        with tempfile.TemporaryDirectory() as temp:
            for family in captions.FONTS:
                for style in ("karaoke", "box", "clean"):
                    path = Path(temp) / f"{family}_{style}.png"
                    captions.CaptionRenderer(style, True, family, "lime").render(["¿QUÉ", "HARÍAS", "TÚ?"], 1, path)
                    self.assertGreater(path.stat().st_size, 500)
            self.assertTrue(captions.render_hook("Un gancho que engancha", Path(temp) / "hook.png"))
            self.assertFalse(captions.render_hook("   ", Path(temp) / "none.png"))


if __name__ == "__main__":
    unittest.main()
