"""Servidor HTTP local (solo 127.0.0.1) con la API y la interfaz web."""

from __future__ import annotations

import html
import json
import mimetypes
import platform
import re
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, brain, captions, config, framing, media, social, sources, transcribe
from .pipeline import JobManager
from .social.oauth import SocialError
from .util import UserError, which

MANAGER: JobManager | None = None
PUBLISHER: social.Publisher | None = None
MAX_UPLOAD = 8 * 1024 ** 3
FILE_PATTERN = re.compile(r"^(clip_\d{2}\.(mp4|srt|jpg)|cover\.(jpg|png|webp)|job\.log|transcript\.json|source\.json)$")
ASSET_PATTERN = re.compile(r"^fonts/[A-Za-z0-9_\-\[\],.]+\.ttf$")


def system_status() -> dict:
    settings = config.get_settings()
    try:
        ffmpeg_ok = bool(media.ffmpeg()) and bool(media.ffprobe())
    except UserError:
        ffmpeg_ok = False
    providers = brain.providers_status()
    try:
        active = brain.resolve_provider(settings)
        ai_label = brain.provider_label(active, settings)
    except UserError:
        active, ai_label = "", ""
    try:
        engine, model = transcribe.pick_engine(settings)
    except UserError:
        engine, model = "", ""
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.machine()}",
        "ffmpeg": {"ok": ffmpeg_ok, "version": media.ffmpeg_version() if ffmpeg_ok else "", "x264": "libx264" in media.encoders() if ffmpeg_ok else False},
        "ytdlp": {"ok": bool(sources.ytdlp_command()), "js_runtime": bool(sources.js_runtime_args()) or bool(which("deno"))},
        "pillow": _has_module("PIL"),
        "faces": framing.available(),
        "ai": {"providers": providers, "active": active, "label": ai_label},
        "transcription": {"engines": transcribe.engines_status(), "active": engine, "model": model},
        "social": social.status(),
        "settings": settings,
        "credentials": config.credential_flags(),
        "fonts": [{"id": key, "label": value["label"], "file": value["file"]} for key, value in captions.FONTS.items()],
        "https_redirect": config.https_redirect(),
    }


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def error_body(exc: Exception) -> dict:
    if isinstance(exc, (UserError, SocialError)):
        return {"error": exc.message, "hint": exc.hint, "detail": (exc.detail or "")[-2000:]}
    return {"error": str(exc) or exc.__class__.__name__}


OAUTH_PAGE = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Corta Clips · {title}</title><style>body{{font-family:system-ui,sans-serif;background:#0e1013;color:#f4f2ec;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{max-width:460px;padding:32px;border:1px solid #2d3238;border-radius:20px;background:#171a1f;text-align:center}}h1{{font-size:24px}}p{{color:#aab0b8;line-height:1.5}}
a{{display:inline-block;margin-top:12px;background:#d5f878;color:#141a0a;padding:12px 20px;border-radius:12px;font-weight:700;text-decoration:none}}</style>
<main><h1>{title}</h1><p>{body}</p><a href="/">Volver a Corta Clips</a></main>
<script>try{{window.opener&&window.opener.postMessage({{cortaclips:'oauth',ok:{ok}}},'*');setTimeout(()=>window.close(),{close})}}catch(e){{}}</script></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = f"CortaClips/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        try:
            line = fmt % args
        except (TypeError, ValueError):
            line = fmt + " " + " ".join(str(a) for a in args)
        # El sondeo de progreso genera mucho ruido: solo registramos errores de esas rutas.
        if ("/api/jobs/" in line or "/api/status" in line) and '" 200 ' in line + " ":
            return
        sys.stderr.write(f"{self.address_string()} - {line}\n")

    # ------------------------------------------------------------ utilidades

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        allowed = {f"127.0.0.1:{config.port()}", f"localhost:{config.port()}"}
        return host in allowed

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin in (None, "null", f"http://127.0.0.1:{config.port()}", f"http://localhost:{config.port()}")

    def send_json(self, status: int, body) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, status: int, text: str) -> None:
        payload = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_json(self, limit: int = 200_000) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise UserError("Solicitud demasiado grande.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise UserError("JSON inválido.") from exc
        if not isinstance(data, dict):
            raise UserError("JSON inválido.")
        return data

    def send_file(self, path: Path, cache: bool = False, download_name: str = "") -> None:
        size = path.stat().st_size
        requested = self.headers.get("Range", "")
        start, end = 0, size - 1
        if requested.startswith("bytes="):
            try:
                left, right = requested[6:].split(",")[0].split("-", 1)
                if left:
                    start = int(left)
                    end = min(size - 1, int(right)) if right else size - 1
                else:
                    start = max(0, size - int(right))
                if start > end or start >= size:
                    raise ValueError
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if requested else 200)
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in (".log", ".srt"):
            kind = "text/plain; charset=utf-8"
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-cache")
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            fallback = download_name.encode("ascii", "ignore").decode().replace('"', "") or "descarga.zip"
            self.send_header("Content-Disposition", f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{urllib.parse.quote(download_name)}")
        elif "download" in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query):
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                try:
                    self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(block)

    # ------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: C901 - enrutado explícito y legible
        if not self._host_ok():
            self.send_json(403, {"error": "Host no permitido."})
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query))
        try:
            if path in ("/", "/index.html"):
                self.send_file(config.WEB / "index.html")
            elif path.startswith("/assets/") and ASSET_PATTERN.match(urllib.parse.unquote(path[8:])) and ".." not in path:
                target = config.ASSETS / urllib.parse.unquote(path[8:])
                if target.is_file():
                    self.send_file(target, cache=True)
                else:
                    self.send_json(404, {"error": "No encontrado."})
            elif path == "/api/status":
                self.send_json(200, system_status())
            elif path == "/api/jobs":
                self.send_json(200, {"jobs": MANAGER.summaries()})
            elif path.startswith("/api/jobs/") and path.endswith("/zip") and len(path.split("/")) == 5:
                archive, name = MANAGER.build_zip(path.split("/")[3])
                self.send_file(archive, download_name=name)
            elif path.startswith("/api/jobs/"):
                job = MANAGER.get(path.split("/")[3])
                self.send_json(200, job) if job else self.send_json(404, {"error": "Trabajo no encontrado."})
            elif path.startswith("/files/"):
                parts = path.split("/")
                if len(parts) != 4 or not MANAGER.get(parts[2]) or not FILE_PATTERN.match(parts[3]):
                    self.send_json(404, {"error": "Archivo no encontrado."})
                    return
                target = config.OUTPUTS / parts[2] / parts[3]
                self.send_file(target) if target.is_file() else self.send_json(404, {"error": "Archivo no encontrado."})
            elif path.startswith("/oauth/start/"):
                self.redirect(social.start(path.split("/")[3]))
            elif path.rstrip("/") == "/oauth/callback":
                self.oauth_finish(query)
            else:
                self.send_json(404, {"error": "Ruta no encontrada."})
        except (UserError, SocialError) as exc:
            if path.startswith("/oauth/"):
                self.send_html(400, OAUTH_PAGE.format(title="No se pudo conectar", body=html.escape(exc.message + " " + exc.hint), ok="false", close=600000))
            else:
                self.send_json(400, error_body(exc))

    def oauth_finish(self, query: dict) -> None:
        try:
            platform_name = social.complete(query)
            label = social.PLATFORMS[platform_name].LABEL
            self.send_html(200, OAUTH_PAGE.format(title=f"{label} conectado ✓", body="Ya puedes cerrar esta pestaña y volver a Corta Clips.", ok="true", close=1500))
        except (SocialError, UserError) as exc:
            self.send_html(400, OAUTH_PAGE.format(title="No se pudo conectar", body=html.escape(f"{exc.message} {exc.hint}"), ok="false", close=600000))

    # ------------------------------------------------------------ POST

    def do_POST(self) -> None:  # noqa: C901
        if not self._host_ok() or not self._origin_ok():
            self.close_connection = True  # el cuerpo no se ha leído: no reutilizamos la conexión
            self.send_json(403, {"error": "Origen no permitido."})
            return
        path = urllib.parse.urlparse(self.path).path
        parts = path.strip("/").split("/")
        try:
            if path == "/api/jobs":
                body = self.read_json()
                job = MANAGER.create_from_url(str(body.get("url", "")), self.job_options(body))
                self.send_json(202, job)
            elif path == "/api/upload":
                self.upload()
            elif path == "/api/settings":
                self.send_json(200, {"settings": config.update_settings(self.read_json())})
            elif path == "/api/credentials":
                values = {k: v for k, v in self.read_json().items() if k in config.ENV_KEYS}
                config.save_env(values)
                brain.invalidate_status()
                transcribe.has_mlx.cache_clear()
                self.send_json(200, {"credentials": config.credential_flags()})
            elif path == "/api/ai/login":
                self.send_json(200, brain.start_login(str(self.read_json().get("provider", ""))))
            elif path == "/api/ai/test":
                provider = str(self.read_json().get("provider", ""))
                if provider not in brain.CALLERS:
                    raise UserError("Proveedor desconocido.")
                brain.invalidate_status()
                self.send_json(200, {"ok": True, "label": brain.test_provider(provider, config.get_settings())})
            elif path == "/api/ai/refresh":
                brain.invalidate_status()
                self.send_json(200, {"ai": brain.providers_status()})
            elif path == "/oauth/complete":
                # Plan B: pegar la URL de retorno si la redirección automática no funcionó.
                url = str(self.read_json().get("url", ""))
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
                platform_name = social.complete(query)
                self.send_json(200, {"platform": platform_name})
            elif len(parts) == 4 and parts[:2] == ["api", "social"] and parts[3] == "disconnect":
                social.disconnect(parts[2])
                self.send_json(200, {"ok": True})
            elif len(parts) >= 4 and parts[:2] == ["api", "jobs"]:
                self.job_action(parts[2], parts[3:])
            else:
                self.send_json(404, {"error": "Ruta no encontrada."})
        except (UserError, SocialError) as exc:
            self.close_connection = True
            self.send_json(400, error_body(exc))
        except (ValueError, KeyError, TypeError) as exc:
            self.close_connection = True
            self.send_json(400, {"error": f"Petición no válida: {exc}"})

    def job_options(self, body: dict) -> dict:
        settings = config.get_settings()
        options = {
            "count": int(body.get("count", settings["clip_count"])),
            "min_seconds": int(body.get("min_seconds", settings["min_seconds"])),
            "max_seconds": int(body.get("max_seconds", settings["max_seconds"])),
            "instructions": str(body.get("instructions", ""))[:2000],
        }
        if not 1 <= options["count"] <= 12:
            raise UserError("Elige entre 1 y 12 clips.")
        if not 5 <= options["min_seconds"] <= options["max_seconds"] <= 180:
            raise UserError("Duración: mínimo 5 s, máximo 180 s, y el mínimo no puede superar al máximo.")
        overrides = {k: body[k] for k in ("layout", "caption_style", "caption_font", "caption_color", "caption_position", "language", "hook_overlay") if k in body}
        options.update(config.validate_settings(overrides))
        return options

    def upload(self) -> None:
        self.close_connection = True  # si algo falla antes de leer el cuerpo, no reutilizamos la conexión
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            raise UserError("El archivo está vacío o supera 8 GB.")
        name = urllib.parse.unquote(self.headers.get("X-Filename", "video.mp4"))
        try:
            options = self.job_options(json.loads(urllib.parse.unquote(self.headers.get("X-Options", "{}")) or "{}"))
        except json.JSONDecodeError as exc:
            raise UserError("Opciones inválidas.") from exc
        job, target = MANAGER.create_upload(name, options)
        remaining = length
        try:
            with target.open("wb") as handle:
                while remaining > 0:
                    block = self.rfile.read(min(4 * 1024 * 1024, remaining))
                    if not block:
                        break
                    handle.write(block)
                    remaining -= len(block)
        except OSError as exc:
            MANAGER.delete(job["id"])
            raise UserError("No se pudo guardar el archivo.", "¿Queda espacio en el disco?", str(exc)) from exc
        if remaining:
            MANAGER.delete(job["id"])
            raise UserError("La subida se interrumpió.", "Vuelve a intentarlo.")
        MANAGER.start_upload(job["id"])
        self.send_json(202, MANAGER.get(job["id"]))

    def job_action(self, job_id: str, rest: list[str]) -> None:
        action = rest[0]
        if action == "cancel":
            MANAGER.cancel(job_id)
            self.send_json(200, MANAGER.get(job_id) or {})
        elif action == "delete":
            MANAGER.delete(job_id)
            self.send_json(200, {"ok": True})
        elif action == "retry":
            self.send_json(202, MANAGER.retry(job_id))
        elif action == "reselect":
            body = self.read_json()
            self.send_json(202, MANAGER.reselect(job_id, self.job_options(body)))
        elif action == "clips" and len(rest) == 3:
            number = int(rest[1])
            body = self.read_json()
            if rest[2] == "trim":
                self.send_json(202, MANAGER.rerender(job_id, number, float(body["start"]), float(body["end"])))
            elif rest[2] == "render":
                self.send_json(202, MANAGER.rerender(job_id, number))
            elif rest[2] == "meta":
                fields = {}
                for key in ("title", "hook", "caption"):
                    if key in body:
                        fields[key] = " ".join(str(body[key]).split())[:600 if key == "caption" else 120]
                if "hashtags" in body:
                    tags = body["hashtags"] if isinstance(body["hashtags"], list) else str(body["hashtags"]).replace(",", " ").split()
                    fields["hashtags"] = [re.sub(r"[^\w]", "", str(t).lstrip("#")) for t in tags if str(t).strip()][:10]
                if "hook" in fields:
                    fields["dirty"] = True
                self.send_json(200, MANAGER.update_clip(job_id, number, **fields))
            elif rest[2] == "publish":
                PUBLISHER.publish(job_id, number, list(body.get("targets", [])), {k: v for k, v in body.get("options", {}).items() if k in config.DEFAULT_SETTINGS})
                self.send_json(202, MANAGER.get(job_id))
            elif rest[2] == "publication-status":
                PUBLISHER.refresh_status(job_id, number, str(body.get("target", "")))
                self.send_json(200, MANAGER.get(job_id))
            else:
                self.send_json(404, {"error": "Acción desconocida."})
        else:
            self.send_json(404, {"error": "Acción desconocida."})


def main(argv: list[str] | None = None) -> None:
    global MANAGER, PUBLISHER
    argv = list(sys.argv[1:] if argv is None else argv)
    config.load_env()
    if "--check" in argv:
        print(json.dumps(system_status(), ensure_ascii=False, indent=2, default=str))
        return
    MANAGER = JobManager()
    MANAGER.load()
    PUBLISHER = social.Publisher(MANAGER)
    try:
        server = ThreadingHTTPServer((config.HOST, config.port()), Handler)
    except OSError as exc:
        print(f"No se pudo abrir el puerto {config.port()}: {exc}. ¿Ya está abierta la app? Visita {config.base_url()}")
        if "--open" in argv:
            webbrowser.open(config.base_url())
        return
    server.daemon_threads = True
    print(f"\n  ✂  Corta Clips {__version__} listo en {config.base_url()}\n     (Ctrl+C para salir)\n")
    if "--open" in argv:
        threading.Timer(0.8, lambda: webbrowser.open(config.base_url())).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
