# QAS Sermon Hosting Platform — ROADMAP

Phased implementation checklist. Public read for media; auth required for upload/admin.

## Phase 0 — Scaffold

- [x] Init Python project (`pyproject.toml`), FastAPI app, Jinja2 base template
- [x] HTMX CDN wired; minimal CSS with touch targets
- [x] SQLite + SQLModel + Alembic
- [x] `LocalStorage` backend + `./data/media/`
- [x] Docker Compose (app + postgres optional; sqlite ok for earliest dev)
- [x] `.env.example`, `.gitignore`

## Phase 1 — Auth and invites

- [x] User model with `role: user | superuser`
- [x] Login/logout (session cookie)
- [x] Superuser bootstrap from env
- [x] Invite token CRUD (superuser creates link; register consumes token)
- [x] Route guards: `require_user`, `require_superuser`

## Phase 2 — Media upload and processing

- [x] `MediaItem` model + migrations
- [x] Upload form (title, published date, file)
- [x] Background thumbnail + ffprobe metadata
- [x] Superuser + user can upload; processing status in UI
- [x] ffprobe validation on upload (reject invalid/non-media before DB record)
- [x] Automatic transcoding (HEVC/ALAC/WebM/OGG → H.264 MP4 or AAC M4A)
- [x] Upload progress bar (XHR + server JSON response)
- [x] Resumable/chunked uploads (tus) for large iPhone videos
- [x] Eager background upload on file select; parallel tus chunks via concatenation; commit on submit

## Phase 3 — Public viewer

- [x] Chronological list at `/` (newest `published_at` first)
- [x] Player page with HTML5 `<video>` / `<audio>`
- [x] Stream endpoint with range requests
- [x] Player hardening (nodownload, no context menu)

## Phase 4 — Admin panel

- [x] `/admin` dashboard (superuser only)
- [x] User list: deactivate/delete
- [x] Invite management: create, revoke, copy link
- [x] Media delete (any item for superuser)

## Phase 5 — PWA installability

- [x] Web app manifest + icons
- [x] Service worker: precache shell
- [x] iOS/Android install meta tags
- [ ] Verify Add to Home Screen on iPad + Chromebook (manual QA — see [docs/PWA_QA.md](docs/PWA_QA.md))

## Phase 6 — Backblaze B2 + production Docker

- [x] `B2Storage` via boto3 S3-compatible API
- [x] Storage backend switch via env
- [x] PostgreSQL in compose
- [x] Production Dockerfile (ffmpeg included)
- [x] Document Backblaze bucket setup in README

## Phase 7 — Offline playback

- [x] "Save for offline" action on player/list
- [x] SW caches media from stream endpoint
- [x] Offline media list + playback without network
- [x] Storage quota handling + "Remove offline"

## Phase 8 — Polish and hardening

- [x] Rate limiting on upload/login
- [x] Upload size limits (env configurable)
- [x] Basic pytest coverage (auth, stream ranges, upload)
- [x] Health check endpoint for Docker
- [x] README: local dev, Docker deploy, Backblaze setup

## Out of scope (v1)

- DRM / encrypted streams
- Email delivery of invites
- Full-text search, tags, playlists
- Multiple organizations/tenants

## Playback posture

True download prevention is impossible on the web. Mitigations: proxy streams (no presigned URLs), `controlsList="nodownload"`, no download UI, offline via SW cache only.
