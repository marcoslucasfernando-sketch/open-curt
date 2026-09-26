# ✂ Corta Clips

Convierte vídeos y podcasts largos en **clips verticales listos para redes**: la IA (**GPT‑6 Luna**) encuentra los mejores momentos, la app los recorta en 9:16 **siguiendo las caras**, añade **subtítulos animados palabra a palabra** y los **publica en YouTube Shorts, TikTok e Instagram Reels** con OAuth.

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
| ↗ **Publicación en un clic** | Conecta YouTube, TikTok e Instagram iniciando sesión (conexión rápida), o con tus propias apps OAuth (avanzado). |
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

## Conectar redes sociales

### Conexión rápida (recomendada): inicias sesión y listo
1. Crea una cuenta gratuita en [Upload‑Post](https://app.upload-post.com/) con «Continuar con Google».
2. Ve a **API Keys**, crea una clave y pégala en **Ajustes → Redes**. Es lo único que se copia, y solo una vez.
3. Pulsa **Conectar** en YouTube, TikTok e Instagram. Se abre el inicio de sesión oficial de cada red:
   - **YouTube:** entras con tu cuenta de Google.
   - **TikTok:** puedes usar «Continuar con Google».
   - **Instagram:** Instagram no admite Google; entras con tu usuario de Instagram o con Facebook. La cuenta debe ser profesional (Creador o Empresa).

Corta Clips crea tu perfil de Upload‑Post automáticamente y muestra qué cuentas están conectadas. Upload‑Post ya tiene sus apps aprobadas por las tres redes, así que los vídeos se publican en público sin auditorías.

> Plan gratuito de Upload‑Post: 10 publicaciones al mes en YouTube e Instagram. Para publicar en TikTok necesitas un plan de pago.

### Avanzado: conexión directa con tus propias apps (gratis, más pasos)

Si prefieres no depender de un intermediario, crea **una vez** una app de desarrollador gratuita en cada red. En **Ajustes → Redes → Avanzado** tienes los pasos, la URL de redirección con botón de copiar y los campos para pegar las claves. Si una red está conectada de esta forma, se usa esta conexión en lugar de la rápida.

Estas URLs sirven para rellenar las fichas de las apps:

| Campo que piden | URL |
|---|---|
| Web / sitio | `https://corta-clips.netlify.app` |
| Política de privacidad | `https://corta-clips.netlify.app/privacidad` |
| Términos del servicio | `https://corta-clips.netlify.app/terminos` |
| Eliminación de datos (Meta) | `https://corta-clips.netlify.app/eliminacion-datos` |

#### YouTube Shorts
1. [Google Cloud Console](https://console.cloud.google.com/apis/credentials): crea un proyecto y activa **YouTube Data API v3**.
2. Configura la **pantalla de consentimiento OAuth** (tipo Externo), añade tu cuenta como usuario de prueba y pásala a **«En producción»**. Si la dejas en «Prueba», el acceso caduca cada 7 días.
3. **Crear credenciales → ID de cliente OAuth → «App de escritorio»**. Pega el ID y el secreto en la app.
4. Pulsa **Conectar**. Se usa la redirección `http://127.0.0.1:8766/oauth/callback`.

> ⚠️ Google deja como **privados** los vídeos subidos por API desde proyectos sin auditar. Puedes hacerlos públicos a mano en YouTube Studio o solicitar la [auditoría](https://support.google.com/youtube/contact/yt_api_form).

#### TikTok
1. [developers.tiktok.com](https://developers.tiktok.com/apps): crea una app y añade **Login Kit** y **Content Posting API**.
2. En Login Kit elige la plataforma **Desktop** y registra `http://127.0.0.1:8766/oauth/callback`.
3. Pide los permisos `user.info.basic`, `video.upload` y `video.publish`. Pega el Client Key y el Client Secret en la app.
4. Elige el modo en Ajustes → Redes:
   - **Borrador** (recomendado mientras la app no esté auditada): el vídeo llega a tu bandeja de TikTok y lo publicas desde el móvil, donde además puedes añadir música.
   - **Directo**: sin auditoría, TikTok solo permite «Solo yo».

#### Instagram Reels
1. Tu cuenta de Instagram debe ser **profesional** (Creador o Empresa).
2. [developers.facebook.com](https://developers.facebook.com/apps/): crea una app de tipo Empresa y añade **Instagram → API con inicio de sesión de Instagram**.
3. En «Configurar inicio de sesión para empresas» añade la redirección **`https://corta-clips.netlify.app/oauth/callback`**. Instagram exige HTTPS, así que esa página reenvía el código a tu ordenador (`127.0.0.1`) sin guardar nada.
4. En **Roles → Probadores de Instagram** añade tu cuenta y acepta la invitación en Instagram (Ajustes → Apps y sitios web).
5. Pega el **ID y la clave secreta de la app de Instagram** (no los de Facebook) y pulsa **Conectar**. Si no vuelve sola a la app, pega la URL final en «¿No volvió solo?».

---

## Problemas frecuentes

| Mensaje | Solución |
|---|---|
| «YouTube está pidiendo verificación» | Ajustes → Sistema → **Cookies del navegador**: elige Chrome o Safari. |
| «yt‑dlp no pudo leer YouTube» | Cierra y abre `iniciar.command` (actualiza yt‑dlp) e instala deno: `brew install deno`. |
| «Spotify protege el audio…» | Pega el enlace del episodio en YouTube, Apple Podcasts o el RSS, o sube el archivo. |
| «No hay ninguna IA conectada» | Ajustes → IA → Conectar con ChatGPT o pega tu API key. |
| «Tu cuenta de ChatGPT no permite usar gpt‑6‑luna en Codex» | Cambia el modelo en Ajustes → IA o usa una API key. |
| «No hay ningún motor de transcripción» | Abre `iniciar.command` (instala la transcripción local) o añade `OPENAI_API_KEY`. |
| TikTok: «Solo yo» | Tu app no está auditada: usa el modo **Borrador**. |
| Cualquier otro error | Pulsa **Registro técnico** en el panel del trabajo y comparte el texto. |

Diagnóstico completo: **Ajustes → Sistema**, o `python3 app.py --check`.

---

## Privacidad y seguridad

- La app escucha **solo en 127.0.0.1**, comprueba `Host` y `Origin` en cada petición (protección contra DNS rebinding y CSRF) y no sirve archivos fuera de la carpeta de cada trabajo.
- Claves en `.env` y tokens OAuth en `.data/tokens.json`, ambos con permisos `600` y excluidos de Git.
- OAuth con **PKCE** y parámetro `state` de un solo uso que caduca a los 20 minutos.
- La página de Netlify es estática: no guarda ni envía nada y solo redirige a `127.0.0.1`.

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
  social/               OAuth + publicación: youtube, tiktok, instagram, uploadpost
web/index.html          interfaz
assets/                 tipografías (OFL) y modelo YuNet (MIT)
site/ + netlify.toml    web pública en Netlify
tests/                  pruebas automáticas
```

## Pruebas

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Las pruebas cubren URLs y errores, transcripción, ajuste de cortes, encuadre, subtítulos, ajustes, el servidor (seguridad y subidas) y los flujos OAuth y de publicación de las tres redes con APIs simuladas.

Tipografías con licencia SIL Open Font License (ver `assets/fonts/OFL-*.txt`). Detector de caras YuNet con licencia MIT (`assets/models/LICENSE-yunet.txt`).
