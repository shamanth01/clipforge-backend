# ClipForge server (FastAPI + ffmpeg)

Real video processing behind the existing ClipForge page. The same service serves the website and the API, so the
public site is simply your Render URL.

## Deploy on Render
1. Put this folder in a GitHub repository.
2. Render dashboard: New > Blueprint, pick the repo. `render.yaml` creates a Docker web service (Standard plan, 2 GB RAM).
3. Set `ANTHROPIC_API_KEY` when asked (needed for AI-written hook headlines; without it the hook falls back to the clip's opening line).
4. Deploy, then open the Render URL. Check `/healthz`: `ffmpeg` should be true, `captions` true after the image builds.

Free and Starter plans are too small for video rendering. Use Standard, or Pro for faster renders.

## Run locally with Docker
    docker build -t clipforge . && docker run -p 10000:10000 -e ANTHROPIC_API_KEY=... clipforge
    open http://localhost:10000

## How it works
Upload (raw streaming, 500 MB cap) or YouTube link > probe (60 min cap) > browser-playable preview > audio loudness + on-screen
motion analysis per second > best non-overlapping windows with a score > per clip: speech-to-text (faster-whisper) >
AI hook (Claude) > ASS captions + hook > ffmpeg render to 1080x1920 H.264/AAC MP4 (also 1:1 and 16:9) > download.

Endpoints: `POST /api/upload?filename=`, `POST /api/url`, `GET /api/jobs/{id}`, `GET /api/jobs/{id}/clips?length=&count=`,
`GET /api/jobs/{id}/preview`, `POST /api/jobs/{id}/render`, `GET /api/renders/{id}`, `GET /api/renders/{id}/download`, `GET /healthz`.

## Settings (environment variables)
| Name | Default | Meaning |
|---|---|---|
| MAX_UPLOAD_MB / MAX_MINUTES | 500 / 60 | Upload size and video length limits |
| FILE_TTL_MIN | 120 | Minutes before files are deleted |
| MAX_WORKERS | 2 | Simultaneous jobs |
| UPLOADS_PER_HOUR / RENDERS_PER_HOUR | 20 / 60 | Per-IP rate limits |
| YT_POLICY | cc_only | `cc_only` allows only Creative Commons YouTube videos, `open` allows any (you take responsibility), `off` disables links |
| YTDLP_COOKIES / YTDLP_PROXY | empty | Optional cookies file path or proxy, to reduce YouTube blocking of cloud servers |
| WHISPER_MODEL | base | faster-whisper model (tiny is faster, small is more accurate) |
| ANTHROPIC_API_KEY / HOOK_MODEL | empty / claude-haiku-5-5 | AI hook headlines |
| ENABLE_CAPTIONS | 1 | Set 0 to skip speech-to-text |
| CORS_ORIGINS | empty | Only needed if the page is hosted elsewhere; then set the page's `<meta name="clipforge-api">` to this server's URL |

## Known limits
- One instance only: jobs live in memory and files on local disk. Restarts lose in-progress jobs. Scaling out needs Redis and shared storage.
- YouTube often blocks cloud IPs. Uploads are the reliable path.
- Highlights come from audio loudness and motion, not understanding of the content.

## Tests
Start the server, then `python tests/e2e.py http://127.0.0.1:8000` (needs ffmpeg and `requests`). Stages: STAGES=A, B, C or D.
