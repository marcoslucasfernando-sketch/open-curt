"""Loopback-only web UI for Corta Clips. No third-party Python packages."""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pipeline import check_dependencies, process, render_clip, validate_url
from publisher import check_connected_accounts, configured as publishing_configured, publish as publish_clip, status as publishing_status


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
HTML = (ROOT / "index.html").read_bytes()
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def load_local_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name in {"OPENAI_API_KEY", "UPLOAD_POST_API_KEY", "UPLOAD_POST_USER"} and value.strip():
            os.environ.setdefault(name, value.strip().strip('"').strip("'"))


def update(job_id: str, **values) -> None:
    with LOCK:
        JOBS[job_id].update(values)


def queue_publications(job_id: str) -> None:
    with LOCK:
        manifest = JOBS[job_id]["manifest"]
        JOBS[job_id]["publishing"] = "uploading"
    folder = OUTPUTS / job_id
    with LOCK:
        records = list(JOBS[job_id].get("publications", []))
    try:
        check_connected_accounts()
    except Exception as exc:
        update(job_id, publishing="error", message=str(exc))
        return
    for number, clip in enumerate(manifest["clips"], 1):
        if any(record["clip"] == number and record["status"] == "submitted" for record in records):
            continue
        update(job_id, message=f"Enviando clip {number} de {len(manifest['clips'])} a la cola de publicación…")
        try:
            response = publish_clip(folder / clip["file"], clip["title"], clip["hook"], job_id, number)
            record = {"clip": number, "status": "submitted", "response": response}
        except Exception as exc:
            record = {"clip": number, "status": "error", "error": str(exc)}
        records = sorted([r for r in records if r["clip"] != number] + [record], key=lambda r: r["clip"])
        with LOCK:
            JOBS[job_id]["publications"] = list(records)
    (folder / "publications.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = any(record["status"] == "error" for record in records)
    update(job_id, publishing="partial_error" if failed else "submitted", message="Algunos envíos fallaron; puedes reintentar." if failed else "Clips enviados a la cola. Consulta el estado de publicación.")


def background(job_id: str, url: str, count: int, low: int, high: int, auto_publish: bool) -> None:
    try:
        manifest = process(url, OUTPUTS / job_id, count, low, high, lambda message: update(job_id, message=message))
        update(job_id, status="done", message="Clips listos", manifest=manifest)
        if auto_publish:
            queue_publications(job_id)
    except Exception as exc:
        update(job_id, status="error", message=str(exc))


class Handler(BaseHTTPRequestHandler):
    server_version = "CortaClips/1.0"

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")

    def json_response(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 10000:
            raise ValueError("Solicitud demasiado grande o vacía.")
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(HTML)))
            self.end_headers()
            self.wfile.write(HTML)
            return
        if path == "/api/health":
            try:
                check_dependencies()
                self.json_response(200, {"ready": True, "model": "gpt-6-luna", "publishing_ready": publishing_configured()})
            except RuntimeError as exc:
                self.json_response(200, {"ready": False, "reason": str(exc), "model": "gpt-6-luna", "publishing_ready": publishing_configured()})
            return
        if path == "/api/jobs":
            with LOCK:
                jobs = [{"id": item["id"], "status": item["status"], "title": item.get("manifest", {}).get("source_title", "Procesando")}
                        for item in reversed(list(JOBS.values()))]
            self.json_response(200, {"jobs": jobs[:30]})
            return
        if path.startswith("/api/publication-status/"):
            job_id = path.split("/")[-1]
            with LOCK:
                job = JOBS.get(job_id)
            if not job:
                self.json_response(404, {"error": "Trabajo no encontrado."})
                return
            reports = []
            for record in job.get("publications", []):
                if record["status"] == "error":
                    reports.append(record)
                    continue
                response = record["response"]
                try:
                    state = publishing_status(response.get("request_id") or (response.get("client_request_id") if not response.get("job_id") else None), response.get("job_id"))
                    reports.append({"clip": record["clip"], "status": state})
                except Exception as exc:
                    reports.append({"clip": record["clip"], "error": str(exc)})
            self.json_response(200, {"reports": reports, "publishing": job.get("publishing"), "message": job.get("message")})
            return
        if path.startswith("/api/jobs/"):
            job_id = path.split("/")[-1]
            with LOCK:
                job = JOBS.get(job_id)
            if job:
                self.json_response(200, job)
            else:
                self.json_response(404, {"error": "Trabajo no encontrado."})
            return
        if path.startswith("/files/"):
            parts = path.split("/")
            if len(parts) != 4 or parts[2] not in JOBS or parts[3] not in ("manifest.json", "transcript.json", "publications.json") and not (parts[3].startswith("clip_") and parts[3].endswith(".mp4")):
                self.json_response(404, {"error": "Archivo no encontrado."})
                return
            file_path = OUTPUTS / parts[2] / parts[3]
            if not file_path.is_file():
                self.json_response(404, {"error": "Archivo no encontrado."})
                return
            size = file_path.stat().st_size
            requested = self.headers.get("Range", "")
            start, end = 0, size - 1
            if requested.startswith("bytes="):
                try:
                    left, right = requested[6:].split("-", 1)
                    start = int(left) if left else max(0, size - int(right))
                    end = min(size - 1, int(right)) if right else size - 1
                    if start > end or start >= size:
                        raise ValueError
                except ValueError:
                    self.send_error(416)
                    return
            self.send_response(206 if requested else 200)
            self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with file_path.open("rb") as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining and (block := source.read(min(1024 * 1024, remaining))):
                    self.wfile.write(block)
                    remaining -= len(block)
            return
        self.json_response(404, {"error": "Ruta no encontrada."})

    def do_POST(self) -> None:
        if self.headers.get("Origin") not in (None, "http://127.0.0.1:8766", "http://localhost:8766"):
            self.json_response(403, {"error": "Origen no permitido."})
            return
        if self.path.startswith("/api/jobs/") and self.path.endswith("/trim"):
            self.trim()
            return
        if self.path.startswith("/api/jobs/") and self.path.endswith("/publish"):
            job_id = self.path.split("/")[3]
            with LOCK:
                job = JOBS.get(job_id)
            if not job or job["status"] != "done":
                self.json_response(404, {"error": "Trabajo no disponible."})
            elif not publishing_configured():
                self.json_response(400, {"error": "Configura UPLOAD_POST_API_KEY y UPLOAD_POST_USER."})
            elif job.get("publishing") in ("uploading", "submitted"):
                self.json_response(409, {"error": "Estos clips ya se enviaron a la cola. Consulta el estado antes de repetir."})
            else:
                threading.Thread(target=queue_publications, args=(job_id,), daemon=True).start()
                self.json_response(202, {"message": "Enviando a la cola de Upload-Post…"})
            return
        if self.path != "/api/jobs":
            self.json_response(404, {"error": "Ruta no encontrada."})
            return
        try:
            body = self.read_json()
            url = validate_url(str(body.get("url", "")))
            count = int(body.get("count", 4))
            low = int(body.get("min_seconds", 20))
            high = int(body.get("max_seconds", 50))
            auto_publish = body.get("auto_publish", False) is True
            if not 1 <= count <= 8 or not 10 <= low <= high <= 90:
                raise ValueError("Elige entre 1 y 8 clips, de 10 a 90 segundos.")
            check_dependencies()
            if auto_publish and not publishing_configured():
                raise ValueError("Configura UPLOAD_POST_API_KEY y UPLOAD_POST_USER para publicar automáticamente.")
        except (ValueError, RuntimeError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.json_response(400, {"error": str(exc)})
            return
        job_id = secrets.token_hex(8)
        update_obj = {"id": job_id, "status": "running", "message": "Preparando…"}
        with LOCK:
            JOBS[job_id] = update_obj
        threading.Thread(target=background, args=(job_id, url, count, low, high, auto_publish), daemon=True).start()
        self.json_response(202, update_obj)

    def trim(self) -> None:
        job_id = self.path.split("/")[3]
        with LOCK:
            job = JOBS.get(job_id)
        if not job or job["status"] != "done":
            self.json_response(404, {"error": "Trabajo no disponible."})
            return
        if job.get("publishing") in ("uploading", "submitted"):
            self.json_response(409, {"error": "El clip ya se envió para publicación y no puede modificarse aquí."})
            return
        try:
            body = self.read_json()
            number = int(body["number"])
            start, end = float(body["start"]), float(body["end"])
            manifest = job["manifest"]
            if not 1 <= number <= len(manifest["clips"]) or not 0 <= start < end <= manifest["source_duration"] or not 5 <= end - start <= 120:
                raise ValueError("Corte fuera de los límites del vídeo (5 a 120 segundos).")
            folder = OUTPUTS / job_id
            source = next((p for p in folder.glob("source.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")), None)
            if not source:
                raise ValueError("No se encontró el vídeo original.")
            segments = json.loads((folder / "transcript.json").read_text())
            clip = dict(manifest["clips"][number - 1], start=start, end=end)
            render_clip(source, segments, clip, folder, number)
            manifest["clips"][number - 1] = clip
            (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            self.json_response(200, {"clip": clip})
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": str(exc)})


def main() -> None:
    load_local_env()
    OUTPUTS.mkdir(exist_ok=True)
    for manifest_path in OUTPUTS.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            records_path = manifest_path.parent / "publications.json"
            records = json.loads(records_path.read_text(encoding="utf-8")) if records_path.exists() else []
            publishing = "partial_error" if any(r["status"] == "error" for r in records) else ("submitted" if records else None)
            JOBS[manifest_path.parent.name] = {"id": manifest_path.parent.name, "status": "done", "message": "Clips listos", "manifest": manifest, "publications": records, "publishing": publishing}
        except (OSError, json.JSONDecodeError):
            continue
    server = ThreadingHTTPServer(("127.0.0.1", 8766), Handler)
    print("Corta Clips: http://127.0.0.1:8766")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
