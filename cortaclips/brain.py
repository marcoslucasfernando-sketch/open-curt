"""Selección de momentos con IA.

Proveedores:
- openai: API de OpenAI con OPENAI_API_KEY (GPT-6 Luna por defecto).
- codex:  tu cuenta de ChatGPT vía Codex CLI (inicio de sesión OAuth, sin API key).
- claude: tu cuenta de Claude vía Claude Code (inicio de sesión OAuth, sin API key).
"""

from __future__ import annotations

import bisect
import json
import math
import os
import platform
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import env
from .util import JobContext, UserError, run, which

OPENAI_API = "https://api.openai.com/v1"
MAX_WINDOW_CHARS = 350_000

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "score": {"type": "integer"},
                },
                "required": ["start", "end", "title", "hook", "caption", "hashtags", "reason", "score"],
            },
        }
    },
    "required": ["clips"],
}

SYSTEM = (
    "Eres el mejor editor de vídeo corto del mundo (TikTok, Instagram Reels, YouTube Shorts). "
    "Encuentras en transcripciones largas los momentos que retienen al espectador de principio a fin. "
    "Solo eliges fragmentos verificables en la transcripción: nunca inventas contenido ni tiempos. "
    "Respondes únicamente con JSON válido que cumple el esquema pedido."
)


# ---------------------------------------------------------------- Proveedores

_status_cache: dict[str, tuple[float, dict]] = {}


def _cached(name: str, fn, ttl: float = 20.0) -> dict:
    now = time.monotonic()
    hit = _status_cache.get(name)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _status_cache[name] = (now, value)
    return value


def invalidate_status() -> None:
    _status_cache.clear()


def codex_status() -> dict:
    def check() -> dict:
        exe = which("codex")
        if not exe:
            return {"installed": False, "logged_in": False, "detail": ""}
        try:
            out = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=20)
            text = (out.stdout + out.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"installed": True, "logged_in": False, "detail": str(exc)}
        logged = "logged in" in text.lower() and "not logged in" not in text.lower()
        return {"installed": True, "logged_in": logged, "chatgpt": "chatgpt" in text.lower(), "detail": text[:200]}

    return _cached("codex", check)


def claude_status() -> dict:
    def check() -> dict:
        exe = which("claude")
        if not exe:
            return {"installed": False, "logged_in": False, "detail": ""}
        try:
            out = subprocess.run([exe, "auth", "status", "--json"], capture_output=True, text=True, timeout=20)
            data = json.loads(out.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            return {"installed": True, "logged_in": False, "detail": str(exc)[:200]}
        return {"installed": True, "logged_in": bool(data.get("loggedIn")), "detail": data.get("authMethod", "")}

    return _cached("claude", check)


def providers_status() -> dict:
    return {
        "openai": {"installed": True, "logged_in": bool(env("OPENAI_API_KEY"))},
        "codex": codex_status(),
        "claude": claude_status(),
    }


def resolve_provider(settings: dict) -> str:
    choice = settings.get("ai_provider", "auto")
    status = providers_status()
    if choice != "auto":
        if not status[choice]["logged_in"]:
            raise UserError(*NOT_READY[choice])
        return choice
    for name in ("openai", "codex", "claude"):
        if status[name]["logged_in"]:
            return name
    raise UserError(
        "No hay ninguna IA conectada.",
        "En Ajustes → IA pulsa «Conectar con ChatGPT» (usa tu suscripción, sin API key) o añade una OPENAI_API_KEY.",
    )


NOT_READY = {
    "openai": ("Falta OPENAI_API_KEY para usar GPT-6 Luna por API.", "Añádela en Ajustes → IA, o elige «ChatGPT (OAuth)»."),
    "codex": ("No has iniciado sesión con ChatGPT (Codex).", "En Ajustes → IA pulsa «Conectar con ChatGPT». Si no tienes Codex: npm install -g @openai/codex"),
    "claude": ("No has iniciado sesión con Claude.", "En Ajustes → IA pulsa «Conectar con Claude». Si no tienes Claude Code: curl -fsSL https://claude.ai/install.sh | bash"),
}


def provider_label(name: str, settings: dict) -> str:
    model = settings.get("ai_model") or "gpt-6-luna"
    if name == "openai":
        return f"{model} · API de OpenAI"
    if name == "codex":
        return f"{model} · cuenta de ChatGPT"
    return (settings.get("claude_model") or "Claude") + " · cuenta de Claude"


def start_login(provider: str) -> dict:
    """Abre el inicio de sesión OAuth del CLI oficial (ChatGPT o Claude)."""
    if provider not in ("codex", "claude"):
        raise UserError("Proveedor desconocido.")
    exe = which(provider)
    if not exe:
        raise UserError(*{
            "codex": ("Codex CLI no está instalado.", "Instálalo con: npm install -g @openai/codex  (o brew install --cask codex) y vuelve a pulsar."),
            "claude": ("Claude Code no está instalado.", "Instálalo con: curl -fsSL https://claude.ai/install.sh | bash  y vuelve a pulsar."),
        }[provider])
    command = [exe, "login"] if provider == "codex" else [exe, "auth", "login"]
    invalidate_status()
    if platform.system() == "Darwin":
        script = " ".join(_shell_quote(part) for part in command)
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"', "-e", 'tell application "Terminal" to activate'])
        return {"message": "Se ha abierto una ventana de Terminal con el inicio de sesión. Autoriza en el navegador y vuelve aquí."}
    # Fuera de macOS: lanzamos el login en segundo plano y devolvemos el enlace si aparece.
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True)
    url = ""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and proc.stdout:
        line = proc.stdout.readline()
        if not line:
            break
        found = re.search(r"https://\S+", line)
        if found:
            url = found.group(0)
            break
    return {"message": "Abre el enlace para autorizar." if url else "Completa el inicio de sesión en la terminal donde abriste la app.", "url": url}


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def call_openai(system: str, prompt: str, settings: dict, ctx: JobContext | None = None) -> dict:
    model = settings.get("ai_model") or "gpt-6-luna"
    payload = {
        "model": model,
        "input": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "text": {"format": {"type": "json_schema", "name": "clip_selection", "strict": True, "schema": SCHEMA}},
    }
    effort = settings.get("ai_effort") or "medium"
    if effort:
        payload["reasoning"] = {"effort": effort}
    last = ""
    for attempt in range(5):
        if ctx:
            ctx.check()
        request = urllib.request.Request(
            OPENAI_API + "/responses", data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + env("OPENAI_API_KEY"), "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                data = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1500]
            last = detail
            if exc.code == 401:
                raise UserError("OpenAI rechazó la clave (401).", "Revisa OPENAI_API_KEY en Ajustes → IA.", detail) from exc
            if exc.code == 404 or "model_not_found" in detail:
                raise UserError(f"Tu cuenta de OpenAI no tiene acceso al modelo {model}.",
                                "Verifica tu organización en platform.openai.com o prueba «ChatGPT (OAuth)» en Ajustes → IA.", detail) from exc
            if exc.code == 429 and "quota" in detail.lower():
                raise UserError("Tu cuenta de OpenAI no tiene saldo.", "Añade crédito en platform.openai.com o usa «ChatGPT (OAuth)».", detail) from exc
            if exc.code == 400 and "reasoning" in detail and "reasoning" in payload:
                payload.pop("reasoning")
                continue
            if exc.code not in (408, 409, 429, 500, 502, 503, 504):
                raise UserError(f"OpenAI devolvió un error ({exc.code}).", detail[:300], detail) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = str(exc)
        time.sleep(min(30, 2 ** attempt * 2))
    else:
        raise UserError("No se pudo contactar con OpenAI.", "Comprueba tu conexión y vuelve a intentarlo.", last)
    for item in data.get("output", []):
        for part in item.get("content", []) or []:
            if part.get("type") == "refusal":
                raise UserError("El modelo se negó a analizar este contenido.", str(part.get("refusal", ""))[:300])
    text = "".join(
        part.get("text", "")
        for item in data.get("output", []) if item.get("type") == "message"
        for part in item.get("content", []) or [] if part.get("type") == "output_text"
    )
    if not text:
        reason = (data.get("incomplete_details") or {}).get("reason", "")
        raise UserError("La IA no devolvió resultados.", f"Estado: {data.get('status')} {reason}".strip(), json.dumps(data)[:2000])
    return _extract_json(text)


def _clean_env(*drop: str) -> dict:
    return {k: v for k, v in os.environ.items() if k not in drop}


def call_codex(system: str, prompt: str, settings: dict, ctx: JobContext | None = None) -> dict:
    exe = which("codex")
    if not exe:
        raise UserError(*NOT_READY["codex"])
    model = settings.get("ai_model") or "gpt-6-luna"
    effort = {"none": "low", "max": "xhigh"}.get(settings.get("ai_effort", "medium"), settings.get("ai_effort", "medium"))
    with tempfile.TemporaryDirectory(prefix="cortaclips-codex-") as tmp:
        schema_path = Path(tmp) / "schema.json"
        out_path = Path(tmp) / "respuesta.json"
        schema_path.write_text(json.dumps(SCHEMA), encoding="utf-8")
        args = [
            exe, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "read-only", "--color", "never", "-m", model,
            "-c", f'model_reasoning_effort="{effort}"',
            "--output-schema", str(schema_path), "-o", str(out_path), "-C", tmp, "-",
        ]
        message = (
            system + "\n\nNo ejecutes comandos ni leas archivos: todo lo necesario está en este mensaje. "
            "Tu respuesta final debe ser solo el JSON.\n\n" + prompt
        )
        try:
            run(args, ctx=ctx, stdin_data=message, timeout=1800, env=_clean_env("OPENAI_API_KEY"), cwd=Path(tmp), what="Codex")
        except RuntimeError as exc:
            text = str(exc)
            lowered = text.lower()
            if "not logged in" in lowered or "401" in lowered or "unauthorized" in lowered or "login" in lowered and "expired" in lowered:
                invalidate_status()
                raise UserError("Tu sesión de ChatGPT ha caducado.", "En Ajustes → IA pulsa «Conectar con ChatGPT» otra vez.", text) from exc
            if "usage limit" in lowered or "rate limit" in lowered:
                raise UserError("Has alcanzado el límite de uso de tu plan de ChatGPT.", "Espera a que se reinicie o usa una OPENAI_API_KEY.", text) from exc
            if "model" in lowered and ("not supported" in lowered or "does not exist" in lowered or "not found" in lowered):
                raise UserError(f"Tu cuenta de ChatGPT no permite usar {model} en Codex.", "Cambia el modelo en Ajustes → IA.", text) from exc
            raise UserError("Codex (ChatGPT) devolvió un error.", text.strip().splitlines()[-1][:300] if text.strip() else "", text) from exc
        if not out_path.is_file():
            raise UserError("Codex no devolvió respuesta.", "Vuelve a intentarlo.")
        return _extract_json(out_path.read_text(encoding="utf-8"))


def call_claude(system: str, prompt: str, settings: dict, ctx: JobContext | None = None) -> dict:
    exe = which("claude")
    if not exe:
        raise UserError(*NOT_READY["claude"])
    args = [
        exe, "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
        "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands",
        "--system-prompt", system,
    ]
    if settings.get("claude_model"):
        args += ["--model", settings["claude_model"]]
    with tempfile.TemporaryDirectory(prefix="cortaclips-claude-") as tmp:
        try:
            out = run(args, ctx=ctx, stdin_data=prompt, timeout=1800, env=_clean_env("ANTHROPIC_API_KEY"), cwd=Path(tmp), what="Claude")
        except RuntimeError as exc:
            text = str(exc)
            if "login" in text.lower() or "401" in text or "auth" in text.lower():
                invalidate_status()
                raise UserError("Tu sesión de Claude ha caducado.", "En Ajustes → IA pulsa «Conectar con Claude».", text) from exc
            raise UserError("Claude devolvió un error.", text.strip().splitlines()[-1][:300] if text.strip() else "", text) from exc
    data = _extract_json(out)
    if data.get("is_error"):
        raise UserError("Claude devolvió un error.", str(data.get("result", ""))[:300], json.dumps(data)[:2000])
    structured = data.get("structured_output")
    if isinstance(structured, dict):
        return structured
    return _extract_json(str(data.get("result", "")))


CALLERS = {"openai": call_openai, "codex": call_codex, "claude": call_claude}


def test_provider(name: str, settings: dict) -> str:
    """Llamada mínima para comprobar la conexión."""
    prompt = "Devuelve un JSON con clips=[] (lista vacía). Es una prueba de conexión."
    result = CALLERS[name](SYSTEM, prompt, settings)
    if "clips" not in result:
        raise UserError("La IA respondió en un formato inesperado.", json.dumps(result)[:200])
    return provider_label(name, settings)


# ---------------------------------------------------------------- Selección

def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"


def build_prompt(lines: list[str], meta: dict, count: int, min_s: int, max_s: int, instructions: str, span: tuple[float, float] | None) -> str:
    chapters = "\n".join(f"- {format_time(c['start'])} {c['title']}" for c in meta.get("chapters", [])[:60])
    parts = [
        f"VÍDEO: «{meta.get('title', 'Sin título')}»" + (f" de {meta['uploader']}" if meta.get("uploader") else ""),
        f"Duración total: {meta.get('duration', 0):.0f} s ({format_time(meta.get('duration', 0))}). "
        f"Formato de origen: {'vídeo' if meta.get('has_video') else 'solo audio (podcast)'}.",
    ]
    if span:
        parts.append(f"Esta es la parte de {format_time(span[0])} a {format_time(span[1])} de una transcripción más larga.")
    if chapters:
        parts.append("Capítulos del autor:\n" + chapters)
    if meta.get("description"):
        parts.append("Descripción (resumida): " + " ".join(meta["description"].split())[:600])
    ideal = round((min_s + max_s) / 2)
    parts.append(f"""
TAREA: elige los {count} mejores clips para redes verticales.

REGLAS DE ORO
1. Autónomo: cada clip se entiende sin haber visto nada más. Si hace falta contexto, empieza antes.
2. Gancho inmediato: los 2 primeros segundos deben atrapar (pregunta, afirmación fuerte, dato sorprendente, conflicto, confesión, promesa). Nunca empieces con muletillas («y», «entonces», «bueno», «o sea», «claro») ni a mitad de frase.
3. Cierre con remate: termina en una conclusión, frase redonda, risa o giro. Nunca a mitad de idea ni con «y…».
4. Duración entre {min_s} y {max_s} s (lo ideal ronda {ideal} s). Sin solapes entre clips.
5. Prioriza: historias personales, opiniones polémicas pero defendibles, datos sorprendentes, consejos accionables, humor, emoción, frases citables. Repártelos por todo el vídeo si hay buen material.
6. Evita: presentaciones, saludos, anuncios/patrocinios, despedidas, llamadas a suscribirse y momentos que dependen de algo visual no descrito.
7. Tiempos: start = inicio de la primera frase del clip; end = fin de la última frase. Usa las marcas [inicio-fin] de la transcripción (segundos).

TEXTOS (en el idioma de la transcripción)
- title: título para YouTube Shorts, máx. 70 caracteres, concreto y con curiosidad, sin mentir.
- hook: texto grande que se verá en pantalla al principio, máx. 7 palabras.
- caption: descripción para TikTok/Instagram de 1-2 frases que invite a comentar (sin hashtags).
- hashtags: 3-6 hashtags relevantes sin «#».
- reason: por qué este momento funciona, en 1 frase.
- score: 0-100, potencial editorial del clip (sé exigente; no es una predicción de visitas).""")
    if instructions.strip():
        parts.append("INSTRUCCIONES DEL USUARIO (tienen prioridad):\n" + instructions.strip()[:2000])
    parts.append("TRANSCRIPCIÓN\n" + "\n".join(lines))
    return "\n\n".join(parts)


def transcript_lines(segments: list[dict]) -> list[str]:
    return [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments]


def choose_clips(transcript: dict, meta: dict, count: int, min_s: int, max_s: int, instructions: str,
                 settings: dict, ctx: JobContext) -> tuple[list[dict], str]:
    provider = resolve_provider(settings)
    label = provider_label(provider, settings)
    ctx.report(stage="select", progress=0.05, message=f"{label} está buscando los mejores momentos…")
    lines = transcript_lines(transcript["segments"])
    windows = _windows(lines, transcript["segments"])
    wanted = count + max(2, math.ceil(count * 0.5))
    raw: list[dict] = []
    for index, (a, b) in enumerate(windows):
        ctx.check()
        share = max(2, math.ceil(wanted * (b - a) / len(lines))) if len(windows) > 1 else wanted
        span = (transcript["segments"][a]["start"], transcript["segments"][b - 1]["end"]) if len(windows) > 1 else None
        prompt = build_prompt(lines[a:b], meta, share, min_s, max_s, instructions, span)
        ctx.log(f"Prompt IA ({provider}): {len(prompt)} caracteres")
        result = CALLERS[provider](SYSTEM, prompt, settings, ctx)
        raw.extend(result.get("clips", []) if isinstance(result, dict) else [])
        ctx.report(progress=0.1 + 0.85 * (index + 1) / len(windows))
    (ctx.folder / "ai_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    clips = finalize(raw, transcript["words"], meta["duration"], count, min_s, max_s)
    if not clips:
        raise UserError("La IA no encontró cortes válidos con esa duración.", "Prueba con otra duración (por ejemplo 20–60 s) o menos clips.")
    return clips, label


def _windows(lines: list[str], segments: list[dict]) -> list[tuple[int, int]]:
    total = sum(len(line) + 1 for line in lines)
    if total <= MAX_WINDOW_CHARS:
        return [(0, len(lines))]
    windows, start, size = [], 0, 0
    for index, line in enumerate(lines):
        size += len(line) + 1
        if size >= MAX_WINDOW_CHARS:
            windows.append((start, index + 1))
            # Solape de ~40 líneas para no perder momentos en el borde.
            start, size = max(start + 1, index - 40), 0
    if start < len(lines):
        windows.append((start, len(lines)))
    return windows


# ---------------------------------------------------------------- Ajuste fino

ENDING = re.compile(r"[.!?…»\"]$")


def snap(words: list[dict], start: float, end: float, min_s: float, max_s: float, duration: float) -> tuple[float, float, int, int]:
    """Ajusta el corte a los límites de palabra y a la duración pedida."""
    if not words:
        return max(0.0, start), min(duration, end), 0, -1
    starts = [w["start"] for w in words]

    def speech(a: float, b: float) -> float:
        """Segundos de voz entre a y b (los silencios no cuentan como distancia)."""
        if b <= a:
            return 0.0
        k = max(0, bisect.bisect_left(starts, a) - 1)
        total = 0.0
        while k < len(words) and words[k]["start"] < b:
            total += max(0.0, min(b, words[k]["end"]) - max(a, words[k]["start"]))
            k += 1
        return total

    punctuated = sum(1 for w in words[:2000] if ENDING.search(w["text"])) >= min(len(words), 2000) * 0.02
    i = bisect.bisect_left(starts, start - 3.0)
    candidates = [k for k in range(i, min(len(words), i + 80)) if words[k]["start"] <= start + 8.0]
    if candidates:
        # Preferimos la palabra que abre frase (la anterior acaba en punto) cerca del inicio pedido.
        def start_cost(k: int) -> float:
            ws = words[k]["start"]
            distance = speech(min(ws, start), max(ws, start))
            if k == 0:
                return distance - 2.0
            after_stop = bool(ENDING.search(words[k - 1]["text"]))
            pause = ws - words[k - 1]["end"] > 0.6
            capital = words[k]["text"][:1].isupper() or words[k]["text"][:1] in "¿¡"
            if after_stop:
                return distance - 2.0
            if capital:
                return distance - (1.0 if pause else 0.2)
            # Minúscula sin punto antes: estamos a mitad de frase.
            return distance + (0.6 if punctuated else 0.0) - (0.3 if pause else 0.0)
        si = min(candidates, key=start_cost)
    else:
        si = min(len(words) - 1, i)
    ends = [w["end"] for w in words]
    j = bisect.bisect_right(ends, end + 3.0) - 1
    candidates = [k for k in range(max(si, j - 80), j + 1) if words[k]["end"] >= end - 8.0]
    if candidates:
        def end_cost(k: int) -> float:
            we = words[k]["end"]
            distance = speech(min(we, end), max(we, end))
            closes = bool(ENDING.search(words[k]["text"]))
            pause = k + 1 < len(words) and words[k + 1]["start"] - we > 0.8
            # Penaliza terminar en la primera palabra de una frase nueva («…ciencia. Yo»).
            dangling = k > si and bool(ENDING.search(words[k - 1]["text"])) and not closes
            return distance - (2.0 if closes else 0.5 if pause else 0) + (2.5 if dangling else 0)
        ei = min(candidates, key=end_cost)
    else:
        ei = max(si, j)
    ei = max(ei, si)
    first = words[si]["start"]
    # Demasiado largo: recortamos hasta un final de frase dentro del máximo.
    if words[ei]["end"] - first > max_s + 1:
        inside = [k for k in range(si, ei + 1) if words[k]["end"] - first <= max_s]
        closing = [k for k in inside if ENDING.search(words[k]["text"]) and words[k]["end"] - first >= min_s]
        ei = (closing or inside or [si])[-1]
    # Demasiado corto: alargamos hasta un final de frase o hasta el mínimo.
    while words[ei]["end"] - first < min_s - 1 and ei + 1 < len(words):
        ei += 1
        if words[ei]["end"] - first >= min_s and ENDING.search(words[ei]["text"]):
            break
    s = max(0.0, first - 0.15)
    if si > 0:
        s = max(s, min(words[si - 1]["end"] + 0.02, first))
    e = words[ei]["end"] + 0.35
    if ei + 1 < len(words):
        e = min(e, max(words[ei]["end"] + 0.05, words[ei + 1]["start"] - 0.03))
    return round(s, 2), round(min(e, duration), 2), si, ei


def finalize(raw: list[dict], words: list[dict], duration: float, count: int, min_s: int, max_s: int) -> list[dict]:
    candidates = []
    for clip in raw:
        try:
            start, end = float(clip["start"]), float(clip["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start or start >= duration:
            continue
        start, end = max(0.0, start), min(duration, end)
        s, e, si, ei = snap(words, start, end, min_s, max_s, duration)
        if e - s < max(4.0, min_s * 0.6) or e - s > max_s * 1.25 + 2:
            continue
        text = " ".join(w["text"] for w in words[si:ei + 1]) if ei >= si else ""
        hashtags = [re.sub(r"[^\w]", "", str(tag).lstrip("#")) for tag in clip.get("hashtags", []) or []]
        candidates.append({
            "start": s,
            "end": e,
            "title": " ".join(str(clip.get("title", "")).split())[:100] or "Clip",
            "hook": " ".join(str(clip.get("hook", "")).split())[:80],
            "caption": " ".join(str(clip.get("caption", "")).split())[:600],
            "hashtags": [tag for tag in hashtags if tag][:8],
            "reason": " ".join(str(clip.get("reason", "")).split())[:400],
            "score": max(0, min(100, int(clip.get("score", 50) or 0))),
            "text": text[:4000],
        })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    selected: list[dict] = []
    for clip in candidates:
        clash = False
        for other in selected:
            overlap = min(clip["end"], other["end"]) - max(clip["start"], other["start"])
            if overlap > min(3.0, 0.2 * min(clip["end"] - clip["start"], other["end"] - other["start"])):
                clash = True
                break
        if not clash:
            selected.append(clip)
        if len(selected) == count:
            break
    return selected
