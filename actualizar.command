#!/bin/zsh
# Actualiza Corta Clips a la última versión sin tocar tus claves, cuentas conectadas ni clips.
cd "${0:A:h}" || exit 1
URL="https://github.com/marcoslucasfernando-sketch/open-curt/archive/refs/heads/claude/eager-archimedes-5n5qdb.zip"
tmp=$(mktemp -d)
print -P "\n%F{green}✂%f  %BDescargando la última versión…%b"
curl -fsSL -o "$tmp/cc.zip" "$URL" || { print "No se pudo descargar. Revisa tu conexión."; read -k1; exit 1; }
unzip -q "$tmp/cc.zip" -d "$tmp" || { print "El archivo descargado está dañado."; read -k1; exit 1; }
src=$(find "$tmp" -maxdepth 1 -type d -name 'open-curt-*' | head -1)
# Cierra la versión anterior si está abierta.
old=$(lsof -ti tcp:8766 2>/dev/null)
[[ -n "$old" ]] && kill $old 2>/dev/null && sleep 1
# Copia el código nuevo. .env, .data, outputs y .venv no vienen en la descarga, así que se conservan.
rsync -a "$src/" ./
rm -rf "$tmp"
chmod +x iniciar.command actualizar.command
print -P "%F{green}✓%f  Actualizado. Abriendo Corta Clips…"
exec ./iniciar.command
