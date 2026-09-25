# Corta Clips

Herramienta local para pegar un enlace de vídeo y obtener clips verticales MP4 con subtítulos. Usa `whisper-1` para transcribir y **`gpt-6-luna`** para elegir los momentos y escribir los títulos. Puedes ajustar cada corte y enviarlo automáticamente a una cola de publicación para YouTube Shorts, TikTok e Instagram Reels mediante Upload-Post. La puntuación editorial no predice viralidad.

## Requisitos

- macOS con Python 3.10 o posterior y Swift (incluido con las herramientas de desarrollo de Apple).
- `ffmpeg`, `ffprobe` y `yt-dlp` en el PATH. Por ejemplo: `brew install ffmpeg yt-dlp`.
- Una clave de la API de OpenAI con acceso a los modelos. La suscripción de ChatGPT/Codex no equivale a una clave de API. La transcripción y Luna se facturan según el uso.
- Para publicar: una cuenta [Upload-Post](https://www.upload-post.com/) con YouTube, TikTok e Instagram conectadas, una API key y el identificador de perfil (`user`). El servicio puede tener coste propio.

## Uso

La opción más sencilla en macOS es copiar `.env.example` a `.env`, rellenar los valores y abrir `iniciar.command`. `.env` está excluido de Git. También puedes arrancar desde Terminal:

```bash
cd "/Users/marcoslucasfernando/Documents/ChatGPT/corta cliips"
export OPENAI_API_KEY='tu_clave_aqui'
export UPLOAD_POST_API_KEY='tu_clave_upload_post'  # opcional
export UPLOAD_POST_USER='tu_perfil'                 # opcional
python3 app.py
```

Abre `http://127.0.0.1:8766`, pega un enlace público (YouTube y otros sitios admitidos por `yt-dlp`), elige el número y la duración de clips y pulsa **Crear clips**. Marca la casilla de publicación automática para añadir cada resultado a la próxima franja disponible de la cola de Upload-Post. También puedes enviar la tanda después de revisarla. Los resultados se guardan en `outputs/<id>/` junto con `manifest.json`, `transcript.json` y, cuando corresponda, `publications.json`.

Los vídeos privados, protegidos por DRM o que `yt-dlp` no pueda descargar darán un error. El encuadre vertical conserva el vídeo completo sobre un fondo desenfocado; no hace seguimiento de caras. Los subtítulos se basan en segmentos de transcripción, por lo que conviene revisarlos antes de publicar. La aceptación de un envío por Upload-Post no prueba que el post esté publicado: usa **Estado de publicación** y revisa los resultados por plataforma. La app desactiva la alternativa de mandar TikTok a borradores cuando se agota su cupo de publicación directa; en ese caso el envío falla y se informa.

La app escucha solo en `127.0.0.1`. No guardes la clave en el repositorio.

## Verificación local

```bash
python3 -m unittest discover -s tests -v
```
