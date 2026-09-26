# ✂ Corta Clips

Convierte vídeos y podcasts largos en **clips verticales listos para redes**: la IA (**GPT‑6 Luna**) encuentra los mejores momentos, la app los recorta en 9:16 **siguiendo las caras**, añade **subtítulos animados palabra a palabra** y los **sube a tus canales de YouTube** con la API oficial.

Todo se ejecuta en tu ordenador. Web pública (privacidad, términos y retorno OAuth): **https://corta-clips.netlify.app**

---

## Qué hace

| | |
|---|---|
| 🎙️ **Cualquier fuente** | YouTube, Apple Podcasts, RSS, Twitch, Vimeo, X… (todo lo que soporta yt‑dlp) o **tus archivos** (arrastrar y soltar, hasta 8 GB). Enlaces de Spotify: busca el mismo episodio en YouTube, porque Spotify cifra el audio. |
| 🎧 **Podcasts sin vídeo** | Se convierten en **audiogramas**: carátula con esquinas redondeadas, fondo difuminado, onda de audio animada, título del episodio y subtítulos. |
| ✦ **GPT‑6 Luna** | Elige los momentos con criterios de editor (gancho en 2 s, historia autónoma, remate) y escribe título, gancho, descripción y hashtags. Conexión con **tu cuenta de ChatGPT (OAuth, sin API key)** o con **API key**. Claude también sirve como alternativa. |
| ✂️ **Cortes limpios** | Cada clip empieza y acaba en **frases completas**: nada de «…ciencia. Yo» colgando. |
| 🎯 **Encuadre inteligente** | Detecta caras (OpenCV YuNet) y recorta a vertical siguiéndolas, sin temblores. Si no hay caras, usa el vídeo entero sobre un fondo difuminado. |
| 🔤 **Subtítulos con estilo** | Karaoke, caja o limpio; 7 tipografías (Bricolage, Poppins, Anton, Montserrat, Unbounded, Bebas Neue, Archivo Black) y 5 colores. Además, archivo `.srt` de cada clip. |
| 🔊 **Sonido de redes** | Volumen normalizado a −14 LUFS. |
| ↗ **Subida a YouTube** | API oficial de YouTube con tu ID de cliente y secreto. Varios canales de la misma cuenta; eliges a cuál sube cada clip. |
| 🛠️ **Edición** | Ajusta inicio y fin (con «empezar/terminar aquí» desde el reproductor), edita títulos y textos, vuelve a renderizar o pide **otros momentos** sin volver a transcribir. |
| 🧾 **Errores claros** | Cada fallo dice qué pasó y cómo arreglarlo, con registro técnico descargable y botón **Reintentar**, que reaprovecha lo ya descargado y transcrito. |

### ¿Por qué fallaba con tu podcast?

La versión anterior tenía varios problemas que rompían justo con podcasts:

- Solo aceptaba archivos de **vídeo**: un podcast solo de audio (MP3/M4A, Apple Podcasts, RSS) terminaba en «La descarga terminó sin un archivo de vídeo legible».
- Rechazaba episodios de **más de 2 horas** y transcripciones largas.
- Los subtítulos dependían de un script **Swift** que se compilaba en cada clip y suele romperse tras actualizar macOS o Xcode.
- Necesitaba sí o sí una **API key de OpenAI**.

Todo eso está resuelto: audio sin vídeo → audiograma, hasta 6 horas, subtítulos con Pillow (sin Swift) y transcripción local gratis.

---

## Empezar (Mac)

1. Descarga o clona este repositorio.
2. Haz **doble clic en `iniciar.command`**. Si macOS lo bloquea: clic derecho → *Abrir* → *Abrir*.
   - La primera vez instala lo que falte (ffmpeg, deno, Python y librerías) y tarda unos minutos.
   - Cada día actualiza yt‑dlp, porque YouTube cambia a menudo.
3. Se abre **http://127.0.0.1:8766**. Deja la ventana de Terminal abierta mientras usas la app.
4. En **⚙︎ Ajustes → IA** conecta GPT‑6 Luna (ver abajo).
5. Pega un enlace o arrastra un archivo y pulsa **Crear clips**.

> ¿Linux o Windows? Instala Python 3.10+ y ffmpeg, y ejecuta:
> `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-local.txt && .venv/bin/python app.py --open`

---

## Conectar GPT‑6 Luna

En **Ajustes → IA** tienes dos formas de conectar:

**A. Con tu cuenta de ChatGPT (OAuth, sin API key).** Usa los límites de tu plan Plus/Pro/Business a través de Codex CLI, la herramienta oficial de OpenAI.
1. Instala Codex una vez: `npm install -g @openai/codex` (o `brew install --cask codex`).
2. Pulsa **Conectar con ChatGPT**: se abre Terminal y el navegador para autorizar.
3. Pulsa **Probar**.

**B. Con API key** (pago por uso, muy barato con Luna). Pega tu `OPENAI_API_KEY` y pulsa **Probar**. Con la key también puedes transcribir rápido con Whisper (~0,006 $/min).

En **Automático** se usa lo primero que esté conectado, en este orden: API → ChatGPT → Claude. Puedes cambiar el modelo (por ejemplo `gpt-6-sol`) y el nivel de razonamiento.

**Transcripción**: por defecto es **local y gratuita**, con mlx‑whisper en Macs con chip M o faster‑whisper en el resto. El modelo se descarga la primera vez. Si hay API key, puedes elegir Whisper de OpenAI, que es más rápido.

---

## Subir a YouTube (uno o varios canales)

Corta Clips sube los clips con la **API oficial de YouTube**, usando tu propio **ID de cliente y secreto** de Google.

**Paso único (5 minutos, gratis).** En **Ajustes → YouTube → «Cómo conseguirlos»** tienes cada paso con su enlace directo:
1. [Crea un proyecto](https://console.cloud.google.com/projectcreate) en Google Cloud (por ejemplo «Corta Clips»).
2. [Activa «YouTube Data API v3»](https://console.cloud.google.com/apis/library/youtube.googleapis.com).
3. [Configura la pantalla de acceso](https://console.cloud.google.com/auth/branding): nombre de la app, tu correo y usuarios «Externo».
4. En [Público](https://console.cloud.google.com/auth/audience) añade tu Gmail como usuario de prueba y pulsa **Publicar aplicación**. Si no, el acceso caduca cada 7 días.
5. [Crea un cliente](https://console.cloud.google.com/auth/clients/create) de tipo **Aplicación de escritorio**. Copia el ID de cliente y el secreto en la app, o descarga su JSON y pulsa **Importar JSON de Google**.

**Conectar tus canales**
- Pulsa **Iniciar sesión con Google** y elige el canal.
- **¿Tienes dos canales en la misma cuenta** (el personal y uno de marca, por ejemplo)? Pulsa **Añadir otro canal** y, cuando Google pregunte, elige el segundo. Puedes conectar todos los que quieras y marcar uno como **canal por defecto**.
- Al subir un clip eliges **a qué canal va** y su visibilidad (público, oculto o privado). Puedes subir el mismo clip a los dos canales.
- La primera vez Google avisa de que la app no está verificada: pulsa **Configuración avanzada → Ir a Corta Clips**. Es tu propia app.

> ⚠️ Mientras Google no audite tu proyecto, YouTube deja como **privados** los vídeos subidos por API. La [auditoría](https://support.google.com/youtube/contact/yt_api_form) es gratuita.

Para TikTok e Instagram: descarga los clips (botón **Descargar todo (.zip)**) y súbelos desde el móvil; ya van en formato vertical con subtítulos.

## Problemas frecuentes

| Mensaje | Solución |
|---|---|
| «YouTube está pidiendo verificación» | Ajustes → Sistema → **Cookies del navegador**: elige Chrome o Safari. |
| «yt‑dlp no pudo leer YouTube» | Cierra y abre `iniciar.command` (actualiza yt‑dlp) e instala deno: `brew install deno`. |
| «Spotify protege el audio…» | Pega el enlace del episodio en YouTube, Apple Podcasts o el RSS, o sube el archivo. |
| «No hay ninguna IA conectada» | Ajustes → IA → Conectar con ChatGPT o pega tu API key. |
| «Tu cuenta de ChatGPT no permite usar gpt‑6‑luna en Codex» | Cambia el modelo en Ajustes → IA o usa una API key. |
| «No hay ningún motor de transcripción» | Abre `iniciar.command` (instala la transcripción local) o añade `OPENAI_API_KEY`. |
| El vídeo sale como privado en YouTube | Tu proyecto de Google no está auditado: solicita la auditoría (gratuita). |
| «Ese ID de cliente no parece de Google» | Copia el ID completo, que termina en `.apps.googleusercontent.com`. |
| Cualquier otro error | Pulsa **Registro técnico** en el panel del trabajo y comparte el texto. |

Diagnóstico completo: **Ajustes → Sistema**, o `python3 app.py --check`.

---

## Privacidad y seguridad

- La app escucha **solo en 127.0.0.1**, comprueba `Host` y `Origin` en cada petición (protección contra DNS rebinding y CSRF) y no sirve archivos fuera de la carpeta de cada trabajo.
- Claves en `.env` y accesos de cada canal en `.data/tokens.json`, ambos con permisos `600` y excluidos de Git.
- OAuth con **PKCE** y parámetro `state` de un solo uso que caduca a los 20 minutos.

---

## Estructura

```
app.py                  arranque (python3 app.py [--open] [--check])
iniciar.command         instalador + lanzador para macOS
cortaclips/
  server.py             servidor HTTP local y API
  pipeline.py           cola de trabajos, etapas, reintentos, re-render
  sources.py            yt-dlp, Spotify, archivos subidos
  transcribe.py         Whisper API / mlx-whisper / faster-whisper, frases y puntuación
  brain.py              GPT-6 Luna (API y ChatGPT/Codex), Claude, ajuste de cortes
  captions.py           subtítulos karaoke, gancho, cabecera, carátula (Pillow)
  framing.py            detección de caras y plan de encuadre
  render.py             composición ffmpeg (caras, difuminado, audiograma)
  social/               YouTube: OAuth con ID de cliente y secreto, varios canales, subida
web/index.html          interfaz
assets/                 tipografías (OFL) y modelo YuNet (MIT)
site/ + netlify.toml    web pública en Netlify (presentación, privacidad y términos)
tests/                  pruebas automáticas
```

## Pruebas

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Las pruebas cubren URLs y errores, transcripción, ajuste de cortes, encuadre, subtítulos, ajustes, el servidor (seguridad y subidas) el inicio de sesión con Google, varios canales y la subida a YouTube con la API simulada.

Tipografías con licencia SIL Open Font License (ver `assets/fonts/OFL-*.txt`). Detector de caras YuNet con licencia MIT (`assets/models/LICENSE-yunet.txt`).
