import base64
from urllib.parse import urlparse

import pytest
from fastapi import BackgroundTasks
from sqlmodel import select

from app.models import MediaItem, MediaStatus, MediaType
from app.routers.tus import decode_upload_metadata
from app.services.media import process_media
from app.services.media_formats import MediaProbe


def _meta(**fields: str) -> str:
    parts = []
    for key, value in fields.items():
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        parts.append(f"{key} {encoded}")
    return ",".join(parts)


def _tus_headers(**extra: str) -> dict[str, str]:
    headers = {"Tus-Resumable": "1.0.0"}
    headers.update(extra)
    return headers


def _upload_url(response) -> str:
    location = response.headers.get("location")
    assert location
    path = urlparse(location).path
    assert path.startswith("/files/")
    return path


AUDIO_PROBE = MediaProbe(
    media_type=MediaType.audio,
    video_codec=None,
    audio_codec="mp3",
    duration=1.0,
    container="mp3",
    has_video=False,
    has_audio=True,
    probe_ok=True,
)


def test_decode_upload_metadata():
    header = _meta(filename="talk.mp3", title="Sunday")
    decoded = decode_upload_metadata(header)
    assert decoded["filename"] == "talk.mp3"
    assert decoded["title"] == "Sunday"


@pytest.mark.asyncio
async def test_tus_create_requires_auth(client):
    r = await client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": "8",
                "Upload-Metadata": _meta(filename="talk.mp3", title="Sunday"),
            }
        ),
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tus_options_public(client):
    r = await client.options("/files")
    assert r.status_code == 204
    assert r.headers.get("tus-resumable") == "1.0.0"
    assert r.headers.get("tus-extension") == "creation,termination"


@pytest.mark.asyncio
async def test_tus_rejects_disallowed_extension(auth_client):
    r = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": "8",
                "Upload-Metadata": _meta(filename="notes.pdf", title="Doc"),
            }
        ),
    )
    assert r.status_code == 400
    assert "Unsupported file type" in r.json()["error"]


@pytest.mark.asyncio
async def test_tus_rejects_oversized_length(auth_client):
    r = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": str(501 * 1024 * 1024),
                "Upload-Metadata": _meta(filename="talk.mp3", title="Sunday"),
            }
        ),
    )
    assert r.status_code == 413
    assert "exceeds" in r.json()["error"]


@pytest.mark.asyncio
async def test_tus_get_is_not_download(auth_client):
    r = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": "8",
                "Upload-Metadata": _meta(filename="talk.mp3", title="Sunday"),
            }
        ),
    )
    assert r.status_code == 201
    path = _upload_url(r)
    got = await auth_client.get(path)
    assert got.status_code == 405


@pytest.mark.asyncio
async def test_tus_other_user_cannot_resume(client):
    from app import database
    from app.deps import SESSION_COOKIE, create_session_token
    from app.services.auth import get_user_by_email

    async with database.async_session() as session:
        user = await get_user_by_email(session, "user@test.com")
        admin = await get_user_by_email(session, "admin@test.com")
    user_headers = _tus_headers()
    user_headers["Cookie"] = f"{SESSION_COOKIE}={create_session_token(user.id)}"
    admin_headers = _tus_headers()
    admin_headers["Cookie"] = f"{SESSION_COOKIE}={create_session_token(admin.id)}"

    created = await client.post(
        "/files",
        headers={
            **user_headers,
            "Upload-Length": "8",
            "Upload-Metadata": _meta(filename="talk.mp3", title="Sunday"),
        },
    )
    assert created.status_code == 201
    path = _upload_url(created)

    r = await client.head(path, headers=admin_headers)
    assert r.status_code == 404
    r = await client.patch(
        path,
        content=b"abcd",
        headers={
            **admin_headers,
            "Upload-Offset": "0",
            "Content-Type": "application/offset+octet-stream",
        },
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_tus_chunked_resume_creates_media(
    auth_client, session, monkeypatch
):
    monkeypatch.setattr("app.routers.upload.probe_media", lambda _path: AUDIO_PROBE)
    monkeypatch.setattr("app.services.media.probe_media", lambda _path: AUDIO_PROBE)

    pending: list[tuple] = []

    def capture_task(self, func, *args, **kwargs):
        pending.append((func, args, kwargs))

    monkeypatch.setattr(BackgroundTasks, "add_task", capture_task)

    payload = b"abcdefgh"
    created = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": str(len(payload)),
                "Upload-Metadata": _meta(
                    filename="talk.mp3",
                    filetype="audio/mpeg",
                    title="Sunday talk",
                ),
            }
        ),
    )
    assert created.status_code == 201
    path = _upload_url(created)

    first = await auth_client.patch(
        path,
        content=payload[:4],
        headers=_tus_headers(
            **{
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            }
        ),
    )
    assert first.status_code == 204
    assert first.headers.get("upload-offset") == "4"
    assert not pending

    head = await auth_client.head(path, headers=_tus_headers())
    assert head.status_code == 204
    assert head.headers.get("upload-offset") == "4"

    rest = await auth_client.patch(
        path,
        content=payload[4:],
        headers=_tus_headers(
            **{
                "Upload-Offset": "4",
                "Content-Type": "application/offset+octet-stream",
            }
        ),
    )
    assert rest.status_code == 204
    assert rest.headers.get("upload-offset") == "8"
    media_id = rest.headers.get("x-media-id")
    assert media_id
    assert len(pending) == 1
    _, args, _ = pending[0]
    await process_media(session, args[0], args[1])

    result = await session.execute(select(MediaItem).where(MediaItem.id == int(media_id)))
    item = result.scalar_one()
    assert item.title == "Sunday talk"
    assert item.status == MediaStatus.ready
    assert item.media_type == MediaType.audio


@pytest.mark.asyncio
async def test_tus_offset_mismatch_conflicts(auth_client):
    created = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": "8",
                "Upload-Metadata": _meta(filename="talk.mp3", title="Sunday"),
            }
        ),
    )
    path = _upload_url(created)
    r = await auth_client.patch(
        path,
        content=b"abcd",
        headers=_tus_headers(
            **{
                "Upload-Offset": "2",
                "Content-Type": "application/offset+octet-stream",
            }
        ),
    )
    assert r.status_code == 409
    assert r.headers.get("upload-offset") == "0"


@pytest.mark.asyncio
async def test_tus_rejects_garbage_on_complete(auth_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.upload.probe_media",
        lambda _path: MediaProbe(
            media_type=None,
            video_codec=None,
            audio_codec=None,
            duration=None,
            container=None,
            has_video=False,
            has_audio=False,
            probe_ok=False,
        ),
    )
    payload = b"not-valid-media-bytes"
    created = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": str(len(payload)),
                "Upload-Metadata": _meta(filename="talk.mp3", title="Bad"),
            }
        ),
    )
    path = _upload_url(created)
    r = await auth_client.patch(
        path,
        content=payload,
        headers=_tus_headers(
            **{
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            }
        ),
    )
    assert r.status_code == 400
    assert "Could not read file" in r.json()["error"]


@pytest.mark.asyncio
async def test_upload_page_includes_tus(auth_client):
    r = await auth_client.get("/upload")
    assert r.status_code == 200
    assert "/static/vendor/tus/tus.min.js" in r.text
