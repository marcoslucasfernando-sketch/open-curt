"""Pruebas de la lógica sin red: URLs, errores, transcripción, selección y encuadre."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cortaclips import brain, config, framing, media, sources, transcribe
from cortaclips.util import UserError


def words_from(text: str, start: float = 0.0, step: float = 0.4, gaps: dict | None = None) -> list[dict]:
    """Palabras con tiempos regulares; gaps={índice: segundos} añade silencios antes de una palabra."""
    out, t = [], start
    for index, token in enumerate(text.split()):
        t += (gaps or {}).get(index, 0.0)
        out.append({"start": round(t, 2), "end": round(t + step - 0.05, 2), "text": token})
        t += step
    return out


class UrlTests(unittest.TestCase):
    def test_blocks_local_and_credentials(self):
        for url in ("file:///etc/passwd", "http://localhost:8000", "http://127.0.0.1/x", "http://10.1.2.3/v",
                    "http://[::1]/v", "https://user:pass@example.com/v", "http://printer.local/x", "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(UserError):
                sources.validate_url(url)

    def test_accepts_public(self):
        self.assertEqual(sources.validate_url(" https://www.youtube.com/watch?v=abc "), "https://www.youtube.com/watch?v=abc")
        self.assertTrue(sources.is_spotify("https://open.spotify.com/episode/123"))

    def test_ytdlp_errors_are_explained(self):
        cases = {
            "ERROR: [youtube] x: Sign in to confirm you’re not a bot": "verificación",
            "ERROR: [generic] Unsupported URL: https://x": "no es compatible",
            "ERROR: This video is DRM protected": "DRM",
            "ERROR: HTTP Error 403: Forbidden": "yt-dlp",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIn(expected, sources.explain_ytdlp_error(text).message)

    def test_upload_extension(self):
        self.assertEqual(sources.upload_extension("Mi Podcast.MP3"), ".mp3")
        with self.assertRaises(UserError):
            sources.upload_extension("documento.pdf")


class TranscriptTests(unittest.TestCase):
    def test_punctuation_is_copied_to_words(self):
        words = words_from("hola a todos hoy hablamos de dinero")
        segments = [{"start": 0, "end": 1.2, "text": "Hola a todos."}, {"start": 1.2, "end": 3.0, "text": "Hoy hablamos de dinero."}]
        fixed = transcribe.punctuate([dict(w) for w in words], segments)
        self.assertEqual([w["text"] for w in fixed], ["Hola", "a", "todos.", "Hoy", "hablamos", "de", "dinero."])

    def test_normalize_builds_sentences(self):
        raw = {"words": words_from("Esto es uno. Esto es dos. Y tres"), "segments": [{"start": 0, "end": 5, "text": "Esto es uno. Esto es"}], "language": "es"}
        result = transcribe.normalize(raw)
        self.assertEqual([s["text"] for s in result["segments"]], ["Esto es uno.", "Esto es dos.", "Y tres"])

    def test_normalize_orders_and_repairs_words(self):
        result = transcribe.normalize({"words": [{"start": 2, "end": 1, "text": " b "}, {"start": 0, "end": 0.5, "text": "a"}, {"start": 3, "end": 3.2, "text": "  "}]})
        self.assertEqual([w["text"] for w in result["words"]], ["a", "b"])
        self.assertGreater(result["words"][1]["end"], result["words"][1]["start"])

    def test_split_points_prefer_silence(self):
        points = media.split_points(3000, 1200, [(1190.0, 1191.0), (2410.0, 2412.0)])
        self.assertEqual(points[0], 0.0)
        self.assertAlmostEqual(points[1], 1190.5)
        self.assertAlmostEqual(points[2], 2411.0)
        self.assertEqual(points[-1], 3000)

    def test_engine_choice(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            self.assertEqual(transcribe.pick_engine({"transcriber": "auto"})[0], "openai")
        with patch.dict(os.environ, {}, clear=True), patch.object(transcribe, "local_engine", return_value=""):
            with self.assertRaises(UserError):
                transcribe.pick_engine({"transcriber": "auto"})


class SelectionTests(unittest.TestCase):
    def setUp(self):
        text = ("Esto es el final de otra idea. Mi madre era maestra de matemáticas y me enseñó todo lo que sé sobre números. "
                "Fue ella quien me hizo amar la ciencia. Yo creo que eso lo cambió todo para mí de verdad.")
        # Un silencio largo antes de «Yo» imita el caso real que dejaba palabras colgando.
        self.words = words_from(text, gaps={34: 5.0})

    def test_snap_starts_and_ends_on_sentences(self):
        first = next(i for i, w in enumerate(self.words) if w["text"] == "Mi")
        science = next(i for i, w in enumerate(self.words) if w["text"] == "ciencia.")
        ai_start = self.words[first - 3]["start"]  # la IA empieza tres palabras antes
        ai_end = self.words[science + 1]["end"]    # y termina en «Yo», tras el silencio
        s, e, si, ei = brain.snap(self.words, ai_start, ai_end, 5, 60, 200)
        self.assertEqual(self.words[si]["text"], "Mi")
        self.assertEqual(self.words[ei]["text"], "ciencia.")
        self.assertLess(s, self.words[si]["start"] + 0.01)

    def test_snap_respects_max_duration(self):
        s, e, si, ei = brain.snap(self.words, 0, 200, 3, 8, 200)
        self.assertLessEqual(e - s, 8 + 1.5)

    def test_finalize_filters_and_ranks(self):
        raw = [
            {"start": 2, "end": 12, "title": "A", "hook": "h", "caption": "c", "hashtags": ["#uno", "dos!"], "reason": "r", "score": 60},
            {"start": 3, "end": 13, "title": "B (solapa)", "hook": "h", "caption": "c", "hashtags": [], "reason": "r", "score": 50},
            {"start": 500, "end": 520, "title": "Fuera", "hook": "h", "caption": "c", "hashtags": [], "reason": "r", "score": 99},
            {"start": 14, "end": 10, "title": "Al revés", "hook": "h", "caption": "c", "hashtags": [], "reason": "r", "score": 99},
            {"start": "x", "end": 3},
        ]
        clips = brain.finalize(raw, self.words, 40, 3, 5, 15)
        self.assertEqual([c["title"] for c in clips], ["A"])
        self.assertEqual(clips[0]["hashtags"], ["uno", "dos"])

    def test_windows_split_long_transcripts(self):
        lines = ["x" * 1000] * 800
        windows = brain._windows(lines, [])
        self.assertGreater(len(windows), 1)
        self.assertEqual(windows[0][0], 0)
        self.assertEqual(windows[-1][1], 800)

    def test_extract_json_variants(self):
        self.assertEqual(brain._extract_json('```json\n{"clips": []}\n```'), {"clips": []})
        self.assertEqual(brain._extract_json('Aquí tienes: {"clips": [1]} ¡listo!'), {"clips": [1]})

    def test_prompt_contains_rules_and_instructions(self):
        prompt = brain.build_prompt(["[0.0-2.0] Hola."], {"title": "Ep", "duration": 100, "has_video": False, "chapters": [{"start": 0, "title": "Intro"}]},
                                    3, 20, 40, "solo dinero", None)
        self.assertIn("solo dinero", prompt)
        self.assertIn("podcast", prompt)
        self.assertIn("Intro", prompt)

    def test_openai_request_uses_luna_and_schema(self):
        seen = {}

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.body

        def fake(request, timeout):
            seen["payload"] = json.loads(request.data)
            text = json.dumps({"clips": []})
            return Response(json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}).encode())

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), patch("cortaclips.brain.urllib.request.urlopen", side_effect=fake):
            result = brain.call_openai("s", "p", {"ai_model": "gpt-6-luna", "ai_effort": "medium"})
        self.assertEqual(result, {"clips": []})
        self.assertEqual(seen["payload"]["model"], "gpt-6-luna")
        self.assertEqual(seen["payload"]["reasoning"], {"effort": "medium"})
        self.assertTrue(seen["payload"]["text"]["format"]["strict"])

    def test_auto_provider_prefers_api_then_chatgpt(self):
        with patch.object(brain, "providers_status", return_value={"openai": {"logged_in": False}, "codex": {"logged_in": True}, "claude": {"logged_in": True}}):
            self.assertEqual(brain.resolve_provider({"ai_provider": "auto"}), "codex")
        with patch.object(brain, "providers_status", return_value={"openai": {"logged_in": False}, "codex": {"logged_in": False}, "claude": {"logged_in": False}}):
            with self.assertRaises(UserError):
                brain.resolve_provider({"ai_provider": "auto"})


class FramingTests(unittest.TestCase):
    def test_plan_ignores_jitter_and_follows_real_moves(self):
        samples = [(i / 3, [(0.30 + (0.01 if i % 2 else 0), 0.1, 0.9)]) for i in range(12)]
        samples += [(i / 3, [(0.70, 0.1, 0.9)]) for i in range(12, 24)]
        keyframes, coverage = framing.plan(samples, 0.3164)
        self.assertEqual(coverage, 1.0)
        self.assertEqual(len(keyframes), 2)
        self.assertAlmostEqual(keyframes[1][1], 0.70, places=2)

    def test_plan_reports_low_coverage(self):
        samples = [(i / 3, [] if i % 4 else [(0.5, 0.1, 0.9)]) for i in range(20)]
        _, coverage = framing.plan(samples, 0.3)
        self.assertLess(coverage, 0.3)

    def test_x_expression(self):
        self.assertEqual(framing.x_expression([(0, 10), (3, 20), (7, 30)]), "if(lt(t,3.000),10,if(lt(t,7.000),20,30))")
        self.assertEqual(framing.x_expression([]), "(iw-ow)/2")


class ConfigTests(unittest.TestCase):
    def test_save_env_keeps_other_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text("# comentario\nOPENAI_API_KEY=viejo\nOTRA=1\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                config.save_env({"OPENAI_API_KEY": "nuevo", "YOUTUBE_CLIENT_ID": "abc"}, path)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "nuevo")
            text = path.read_text(encoding="utf-8")
            self.assertIn("# comentario", text)
            self.assertIn("OPENAI_API_KEY=nuevo", text)
            self.assertIn("OTRA=1", text)
            self.assertIn("YOUTUBE_CLIENT_ID=abc", text)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_save_env_rejects_unknown_or_multiline(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                config.save_env({"PATH": "/tmp"}, Path(temp) / ".env")
            with self.assertRaises(ValueError):
                config.save_env({"OPENAI_API_KEY": "a\nb=c"}, Path(temp) / ".env")

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            config.validate_settings({"caption_style": "comic-sans"})
        with self.assertRaises(ValueError):
            config.validate_settings({"min_seconds": 90, "max_seconds": 30})
        self.assertEqual(config.validate_settings({"caption_font": "anton", "hook_overlay": 0}), {"caption_font": "anton", "hook_overlay": False})


if __name__ == "__main__":
    unittest.main()
