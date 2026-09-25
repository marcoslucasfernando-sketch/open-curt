"""Utilidades comunes: errores con pista, subprocesos cancelables y registro por trabajo."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Iterable


class UserError(RuntimeError):
    """Error que se muestra tal cual en la interfaz, con una pista para resolverlo."""

    def __init__(self, message: str, hint: str = "", detail: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail


class Cancelled(RuntimeError):
    pass


class JobContext:
    """Estado compartido por las etapas de un trabajo: log, progreso y cancelación."""

    def __init__(self, folder: Path, report: Callable[..., None] | None = None):
        folder = Path(folder).resolve()
        self.folder = folder
        self.cancel_event = threading.Event()
        self._report = report or (lambda **_: None)
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        folder.mkdir(parents=True, exist_ok=True)
        self.log_path = folder / "job.log"

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                for line in str(text).rstrip().splitlines() or [""]:
                    handle.write(f"[{stamp}] {line}\n")
        except OSError:
            pass

    def report(self, **values) -> None:
        if "message" in values:
            self.log(values["message"])
        self._report(**values)

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled("Trabajo cancelado.")

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass

    def track(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)

    def untrack(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)


def run(
    args: Iterable[str],
    ctx: JobContext | None = None,
    timeout: float = 3600,
    cwd: Path | None = None,
    env: dict | None = None,
    on_line: Callable[[str], None] | None = None,
    stdin_data: str | None = None,
    what: str = "",
) -> str:
    """Ejecuta un programa. Devuelve stdout. Lanza RuntimeError con el final de stderr si falla."""
    args = [str(a) for a in args]
    if ctx:
        ctx.check()
        ctx.log("$ " + " ".join(_short(a) for a in args))
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if ctx:
        ctx.track(proc)
    out_lines: list[str] = []
    err_lines: list[str] = []

    def pump(stream, sink, callback):
        for line in iter(stream.readline, ""):
            sink.append(line)
            if len(sink) > 4000:
                del sink[:1000]
            if callback:
                try:
                    callback(line.rstrip("\n"))
                except Exception:  # noqa: BLE001 - un callback nunca debe romper el proceso
                    pass
        stream.close()

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, out_lines, on_line), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err_lines, on_line), daemon=True),
    ]
    for thread in threads:
        thread.start()
    if stdin_data is not None:
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if ctx and ctx.cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise Cancelled("Trabajo cancelado.")
            if time.monotonic() > deadline:
                proc.kill()
                raise RuntimeError(f"{what or args[0]} tardó demasiado (más de {int(timeout)} s).")
            time.sleep(0.1)
    finally:
        for thread in threads:
            thread.join(2)
        if ctx:
            ctx.untrack(proc)
    stdout = "".join(out_lines)
    stderr = "".join(err_lines)
    if proc.returncode:
        tail = (stderr.strip() or stdout.strip())[-2500:]
        if ctx:
            ctx.log(f"(código {proc.returncode})\n{tail}")
        raise RuntimeError(tail or f"{args[0]} terminó con código {proc.returncode}")
    return stdout


def _short(arg: str) -> str:
    return arg if len(arg) < 300 else arg[:120] + f"…({len(arg)} caracteres)"


def which(*names: str) -> str | None:
    """Busca un ejecutable en PATH y en las rutas típicas de Homebrew."""
    extra = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"), str(Path(sys.executable).parent)]
    path = os.pathsep.join([os.environ.get("PATH", "")] + extra)
    for name in names:
        found = shutil.which(name, path=path)
        if found:
            return found
    return None


def format_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
