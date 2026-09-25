"""Pruebas de OAuth y publicación con las APIs simuladas (sin red)."""

import base64
import hashlib
import os
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from cortaclips import config, social
from cortaclips.social import instagram, oauth, tiktok, youtube
from cortaclips.social.oauth import SocialError

ENV = {
    "YOUTUBE_CLIENT_ID": "yt-id", "YOUTUBE_CLIENT_SECRET": "yt-secret",
    "TIKTOK_CLIENT_KEY": "tt-key", "TIKTOK_CLIENT_SECRET": "tt-secret",
    "INSTAGRAM_APP_ID": "ig-id", "INSTAGRAM_APP_SECRET": "ig-secret",
    "OAUTH_HTTPS_REDIRECT": "https://ejemplo.netlify.app/oauth/callback",
}
CLIP = {"number": 1, "start": 10.0, "end": 40.0, "title": "Un título", "caption": "Una descripción", "hashtags": ["uno", "dos"]}


class FakeHttp:
    """Sustituye oauth.http registrando las llamadas y devolviendo respuestas en orden."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"Llamada inesperada: {method} {url}")
        return self.responses.pop(0)


class TempData(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        self.patches = [
            patch.object(config, "DATA", data),
            patch.object(config, "TOKENS_FILE", data / "tokens.json"),
            patch.dict(os.environ, ENV),
        ]
        for p in self.patches:
            p.start()
        self.video = data / "clip_01.mp4"
        self.video.write_bytes(b"\x00" * 2048)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()


class PkceStateTests(TempData):
    def test_pkce_formats(self):
        verifier, challenge = oauth.pkce_pair()
        digest = hashlib.sha256(verifier.encode()).digest()
        self.assertEqual(challenge, base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
        verifier, challenge = oauth.pkce_pair(hex_challenge=True)
        self.assertEqual(challenge, hashlib.sha256(verifier.encode()).hexdigest())
        self.assertTrue(43 <= len(verifier) <= 128)

    def test_state_is_single_use_and_carries_port(self):
        state = oauth.new_state("tiktok", "v", "http://127.0.0.1:8766/oauth/callback")
        self.assertTrue(state.startswith(f"{config.port()}.tiktok."))
        self.assertEqual(oauth.pop_state(state)["platform"], "tiktok")
        with self.assertRaises(SocialError):
            oauth.pop_state(state)

    def test_forged_state_rejected(self):
        with self.assertRaises(SocialError):
            social.complete({"state": "8766.youtube.inventado", "code": "x"})

    def test_auth_urls(self):
        url = social.start("tiktok")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(query["client_key"], "tt-key")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(len(query["code_challenge"]), 64)  # hexadecimal
        self.assertEqual(query["redirect_uri"], f"http://127.0.0.1:{config.port()}/oauth/callback")
        url = social.start("instagram")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(query["redirect_uri"], ENV["OAUTH_HTTPS_REDIRECT"])
        self.assertIn("instagram_business_content_publish", query["scope"])
        url = social.start("youtube")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(query["access_type"], "offline")
        self.assertIn("youtube.upload", query["scope"])

    def test_start_requires_configuration(self):
        with patch.dict(os.environ, {"TIKTOK_CLIENT_KEY": ""}):
            with self.assertRaises(SocialError):
                social.start("tiktok")


class YoutubeTests(TempData):
    def test_full_oauth_and_upload(self):
        fake = FakeHttp([
            (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}, {}),
            (200, {"items": [{"id": "UC1", "snippet": {"title": "Mi canal", "thumbnails": {"default": {"url": "http://a"}}}}]}, {}),
            (200, {}, {"Location": "https://upload.example/session"}),
            (200, {"id": "vid123", "status": {"privacyStatus": "public"}}, {}),
        ])
        with patch("cortaclips.social.youtube.http", fake), patch("cortaclips.social.oauth.http", fake):
            state = oauth.new_state("youtube", "verif", youtube.redirect_uri())
            self.assertEqual(social.complete({"state": state, "code": "c0de"}), "youtube")
            token = oauth.get_token("youtube")
            self.assertEqual(token["account"]["name"], "Mi canal")
            result = youtube.publish(token, self.video, CLIP, {"youtube_privacy": "public"}, lambda *_: None)
        self.assertEqual(result["url"], "https://youtube.com/shorts/vid123")
        self.assertEqual(fake.calls[0][2]["form"]["code_verifier"], "verif")
        metadata = fake.calls[2][2]["json_body"]
        self.assertIn("#Shorts", metadata["snippet"]["description"])
        self.assertEqual(metadata["status"]["privacyStatus"], "public")

    def test_unverified_project_reports_private(self):
        fake = FakeHttp([(200, {}, {"Location": "https://u"}), (200, {"id": "v", "status": {"privacyStatus": "private"}}, {})])
        with patch("cortaclips.social.youtube.http", fake), patch("cortaclips.social.oauth.http", fake):
            result = youtube.publish({"access_token": "a"}, self.video, CLIP, {"youtube_privacy": "public"}, lambda *_: None)
        self.assertEqual(result["status"], "private")
        self.assertIn("auditado", result["message"])


class TiktokTests(TempData):
    def test_chunk_plan(self):
        mb = 1024 * 1024
        self.assertEqual(tiktok.chunk_plan(3 * mb), (3 * mb, 1))
        self.assertEqual(tiktok.chunk_plan(40 * mb), (40 * mb, 1))
        size, count = tiktok.chunk_plan(95 * mb)
        self.assertEqual((size, count), (10 * mb, 9))
        self.assertLessEqual(95 * mb - size * (count - 1), 128 * mb)

    def test_draft_upload_flow(self):
        fake = FakeHttp([
            (200, {"data": {"publish_id": "p1", "upload_url": "https://up.tiktok/x"}, "error": {"code": "ok"}}, {}),
            (201, {}, {}),
            (200, {"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}}, {}),
        ])
        with patch("cortaclips.social.tiktok.http", fake), patch("cortaclips.social.oauth.http", fake):
            result = tiktok.publish({"access_token": "a"}, self.video, CLIP, {"tiktok_mode": "draft"}, lambda *_: None)
        self.assertEqual(result["status"], "draft")
        self.assertTrue(fake.calls[0][1].endswith("/v2/post/publish/inbox/video/init/"))
        self.assertEqual(fake.calls[1][2]["headers"]["Content-Range"], "bytes 0-2047/2048")

    def test_direct_post_falls_back_to_allowed_privacy(self):
        fake = FakeHttp([
            (200, {"data": {"privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 600}, "error": {"code": "ok"}}, {}),
            (200, {"data": {"publish_id": "p1", "upload_url": "https://up"}, "error": {"code": "ok"}}, {}),
            (201, {}, {}),
            (200, {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [777]}, "error": {"code": "ok"}}, {}),
        ])
        with patch("cortaclips.social.tiktok.http", fake), patch("cortaclips.social.oauth.http", fake):
            result = tiktok.publish({"access_token": "a"}, self.video, CLIP, {"tiktok_mode": "direct", "tiktok_privacy": "PUBLIC_TO_EVERYONE"}, lambda *_: None)
        self.assertEqual(fake.calls[1][2]["json_body"]["post_info"]["privacy_level"], "SELF_ONLY")
        self.assertIn("#uno", fake.calls[1][2]["json_body"]["post_info"]["title"])
        self.assertEqual(result["url"], "https://www.tiktok.com/video/777")

    def test_unaudited_error_has_hint(self):
        fake = FakeHttp([(403, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "x"}}, {})])
        with patch("cortaclips.social.tiktok.http", fake), patch("cortaclips.social.oauth.http", fake):
            with self.assertRaises(SocialError) as caught:
                tiktok.publish({"access_token": "a"}, self.video, CLIP, {"tiktok_mode": "draft"}, lambda *_: None)
        self.assertIn("Solo yo", caught.exception.hint)


class InstagramTests(TempData):
    def test_login_and_reel_publish(self):
        fake = FakeHttp([
            (200, {"data": [{"access_token": "short", "user_id": "17841", "permissions": "x"}]}, {}),
            (200, {"access_token": "long", "expires_in": 5184000}, {}),
            (200, {"user_id": "17841", "username": "micuenta"}, {}),
            (200, {"id": "container1"}, {}),
            (200, {"success": True}, {}),
            (200, {"status_code": "FINISHED"}, {}),
            (200, {"id": "media9"}, {}),
            (200, {"permalink": "https://instagram.com/reel/abc"}, {}),
        ])
        with patch("cortaclips.social.instagram.http", fake), patch("cortaclips.social.oauth.http", fake):
            state = oauth.new_state("instagram", "unused", instagram.redirect_uri())
            social.complete({"state": state, "code": "abc#_"})
            token = oauth.get_token("instagram")
            self.assertEqual(token["access_token"], "long")
            result = instagram.publish(token, self.video, CLIP, {}, lambda *_: None)
        self.assertEqual(fake.calls[0][2]["form"]["code"], "abc")
        self.assertEqual(fake.calls[3][2]["form"]["media_type"], "REELS")
        upload = fake.calls[4]
        self.assertTrue(upload[1].startswith("https://rupload.facebook.com/ig-api-upload/"))
        self.assertEqual(upload[2]["headers"]["file_size"], "2048")
        self.assertEqual(result["url"], "https://instagram.com/reel/abc")

    def test_refresh_only_after_a_day(self):
        token = {"access_token": "a", "obtained_at": time.time(), "expires_at": time.time() + 100}
        self.assertIs(instagram.refresh(token), token)


class PublisherTests(TempData):
    def test_publish_records_result_on_clip(self):
        from cortaclips.pipeline import JobManager

        manager = JobManager(Path(self.temp.name) / "outputs")
        job, target = manager.create_upload("demo.mp4", {})
        target.write_bytes(b"x")
        (target.parent / "clip_01.mp4").write_bytes(b"\x00" * 10)
        manager._update(job["id"], status="done", clips=[{**CLIP, "file": "clip_01.mp4", "status": "ready", "publications": {}}])
        oauth.set_token("youtube", {"access_token": "a", "expires_at": time.time() + 3600})
        publisher = social.Publisher(manager)
        with patch.object(youtube, "publish", return_value={"status": "published", "id": "v", "url": "https://youtube.com/shorts/v", "message": "ok"}):
            publisher.publish(job["id"], 1, ["youtube"], {})
            for _ in range(50):
                record = manager.get(job["id"])["clips"][0]["publications"]["youtube"]
                if record["status"] != "uploading":
                    break
                time.sleep(0.05)
        self.assertEqual(record["status"], "published")
        self.assertEqual(record["url"], "https://youtube.com/shorts/v")
        with self.assertRaises(SocialError):
            publisher.publish(job["id"], 1, ["tiktok"], {})  # no conectado


if __name__ == "__main__":
    unittest.main()
