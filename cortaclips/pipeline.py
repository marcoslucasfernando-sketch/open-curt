"""Gestión de trabajos: cola, etapas, progreso, persistencia y reintentos."""

from __future__ import annotations

import json
import queue
import secrets
import shutil
import threading
import time
from pathlib import Path

from . import brain, config, render, sources, transcribe
from .util import Cancelled, JobContext, UserError, format_exception

STAGES = {"download": (0.0, 0.15), "transcribe": (0.15, 0.55), "select": (0.55, 0.65), "render": (0.65, 1.0)}
CLIP_TEXT_FIELDS = ("title", "hook", "caption", "hashtags")


class JobManager:
    def __init__(self, outputs: Path = config.OUTPUTS):
        self.outputs = outputs
        self.jobs: dict[str, dict] = {}
        self.contexts: dict[str, JobContext] = {}
        self.lock = threading.RLock()
        self.queue: queue.Queue = queue.Queue()
        self.render_lock = threading.Lock()
        self.worker = threading.Thread(target=self._work, daemon=True, name="cortaclips-worker")
        self.worker.start()

    # ------------------------------------------------------------ persistencia

    def load(self) -> None:
        self.outputs.mkdir(parents=True, exist_ok=True)
        for folder in sorted(self.outputs.iterdir()):
            if not folder.is_dir():
                continue
            job = None
            try:
                if (folder / "job.json").is_file():
                    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
                elif (folder / "manifest.json").is_file():
                    job = self._from_legacy(folder)
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
            if not job:
                continue
            if job.get("status") in ("queued", "running"):
                job.update(status="error", error={"message": "La app se cerró mientras se procesaba.", "hint": "Pulsa «Reintentar»: se reaprovecha lo ya descargado y transcrito.", "detail": ""})
            self.jobs[job["id"]] = job

    def _from_legacy(self, folder: Path) -> dict:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        clips = []
        for number, clip in enumerate(manifest.get("clips", []), 1):
            clips.append({**clip, "number": number, "caption": clip.get("caption", clip.get("hook", "")), "hashtags": clip.get("hashtags", []),
                          "publications": {}})
        return {
            "id": folder.name, "created": folder.stat().st_mtime, "updated": folder.stat().st_mtime, "status": "done",
            "stage": "done", "progress": 1.0, "message": "Clips listos", "error": None,
            "input": {"kind": "url", "url": manifest.get("source_url", "")},
            "options": {}, "source": {"title": manifest.get("source_title", "Vídeo"), "duration": manifest.get("source_duration", 0)},
            "ai": manifest.get("selection_model", ""), "clips": clips,
        }

    def save(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            data = json.dumps(job, ensure_ascii=False, indent=2)
        folder = self.outputs / job_id
        if not folder.is_dir():
            return
        tmp = folder / "job.json.tmp"
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(folder / "job.json")

    # ------------------------------------------------------------ consulta

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return json.loads(json.dumps(job)) if job else None

    def summaries(self) -> list[dict]:
        with self.lock:
            items = sorted(self.jobs.values(), key=lambda j: j.get("created", 0), reverse=True)
            return [
                {"id": j["id"], "status": j["status"], "title": (j.get("source") or {}).get("title") or j.get("input", {}).get("url") or j.get("input", {}).get("filename", "Trabajo"),
                 "created": j.get("created"), "clips": len(j.get("clips", [])),
                 "thumb": next((c.get("thumb") for c in j.get("clips", []) if c.get("thumb")), ""), "message": j.get("message", "")}
                for j in items[:60]
            ]

    # ------------------------------------------------------------ creación

    def _new(self, input_info: dict, options: dict) -> dict:
        job_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        now = time.time()
        job = {
            "id": job_id, "created": now, "updated": now, "status": "queued", "stage": "queued", "progress": 0.0,
            "stage_progress": 0.0, "message": "En cola…", "error": None, "input": input_info, "options": options,
            "source": {}, "ai": "", "transcription": {}, "clips": [],
        }
        (self.outputs / job_id).mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.jobs[job_id] = job
        self.save(job_id)
        return job

    def create_from_url(self, url: str, options: dict) -> dict:
        url = sources.validate_url(url)
        job = self._new({"kind": "url", "url": url}, options)
        self.queue.put(("full", job["id"]))
        return job

    def create_upload(self, filename: str, options: dict) -> tuple[dict, Path]:
        suffix = sources.upload_extension(filename)
        job = self._new({"kind": "file", "filename": Path(filename).name[:200]}, options)
        return job, self.outputs / job["id"] / f"source{suffix}"

    def start_upload(self, job_id: str) -> None:
        self.queue.put(("full", job_id))

    def retry(self, job_id: str) -> dict:
        job = self._require(job_id)
        if job["status"] in ("queued", "running"):
            raise UserError("Este trabajo ya está en marcha.")
        self._update(job_id, status="queued", stage="queued", progress=0.0, message="En cola…", error=None)
        self.queue.put(("full", job_id))
        return self.get(job_id)

    def reselect(self, job_id: str, options: dict) -> dict:
        job = self._require(job_id)
        if job["status"] in ("queued", "running"):
            raise UserError("Espera a que termine el trabajo actual.")
        if not (self.outputs / job_id / "transcript.json").is_file():
            raise UserError("Este trabajo no tiene transcripción guardada.", "Usa «Reintentar» para procesarlo desde el principio.")
        self._update(job_id, status="queued", stage="queued", progress=0.0, message="En cola…", error=None,
                     options={**job.get("options", {}), **options})
        self.queue.put(("reselect", job_id))
        return self.get(job_id)

    def cancel(self, job_id: str) -> None:
        job = self._require(job_id)
        ctx = self.contexts.get(job_id)
        if ctx:
            ctx.cancel()
        elif job["status"] == "queued":
            self._update(job_id, status="cancelled", message="Cancelado.")

    def delete(self, job_id: str) -> None:
        job = self._require(job_id)
        if job["status"] in ("queued", "running"):
            self.cancel(job_id)
        with self.lock:
            self.jobs.pop(job_id, None)
        shutil.rmtree(self.outputs / job_id, ignore_errors=True)

    def _require(self, job_id: str) -> dict:
        job = self.get(job_id)
        if not job:
            raise UserError("Trabajo no encontrado.")
        return job

    # ------------------------------------------------------------ actualización

    def _update(self, job_id: str, persist: bool = True, **values) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job.update(values)
            job["updated"] = time.time()
        if persist:
            self.save(job_id)

    def _reporter(self, job_id: str):
        state = {"stage": "download", "last_save": 0.0}

        def report(**values) -> None:
            stage = values.get("stage")
            if stage:
                state["stage"] = stage
            low, high = STAGES.get(state["stage"], (0.0, 1.0))
            update = {}
            if "message" in values:
                update["message"] = values["message"]
            if stage:
                update["stage"] = stage
            if "progress" in values:
                fraction = max(0.0, min(1.0, float(values["progress"])))
                update["stage_progress"] = fraction
                update["progress"] = round(low + (high - low) * fraction, 4)
            elif stage:
                update["stage_progress"] = 0.0
                update["progress"] = low
            now = time.monotonic()
            persist = bool(stage) or now - state["last_save"] > 5
            if persist:
                state["last_save"] = now
            self._update(job_id, persist=persist, **update)

        return report

    def update_clip(self, job_id: str, number: int, **fields) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise UserError("Trabajo no encontrado.")
            clip = next((c for c in job["clips"] if c["number"] == number), None)
            if not clip:
                raise UserError("Clip no encontrado.")
            clip.update(fields)
            job["updated"] = time.time()
            result = json.loads(json.dumps(clip))
        self.save(job_id)
        return result

    # ------------------------------------------------------------ trabajo

    def _work(self) -> None:
        while True:
            kind, job_id = self.queue.get()
            job = self.get(job_id)
            if not job or job["status"] == "cancelled":
                continue
            folder = self.outputs / job_id
            ctx = JobContext(folder, self._reporter(job_id))
            self.contexts[job_id] = ctx
            self._update(job_id, status="running", started=time.time())
            try:
                if kind == "full":
                    self._run_full(job_id, ctx)
                else:
                    self._run_selection(job_id, ctx, json.loads((folder / "transcript.json").read_text(encoding="utf-8")))
                self._update(job_id, status="done", stage="done", progress=1.0, message="¡Clips listos!", finished=time.time())
            except Cancelled:
                self._update(job_id, status="cancelled", message="Cancelado.")
            except UserError as exc:
                ctx.log(f"ERROR: {exc.message}\n{exc.hint}\n{exc.detail}\n{format_exception(exc)}")
                self._update(job_id, status="error", message=exc.message, error={"message": exc.message, "hint": exc.hint, "detail": (exc.detail or "")[-3000:]})
            except Exception as exc:  # noqa: BLE001 - cualquier fallo inesperado se muestra con detalle
                trace = format_exception(exc)
                ctx.log("ERROR inesperado:\n" + trace)
                self._update(job_id, status="error", message="Error inesperado.", error={
                    "message": f"Error inesperado: {exc}"[:300],
                    "hint": "Copia los detalles técnicos y compártelos para corregirlo. Puedes pulsar «Reintentar».",
                    "detail": trace[-3000:],
                })
            finally:
                self.contexts.pop(job_id, None)

    def _run_full(self, job_id: str, ctx: JobContext) -> None:
        job = self.get(job_id)
        settings = {**config.get_settings(), **{k: v for k, v in job.get("options", {}).items() if k in config.DEFAULT_SETTINGS}}
        folder = ctx.folder
        existing = sources.find_source(folder)
        if job["input"]["kind"] == "url" and not (existing and (folder / "source.json").is_file()):
            meta = sources.download(job["input"]["url"], ctx, settings)
        elif existing and (folder / "source.json").is_file():
            meta = json.loads((folder / "source.json").read_text(encoding="utf-8"))
        else:
            if not existing:
                raise UserError("No se encontró el archivo subido.", "Vuelve a subirlo.")
            ctx.report(stage="download", progress=1.0, message="Analizando el archivo…")
            meta = sources.describe(existing)
            if not meta.get("tag_title"):
                meta["title"] = Path(job["input"].get("filename") or existing.name).stem.replace("_", " ")[:200]
        (folder / "source.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self._update(job_id, source=meta)
        if meta.get("duration", 0) > 6 * 3600:
            raise UserError("El archivo dura más de 6 horas.", "Recórtalo o súbelo por partes.")
        if meta.get("duration", 0) < max(8, int(job["options"].get("min_seconds", 20)) * 0.6):
            raise UserError("El vídeo es más corto que la duración mínima de clip.", "Baja la duración mínima o usa un vídeo más largo.")
        transcript_path = folder / "transcript.json"
        if transcript_path.is_file():
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            ctx.report(stage="transcribe", progress=1.0, message="Transcripción reutilizada.")
        else:
            transcript = transcribe.transcribe(folder / meta["file"], meta["duration"], ctx, settings)
            transcript_path.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
        (folder / "audio_16k.mp3").unlink(missing_ok=True)
        self._update(job_id, transcription={"engine": transcript.get("engine"), "model": transcript.get("model"), "language": transcript.get("language"),
                                             "words": len(transcript.get("words", []))})
        self._run_selection(job_id, ctx, transcript)

    def _run_selection(self, job_id: str, ctx: JobContext, transcript: dict) -> None:
        job = self.get(job_id)
        options = job.get("options", {})
        settings = {**config.get_settings(), **{k: v for k, v in options.items() if k in config.DEFAULT_SETTINGS}}
        meta = job.get("source") or json.loads((ctx.folder / "source.json").read_text(encoding="utf-8"))
        count = int(options.get("count", settings["clip_count"]))
        low = int(options.get("min_seconds", settings["min_seconds"]))
        high = int(options.get("max_seconds", settings["max_seconds"]))
        clips, label = brain.choose_clips(transcript, meta, count, low, high, str(options.get("instructions", "")), settings, ctx)
        for old in ctx.folder.glob("clip_*"):
            if old.is_file():
                old.unlink()
        prepared = []
        for number, clip in enumerate(clips, 1):
            prepared.append({**clip, "number": number, "file": "", "status": "pending", "publications": {}})
        self._update(job_id, ai=label, clips=prepared)
        self._render_all(job_id, ctx, transcript, meta, settings)

    def _render_all(self, job_id: str, ctx: JobContext, transcript: dict, meta: dict, settings: dict) -> None:
        clips = self.get(job_id)["clips"]
        total = len(clips)
        source = ctx.folder / meta["file"]
        for index, clip in enumerate(clips):
            ctx.check()
            ctx.report(stage="render", progress=index / total, message=f"Creando clip {index + 1} de {total}: «{clip['title']}»…")

            def on_progress(fraction: float, index=index) -> None:
                ctx.report(progress=(index + fraction) / total)

            with self.render_lock:
                result = render.render_clip(source, meta, meta, transcript["words"], clip, clip["number"], settings, ctx, on_progress)
            self.update_clip(job_id, clip["number"], **result, status="ready", rendered_at=time.time(), dirty=False)

    def rerender(self, job_id: str, number: int, start: float | None = None, end: float | None = None) -> dict:
        """Vuelve a renderizar un clip (tras ajustar tiempos o textos). Se ejecuta en segundo plano."""
        job = self._require(job_id)
        if job["status"] in ("queued", "running"):
            raise UserError("Espera a que termine el trabajo actual.")
        clip = next((c for c in job["clips"] if c["number"] == number), None)
        if not clip:
            raise UserError("Clip no encontrado.")
        duration = float(job["source"].get("duration", 0))
        start = float(clip["start"] if start is None else start)
        end = float(clip["end"] if end is None else end)
        if not (0 <= start < end <= duration + 0.05) or not 3 <= end - start <= 180:
            raise UserError("Corte fuera de los límites.", f"El clip debe durar entre 3 y 180 s y estar dentro del vídeo (0–{duration:.1f} s).")
        folder = self.outputs / job_id
        if not sources.find_source(folder):
            raise UserError("No encuentro el vídeo original de este trabajo.", "Puede que se borrara la carpeta. Crea el trabajo de nuevo.")
        self.update_clip(job_id, number, status="rendering", start=round(start, 2), end=round(min(end, duration), 2))
        threading.Thread(target=self._rerender, args=(job_id, number), daemon=True).start()
        return self.get(job_id)

    def _rerender(self, job_id: str, number: int) -> None:
        folder = self.outputs / job_id
        job = self.get(job_id)
        clip = next(c for c in job["clips"] if c["number"] == number)
        ctx = JobContext(folder, lambda **_: None)
        try:
            transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
            words = transcript["words"]
            clip["text"] = " ".join(w["text"] for w in words if w["start"] >= clip["start"] - 0.05 and w["end"] <= clip["end"] + 0.05)[:4000]
            settings = {**config.get_settings(), **{k: v for k, v in job.get("options", {}).items() if k in config.DEFAULT_SETTINGS}}
            meta = job["source"]
            with self.render_lock:
                result = render.render_clip(folder / meta["file"], meta, meta, words, clip, number, settings, ctx)
            self.update_clip(job_id, number, **result, text=clip["text"], status="ready", rendered_at=time.time(), dirty=False, error="")
        except Exception as exc:  # noqa: BLE001
            message = exc.message if isinstance(exc, UserError) else str(exc)
            ctx.log("Error al re-renderizar:\n" + format_exception(exc))
            self.update_clip(job_id, number, status="error", error=message[:300])
