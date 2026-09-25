#!/bin/zsh
# Corta Clips: doble clic para instalar lo necesario (solo la primera vez) y abrir la app.
cd "${0:A:h}" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

say_step() { print -P "\n%F{green}✂%f  %B$1%b"; }
pause_exit() { print -P "\n%F{red}$1%f"; read -k1 "?Pulsa una tecla para cerrar…"; exit 1; }

say_step "Corta Clips"

# 1. Programas del sistema (Homebrew): ffmpeg para el vídeo y deno para que yt-dlp lea YouTube.
if ! command -v brew >/dev/null 2>&1; then
  print "Homebrew no está instalado. Es la forma fácil de instalar ffmpeg en Mac."
  print 'Instálalo pegando esto en Terminal y vuelve a abrir este archivo:'
  print '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  command -v ffmpeg >/dev/null 2>&1 || pause_exit "Falta ffmpeg."
fi
need=()
command -v ffmpeg >/dev/null 2>&1 || need+=(ffmpeg)
command -v deno >/dev/null 2>&1 || need+=(deno)
if (( ${#need} )) && command -v brew >/dev/null 2>&1; then
  say_step "Instalando ${need[*]} (solo la primera vez)…"
  brew install "${need[@]}" || print "Aviso: no se pudo instalar ${need[*]}."
fi

# 2. Python 3.10+ (se prefieren versiones con ruedas binarias para todo).
PY=""
for candidate in python3.13 python3.12 python3.11 python3.14 python3; do
  if command -v $candidate >/dev/null 2>&1 && $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY=$(command -v $candidate); break
  fi
done
if [[ -z "$PY" ]]; then
  command -v brew >/dev/null 2>&1 || pause_exit "Instala Python 3.12 desde python.org y vuelve a abrir este archivo."
  say_step "Instalando Python…"
  brew install python@3.12 && PY="$(brew --prefix)/bin/python3.12"
fi

# 3. Entorno virtual con las librerías (Pillow, OpenCV, yt-dlp y transcripción local).
if [[ ! -x .venv/bin/python ]]; then
  say_step "Creando el entorno de Python…"
  "$PY" -m venv .venv || pause_exit "No se pudo crear el entorno de Python."
fi
VPY=.venv/bin/python
stamp=.venv/.cortaclips-deps
want="$(cat requirements.txt requirements-local.txt 2>/dev/null | shasum | cut -c1-12)-$(uname -m)"
if [[ ! -f $stamp || "$(cat $stamp)" != "$want" ]]; then
  say_step "Instalando librerías (la primera vez tarda unos minutos)…"
  $VPY -m pip install -q --upgrade pip
  $VPY -m pip install -q -r requirements.txt || pause_exit "Falló la instalación de librerías. Revisa tu conexión y vuelve a intentarlo."
  if [[ "$(uname -m)" == "arm64" ]] && $VPY -m pip install -q mlx-whisper; then
    print "Transcripción local con la GPU de tu Mac: lista."
  else
    $VPY -m pip install -q -r requirements-local.txt && print "Transcripción local: lista." \
      || print "Aviso: sin transcripción local. Puedes usar una OPENAI_API_KEY."
  fi
  echo "$want" > $stamp
fi

# yt-dlp se actualiza cada día: YouTube cambia a menudo y una versión vieja es el fallo n.º 1.
if [[ -z "$(find .venv/.ytdlp-updated -mtime -1 2>/dev/null)" ]]; then
  $VPY -m pip install -q -U "yt-dlp[default]" && touch .venv/.ytdlp-updated
fi

[[ -f .env ]] || cp .env.example .env

say_step "Abriendo Corta Clips en tu navegador… (deja esta ventana abierta mientras lo usas)"
exec $VPY app.py --open
