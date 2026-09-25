import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from publisher import check_connected_accounts, publish


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class PublisherTests(unittest.TestCase):
    def test_requires_all_three_connections_before_upload(self):
        response = FakeResponse(json.dumps({"profile": {"social_accounts": {"youtube": {"username": "channel"}, "tiktok": None, "instagram": {"username": "ig"}}}}).encode())
        with patch.dict("os.environ", {"UPLOAD_POST_API_KEY": "test-key", "UPLOAD_POST_USER": "tester"}), patch("publisher.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "tiktok"):
                check_connected_accounts()

    def test_queue_request_contains_all_platforms_and_idempotency(self):
        seen = {}

        def fake_open(request, timeout):
            seen["url"] = request.full_url
            seen["headers"] = request.headers
            seen["body"] = request.data
            return FakeResponse(b'{"request_id":"remote-1"}')

        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "clip_01.mp4"
            video.write_bytes(b"fake-mp4")
            with patch.dict("os.environ", {"UPLOAD_POST_API_KEY": "test-key", "UPLOAD_POST_USER": "tester"}), patch("publisher.urllib.request.urlopen", side_effect=fake_open):
                result = publish(video, "Título", "Descripción", "job123", 1)
        self.assertEqual(result["client_request_id"], "cortaclips-job123-1")
        self.assertEqual(seen["headers"]["Idempotency-key"], "cortaclips-job123-1")
        self.assertEqual(seen["body"].count(b'name="platform[]"'), 3)
        self.assertIn(b"add_to_queue", seen["body"])
        self.assertIn(b"disable_inbox_fallback", seen["body"])


if __name__ == "__main__":
    unittest.main()
