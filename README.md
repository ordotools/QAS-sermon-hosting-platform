# QAS Sermon Hosting Platform

FastAPI + HTMX + PWA platform for sermon and media hosting. Public playback, authenticated uploads, superuser admin with invite links.

## Features

- Public chronological media list and HTML5 player
- Session-based auth (user / superuser roles)
- Invite-only registration
- Media upload with ffmpeg thumbnails and ffprobe metadata
- Range-aware stream proxy (no raw storage URLs exposed)
- PWA installability and save-for-offline playback
- Local storage (dev) or Backblaze B2 (prod)

## Local development

### Prerequisites

- Python 3.12+
- ffmpeg (for thumbnails/metadata)

### Setup

```bash
cp .env.example .env
# Edit SECRET_KEY, SUPERUSER_EMAIL, SUPERUSER_PASSWORD

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

mkdir -p data/media
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

Default superuser comes from `.env` (`SUPERUSER_EMAIL` / `SUPERUSER_PASSWORD`).

### Create users

1. Log in as superuser → **Admin**
2. Create an invite link and copy it
3. Share the link; recipient registers at `/register?token=...`

### Recommended media formats

- Video: MP4 (H.264), WebM
- Audio: MP3, M4A

## Docker (PostgreSQL)

```bash
cp .env.example .env
docker compose up --build
```

App: http://localhost:8000

Uses PostgreSQL + local volume storage by default. Set `STORAGE_BACKEND=b2` for Backblaze.

On startup the container runs `alembic upgrade head` before uvicorn (see `scripts/entrypoint.sh`).

## Backblaze B2 setup

1. Create a **private** bucket in Backblaze B2
2. Create an application key scoped to that bucket (read + write)
3. Set in `.env`:

```env
STORAGE_BACKEND=b2
B2_KEY_ID=your-key-id
B2_APP_KEY=your-app-key
B2_BUCKET=your-bucket-name
B2_ENDPOINT=https://s3.us-west-004.backblazeb2.com
```

Use the S3-compatible endpoint for your bucket region. Streams always go through `/stream/{id}` — never expose presigned B2 URLs.

## Production notes

- Set `DEBUG=false` for secure cookies
- Terminate TLS at nginx/Caddy (required for service workers)
- Set a strong `SECRET_KEY`
- Configure `MAX_UPLOAD_SIZE_MB` as needed
- Run with a single uvicorn worker (in-memory rate limits and background processing are per-process)

## PWA manual QA

Before production launch, complete the device checklist in [docs/PWA_QA.md](docs/PWA_QA.md) (iPad Safari + Chromebook Chrome over HTTPS).

## Tests

```bash
pytest
```

## Health check

`GET /health` → `{"status": "ok"}`

## Project layout

See [ROADMAP.md](ROADMAP.md) for phased implementation details.
