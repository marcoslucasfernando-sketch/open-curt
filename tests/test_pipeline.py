import unittest
from unittest.mock import patch

from pipeline import caption_events, choose_clips, validate_url


class PipelineTests(unittest.TestCase):
    def test_url_blocks_local_destinations(self):
        for url in ("file:///etc/passwd", "http://localhost:8000", "http://127.0.0.1/test", "https://user:pass@example.com/video"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        self.assertEqual(validate_url(" https://www.youtube.com/watch?v=abc "), "https://www.youtube.com/watch?v=abc")

    def test_caption_events_clip_and_split_text(self):
        segments = [{"start": 8, "end": 12, "text": "uno dos tres cuatro cinco seis siete ocho nueve"}]
        events = caption_events(segments, 10, 15)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["start"], 0)
        self.assertAlmostEqual(events[-1]["end"], 2)
        self.assertEqual(events[1]["text"], "ocho nueve")

    def test_luna_selection_rejects_overlap_and_out_of_bounds(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"clips":[{"start":10,"end":35,"title":"A","reason":"Momento A","score":82,"hook":"A"},{"start":20,"end":45,"title":"B","reason":"Solapa","score":90,"hook":"B"},{"start":100,"end":140,"title":"C","reason":"Fuera","score":75,"hook":"C"}]}'}]}]}
        with patch("pipeline.api_json", return_value=response):
            clips = choose_clips([{"start": 10, "end": 35, "text": "Un momento de ejemplo"}], 120, 3, 20, 50)
        self.assertEqual(len(clips), 1)
        self.assertEqual((clips[0]["start"], clips[0]["end"]), (10, 35))


if __name__ == "__main__":
    unittest.main()
