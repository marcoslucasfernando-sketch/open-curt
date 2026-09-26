"""Pruebas HTTP del servidor local: seguridad, rutas y subida de archivos."""

import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cortaclips import config, server, social
from cortaclips.pipeline import JobManager


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.patches = [
            patch.object(config, "OUTPUTS", root / "outputs"),
            patch.object(config, "DATA", root / "data"),
            patch.object(config, "SETTINGS_FILE", root / "data" / "settings.json"),
            patch.object(config, "TOKENS_FILE", root / "data" / "tokens.json"),
        ]
        for p in cls.patches:
            p.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True  # no esperar a conexiones keep-alive al cerrar
        cls.httpd.block_on_close = False
        cls.port = cls.httpd.server_address[1]
        cls.env = patch.dict(os.environ, {"CORTACLIPS_PORT": str(cls.port)})
        cls.env.start()
        server.MANAGER = JobManager(root / "outputs")
        server.PUBLISHER = social.Publisher(server.MANAGER)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env.stop()
        for p in reversed(cls.patches):
            p.stop()
        cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            return response.status, json.loads(data)
        except json.JSONDecodeError:
            return response.status, data

    def test_rejects_foreign_host_and_origin(self):
        self.assertEqual(self.request("GET", "/api/jobs", host="evil.example")[0], 403)
        self.assertEqual(self.request("POST", "/api/settings", {"clip_count": 3}, {"Origin": "https://evil.example"})[0], 403)

    def test_home_and_fonts(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Corta Clips", body)
        status, _ = self.request("GET", "/assets/fonts/BricolageGrotesque%5Bopsz,wdth,wght%5D.ttf")
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/assets/../app.py")[0], 404)

    def test_status_has_everything_the_ui_needs(self):
        with patch("cortaclips.brain.providers_status", return_value={"openai": {"logged_in": False}, "codex": {"installed": False, "logged_in": False}, "claude": {"installed": False, "logged_in": False}}):
            status, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        for key in ("ffmpeg", "ytdlp", "ai", "transcription", "social", "settings", "credentials", "fonts"):
            self.assertIn(key, body)
        self.assertNotIn("sk-", json.dumps(body["credentials"]))

    def test_settings_roundtrip_and_validation(self):
        status, body = self.request("POST", "/api/settings", {"caption_font": "anton", "caption_color": "cyan"})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["caption_font"], "anton")
        self.assertEqual(self.request("POST", "/api/settings", {"caption_color": "morado"})[0], 400)

    def test_invalid_job_requests(self):
        self.assertEqual(self.request("POST", "/api/jobs", {"url": "http://localhost/x"})[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", {"url": "https://example.com/v", "count": 99})[0], 400)
        self.assertEqual(self.request("GET", "/api/jobs/nope")[0], 404)

    def test_upload_creates_job_and_files_are_protected(self):
        with patch.object(server.MANAGER, "start_upload") as start:
            status, job = self.request("POST", "/api/upload", body=b"fake-audio", headers={"X-Filename": "mi%20podcast.mp3", "X-Options": "%7B%22count%22%3A2%7D"})
        self.assertEqual(status, 202)
        start.assert_called_once()
        self.assertEqual(job["options"]["count"], 2)
        self.assertTrue((config.OUTPUTS / job["id"] / "source.mp3").is_file())
        self.assertEqual(self.request("GET", f"/files/{job['id']}/source.mp3")[0], 404)  # el original no se sirve
        self.assertEqual(self.request("GET", f"/files/{job['id']}/..%2F..%2Fapp.py")[0], 404)
        self.assertEqual(self.request("POST", "/api/upload", body=b"x", headers={"X-Filename": "virus.exe"})[0], 400)

    def test_download_all_as_zip(self):
        job, source = server.MANAGER.create_upload("episodio.mp3", {})
        folder = source.parent
        for name in ("clip_01.mp4", "clip_01.srt", "clip_02.mp4"):
            (folder / name).write_bytes(b"datos " + name.encode())
        clips = [
            {"number": 1, "title": "¿Qué harías tú? 🚀", "hook": "Gancho", "caption": "Texto", "hashtags": ["uno"], "reason": "r", "score": 90,
             "start": 10, "end": 40, "file": "clip_01.mp4", "srt": "clip_01.srt", "status": "ready", "publications": {}},
            {"number": 2, "title": "Otro/clip: bueno", "hook": "", "caption": "", "hashtags": [], "reason": "", "score": 70,
             "start": 50, "end": 80, "file": "clip_02.mp4", "status": "ready", "publications": {}},
        ]
        server.MANAGER._update(job["id"], status="done", clips=clips, source={"title": "Mi Podcast: episodio 1"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("GET", f"/api/jobs/{job['id']}/zip", headers={"Host": f"127.0.0.1:{self.port}"})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertIn("attachment", response.getheader("Content-Disposition"))
        self.assertIn("Mi%20Podcast%20episodio%201%20-%20clips.zip", response.getheader("Content-Disposition"))
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn("01 - ¿Qué harías tú.mp4", names)
        self.assertIn("01 - ¿Qué harías tú.srt", names)
        self.assertIn("02 - Otro clip bueno.mp4", names)
        self.assertIn("textos-para-redes.txt", names)
        text = zipfile.ZipFile(io.BytesIO(data)).read("textos-para-redes.txt").decode()
        self.assertIn("#uno", text)
        empty, _ = server.MANAGER.create_upload("vacio.mp3", {})
        self.assertEqual(self.request("GET", f"/api/jobs/{empty['id']}/zip")[0], 400)

    def test_oauth_callback_with_unknown_state_is_rejected(self):
        status, body = self.request("GET", "/oauth/callback?state=8766.tiktok.fake&code=abc")
        self.assertEqual(status, 400)
        self.assertIn(b"No se pudo conectar", body)


if __name__ == "__main__":
    unittest.main()
