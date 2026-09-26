"""Pruebas de YouTube: OAuth con ID de cliente y secreto, varios canales y subida (APIs simuladas, sin red)."""

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
from cortaclips.social import oauth, youtube
from cortaclips.social.oauth import SocialError

ENV = {"YOUTUBE_CLIENT_ID": "123456-abc.apps.googleusercontent.com", "YOUTUBE_CLIENT_SECRET": "yt-secret"}
CLIP = {"number": 1, "start": 10.0, "end": 40.0, "title": "Un <gran> título", "caption": "Una descripción", "hashtags": ["uno", "dos"]}


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


def channel_login(channel_id: str, name: str):
    """Respuestas de Google para un inicio de sesión que elige el canal indicado."""
    return [
        (200, {"access_token": f"at-{channel_id}", "refresh_token": f"rt-{channel_id}", "expires_in": 3600}, {}),
        (200, {"items": [{"id": channel_id, "snippet": {"title": name, "thumbnails": {"default": {"url": f"http://img/{channel_id}"}}}}]}, {}),
    ]


class TempData(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        self.patches = [
            patch.object(config, "DATA", data),
            patch.object(config, "TOKENS_FILE", data / "tokens.json"),
            patch.object(config, "SETTINGS_FILE", data / "settings.json"),
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

    def login(self, channel_id: str, name: str) -> dict:
        fake = FakeHttp(channel_login(channel_id, name))
        with patch("cortaclips.social.youtube.http", fake):
            state = oauth.new_state("youtube", "verif", youtube.redirect_uri())
            account = social.complete({"state": state, "code": "c0de"})
        self.assertEqual(fake.calls[0][2]["form"]["code_verifier"], "verif")
        return account


class OAuthTests(TempData):
    def test_pkce_is_base64url_sha256(self):
        verifier, challenge = oauth.pkce_pair()
        digest = hashlib.sha256(verifier.encode()).digest()
        self.assertEqual(challenge, base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
        self.assertTrue(43 <= len(verifier) <= 128)

    def test_auth_url_lets_you_pick_account_and_channel(self):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(social.start("youtube")).query))
        self.assertEqual(query["client_id"], ENV["YOUTUBE_CLIENT_ID"])
        self.assertEqual(query["access_type"], "offline")
        self.assertIn("select_account", query["prompt"])
        self.assertIn("youtube.upload", query["scope"])
        self.assertEqual(query["redirect_uri"], f"http://127.0.0.1:{config.port()}/oauth/callback")

    def test_state_is_single_use_and_forgeries_fail(self):
        state = oauth.new_state("youtube", "v", youtube.redirect_uri())
        self.assertEqual(oauth.pop_state(state)["platform"], "youtube")
        with self.assertRaises(SocialError):
            oauth.pop_state(state)
        with self.assertRaises(SocialError):
            social.complete({"state": "8766.youtube.inventado", "code": "x"})

    def test_needs_client_id_and_secret(self):
        with patch.dict(os.environ, {"YOUTUBE_CLIENT_SECRET": ""}):
            with self.assertRaises(SocialError):
                social.start("youtube")

    def test_client_id_format(self):
        self.assertTrue(youtube.valid_client_id(ENV["YOUTUBE_CLIENT_ID"]))
        self.assertFalse(youtube.valid_client_id("mi-clave"))

    def test_cancel_is_explained(self):
        state = oauth.new_state("youtube", "v", youtube.redirect_uri())
        with self.assertRaises(SocialError) as caught:
            social.complete({"state": state, "error": "access_denied"})
        self.assertIn("cancelado", caught.exception.message)


class ChannelTests(TempData):
    def test_two_channels_same_account(self):
        self.assertEqual(self.login("UC_personal", "Marcos")["name"], "Marcos")
        self.login("UC_marca", "Mi Podcast")
        status = social.status()["youtube"]
        self.assertTrue(status["connected"])
        self.assertEqual({c["name"] for c in status["channels"]}, {"Marcos", "Mi Podcast"})
        self.assertEqual(status["default"], "UC_personal")  # el primero conectado queda por defecto
        social.set_default("UC_marca")
        self.assertEqual(social.default_channel(), "UC_marca")
        social.disconnect("UC_marca")
        self.assertEqual([c["id"] for c in social.channels()], ["UC_personal"])
        self.assertEqual(social.default_channel(), "UC_personal")

    def test_reconnecting_same_channel_does_not_duplicate(self):
        self.login("UC_1", "Canal")
        self.login("UC_1", "Canal renombrado")
        self.assertEqual([(c["id"], c["name"]) for c in social.channels()], [("UC_1", "Canal renombrado")])

    def test_old_single_channel_token_is_migrated(self):
        oauth.set_token("youtube", {"access_token": "a", "expires_at": time.time() + 3600, "account": {"id": "UC_viejo", "name": "Antiguo"}})
        self.assertEqual([c["id"] for c in social.channels()], ["UC_viejo"])
        self.assertIsNone(oauth.get_token("youtube"))

    def test_expired_token_is_refreshed_per_channel(self):
        oauth.set_token("youtube:UC_1", {"access_token": "viejo", "refresh_token": "rt", "expires_at": time.time() - 10, "account": {"id": "UC_1"}})
        fake = FakeHttp([(200, {"access_token": "nuevo", "expires_in": 3600}, {})])
        with patch("cortaclips.social.youtube.http", fake):
            self.assertEqual(social.valid_token("UC_1")["access_token"], "nuevo")
        self.assertEqual(oauth.get_token("youtube:UC_1")["access_token"], "nuevo")


class UploadTests(TempData):
    def test_resumable_upload_with_clean_metadata(self):
        fake = FakeHttp([(200, {}, {"Location": "https://upload.example/session"}), (200, {"id": "vid123", "status": {"privacyStatus": "unlisted"}}, {})])
        with patch("cortaclips.social.youtube.http", fake), patch("cortaclips.social.oauth.http", fake):
            result = youtube.publish({"access_token": "a"}, self.video, CLIP, {"youtube_privacy": "unlisted"}, lambda *_: None)
        self.assertEqual(result["url"], "https://youtube.com/shorts/vid123")
        metadata = fake.calls[0][2]["json_body"]
        self.assertNotIn("<", metadata["snippet"]["title"])
        self.assertIn("#Shorts", metadata["snippet"]["description"])
        self.assertEqual(metadata["status"]["privacyStatus"], "unlisted")

    def test_unverified_project_reports_private(self):
        fake = FakeHttp([(200, {}, {"Location": "https://u"}), (200, {"id": "v", "status": {"privacyStatus": "private"}}, {})])
        with patch("cortaclips.social.youtube.http", fake), patch("cortaclips.social.oauth.http", fake):
            result = youtube.publish({"access_token": "a"}, self.video, CLIP, {"youtube_privacy": "public"}, lambda *_: None)
        self.assertEqual(result["status"], "private")
        self.assertIn("auditado", result["message"])

    def test_tags_fit_youtube_limit(self):
        tags = youtube.fit_tags(["x" * 40] * 30)
        self.assertLessEqual(sum(len(t) + 1 for t in tags), 450)
        self.assertTrue(all(len(t) <= 30 for t in tags))


class PublisherTests(TempData):
    def test_upload_goes_to_the_chosen_channel(self):
        from cortaclips.pipeline import JobManager

        manager = JobManager(Path(self.temp.name) / "outputs")
        job, source = manager.create_upload("demo.mp4", {})
        (source.parent / "clip_01.mp4").write_bytes(b"\x00" * 10)
        manager._update(job["id"], status="done", clips=[{**CLIP, "file": "clip_01.mp4", "status": "ready", "publications": {}}])
        for channel, name in (("UC_a", "Canal A"), ("UC_b", "Canal B")):
            oauth.set_token(f"youtube:{channel}", {"access_token": f"tok-{channel}", "expires_at": time.time() + 3600, "account": {"id": channel, "name": name}})
        used = []

        def fake_publish(token, video, clip, options, progress):
            used.append((token["access_token"], options["youtube_privacy"]))
            return {"status": "published", "id": "v1", "url": "https://youtube.com/shorts/v1", "message": "ok"}

        publisher = social.Publisher(manager)
        with patch.object(youtube, "publish", side_effect=fake_publish):
            publisher.publish(job["id"], 1, "UC_b", {"youtube_privacy": "unlisted"})
            for _ in range(60):
                record = manager.get(job["id"])["clips"][0]["publications"].get("youtube:UC_b", {})
                if record.get("status") not in (None, "uploading"):
                    break
                time.sleep(0.05)
        self.assertEqual(used, [("tok-UC_b", "unlisted")])
        self.assertEqual(record["status"], "published")
        self.assertEqual(record["channel"], "Canal B")
        with self.assertRaises(SocialError):
            publisher.publish(job["id"], 1, "UC_desconocido", {})


if __name__ == "__main__":
    unittest.main()
