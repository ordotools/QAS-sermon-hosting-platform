# QAS Sermon Hosting Platform

FastAPI + HTMX + PWA platform for sermon and media hosting. Public playback, authenticated uploads, superuser admin with invite links.

## Features

- Public chronological media list and HTML5 player
- Session-based auth (user / superuser roles)
- Invite-only registration
- Media upload with ffprobe validation, automatic transcoding, upload progress, and ffmpeg thumbnails
- Range-aware stream proxy (no raw storage URLs exposed)
- PWA installability and save-for-offline playback
- Local storage (dev) or Backblaze B2 (prod)

## Local development

### Prerequisites

- Python 3.12+
- ffmpeg (validation, transcoding, thumbnails, metadata)

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

### Supported media formats

Upload accepts `.mp4`, `.mov`, `.m4a`, `.mp3`, `.webm`, and `.ogg`. Files are validated with ffprobe on upload; invalid or non-media files are rejected before a record is created.

| Source | Typical file | Server action |
|--------|--------------|---------------|
| iPhone camera (default) | `.mov` (HEVC + AAC) | Transcode → H.264 MP4 + AAC |
| iPhone "Most Compatible" | `.mov` / `.mp4` (H.264 + AAC) | Passthrough (remux with faststart when needed) |
| iPhone Voice Memos | `.m4a` (AAC) | Passthrough |
| iPhone Voice Memos (Lossless) | `.m4a` (ALAC) | Transcode → AAC M4A |
| Common uploads | `.mp3` | Passthrough |
| WebM / OGG | `.webm` / `.ogg` | Transcode to H.264 MP4 or AAC M4A |

Rejected: images (`.heic`, `.jpg`, etc.), documents, archives, corrupt/unreadable files, and files with no audio or video stream.

iPhone video (`.mov`) and voice memos (`.m4a`) are supported; files are converted automatically for web playback.

**Upload UX:** a progress bar shows bytes reaching the server; after upload completes, the processing status poll shows transcoding and thumbnail generation (`processing` → `ready` / `failed`). Large iPhone 4K HEVC files may take several minutes to transcode.

## Docker (PostgreSQL)

```bash
cp .env.example .env
docker compose up --build
```

App: http://localhost:8000

Uses PostgreSQL + local volume storage by default. Set `STORAGE_BACKEND=b2` for Backblaze.

On startup the container runs `alembic upgrade head` before uvicorn (see `scripts/entrypoint.sh`).

## Coolify (Path A)

Deploy the app with a **separate** Coolify PostgreSQL database:

1. Create PostgreSQL in the same Coolify project/environment.
2. New resource → **Docker Compose** → set compose file to `docker-compose.coolify.yml`.
3. Enable **Connect to Predefined Network** on the service stack, then redeploy.
4. Set `DATABASE_URL` to the internal Postgres URL using the `postgresql+asyncpg://` driver (copy from the database page and swap the scheme).
5. Set `SECRET_KEY`, `SUPERUSER_EMAIL`, `SUPERUSER_PASSWORD`, and `DEBUG=false`.
6. Add a domain with HTTPS (required for the PWA).

Media files persist on the `media_data` volume. Use `STORAGE_BACKEND=b2` to store uploads in Backblaze instead.

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
- Run with a single uvicorn worker (in-memory rate limits and background transcoding/thumbnails are per-process; uploads queue behind each other)

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
