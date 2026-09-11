import asyncio
import base64
from urllib.parse import urlparse

import pytest
from sqlmodel import select

from app.models import MediaItem, MediaStatus, MediaType
from app.routers.tus import _read_patch_bytes, decode_upload_metadata
from app.services.media import process_media
from app.services.media_formats import MediaProbe
from app.services.tus_store import load


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


def _uid(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _capture_processing(monkeypatch) -> list[tuple[int, str]]:
    pending: list[tuple[int, str]] = []

    def capture(media_id: int, temp_path: str) -> None:
        pending.append((media_id, temp_path))

    monkeypatch.setattr("app.routers.upload.schedule_processing", capture)
    monkeypatch.setattr("app.routers.tus.schedule_processing", capture)
    return pending


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
    assert r.headers.get("tus-extension") == "creation,termination,concatenation"


@pytest.mark.asyncio
async def test_tus_create_without_title(auth_client):
    r = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": "8",
                "Upload-Metadata": _meta(filename="talk.mp3"),
            }
        ),
    )
    assert r.status_code == 201
    assert r.headers.get("location")


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


async def _patch(client, path: str, data: bytes, offset: int):
    return await client.patch(
        path,
        content=data,
        headers=_tus_headers(
            **{
                "Upload-Offset": str(offset),
                "Content-Type": "application/offset+octet-stream",
            }
        ),
    )


@pytest.mark.asyncio
async def test_tus_chunked_resume_creates_media(auth_client, session, monkeypatch):
    monkeypatch.setattr("app.routers.upload.probe_media", lambda _path: AUDIO_PROBE)
    monkeypatch.setattr("app.services.media.probe_media", lambda _path: AUDIO_PROBE)

    pending = _capture_processing(monkeypatch)

    payload = b"abcdefgh"
    created = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": str(len(payload)),
                "Upload-Metadata": _meta(
                    filename="talk.mp3",
                    filetype="audio/mpeg",
                ),
            }
        ),
    )
    assert created.status_code == 201
    path = _upload_url(created)

    first = await _patch(auth_client, path, payload[:4], 0)
    assert first.status_code == 204
    assert first.headers.get("upload-offset") == "4"
    assert not pending
    assert first.headers.get("x-media-id") is None

    head = await auth_client.head(path, headers=_tus_headers())
    assert head.status_code == 204
    assert head.headers.get("upload-offset") == "4"

    rest = await _patch(auth_client, path, payload[4:], 4)
    assert rest.status_code == 204
    assert rest.headers.get("upload-offset") == "8"
    assert rest.headers.get("x-media-id") is None
    assert not pending

    incomplete = await auth_client.post(
        f"{path}/commit",
        json={"title": "Sunday talk"},
    )
    assert incomplete.status_code == 204
    media_id = incomplete.headers.get("x-media-id")
    assert media_id
    assert len(pending) == 1
    media_id_arg, temp_path = pending[0]
    await process_media(session, media_id_arg, temp_path)

    result = await session.execute(select(MediaItem).where(MediaItem.id == int(media_id)))
    item = result.scalar_one()
    assert item.title == "Sunday talk"
    assert item.status == MediaStatus.ready
    assert item.media_type == MediaType.audio


@pytest.mark.asyncio
async def test_tus_commit_returns_before_processing(auth_client, monkeypatch):
    monkeypatch.setattr("app.routers.upload.probe_media", lambda _path: AUDIO_PROBE)

    blocked = asyncio.Event()
    started = asyncio.Event()

    async def hang(_media_id: int, _temp_path: str) -> None:
        started.set()
        await blocked.wait()

    monkeypatch.setattr("app.routers.upload.run_processing", hang)

    payload = b"abcdefgh"
    created = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": str(len(payload)),
                "Upload-Metadata": _meta(filename="talk.mp3", filetype="audio/mpeg"),
            }
        ),
    )
    path = _upload_url(created)
    patched = await _patch(auth_client, path, payload, 0)
    assert patched.status_code == 204

    try:
        commit = await asyncio.wait_for(
            auth_client.post(f"{path}/commit", json={"title": "Sunday talk"}),
            timeout=2,
        )
        assert commit.status_code == 204
        assert commit.headers.get("x-media-id")
        await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        blocked.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_tus_commit_incomplete_conflicts(auth_client):
    created = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": "8",
                "Upload-Metadata": _meta(filename="talk.mp3"),
            }
        ),
    )
    path = _upload_url(created)
    await _patch(auth_client, path, b"abcd", 0)
    r = await auth_client.post(f"{path}/commit", json={"title": "Sunday"})
    assert r.status_code == 409
    assert "not complete" in r.json()["error"]


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

    ok = await _patch(auth_client, path, b"abcdefgh", 0)
    assert ok.status_code == 204
    assert ok.headers.get("upload-offset") == "8"


class _FakePatchRequest:
    def __init__(self, messages: list[dict], content_length: int | None = None):
        self._messages = list(messages)
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)

    async def receive(self):
        if not self._messages:
            raise AssertionError("receive called after body was fully drained")
        return self._messages.pop(0)


@pytest.mark.asyncio
async def test_read_patch_bytes_drains_trailing_more_body():
    body = b"x" * 256
    req = _FakePatchRequest(
        [
            {"type": "http.request", "body": body, "more_body": True},
            {"type": "http.request", "body": b"", "more_body": False},
        ],
        content_length=256,
    )
    assert await _read_patch_bytes(req, 256) == body
    assert req._messages == []


@pytest.mark.asyncio
async def test_read_patch_bytes_drains_before_rejecting_oversize():
    req = _FakePatchRequest(
        [
            {"type": "http.request", "body": b"a" * 100, "more_body": True},
            {"type": "http.request", "body": b"b" * 100, "more_body": False},
        ],
        content_length=200,
    )
    with pytest.raises(ValueError, match="too large"):
        await _read_patch_bytes(req, 50)
    assert req._messages == []


@pytest.mark.asyncio
async def test_tus_parallel_patches_complete(auth_client):
    payload = b"abcdefgh"
    paths = []
    for _ in range(4):
        created = await auth_client.post(
            "/files",
            headers=_tus_headers(
                **{
                    "Upload-Length": str(len(payload)),
                    "Upload-Concat": "partial",
                }
            ),
        )
        assert created.status_code == 201
        paths.append(_upload_url(created))

    results = await asyncio.gather(*[_patch(auth_client, path, payload, 0) for path in paths])
    for patched in results:
        assert patched.status_code == 204
        assert patched.headers.get("upload-offset") == str(len(payload))


@pytest.mark.asyncio
async def test_tus_parallel_large_patches_concat_commit(auth_client, session, monkeypatch):
    monkeypatch.setattr("app.routers.upload.probe_media", lambda _path: AUDIO_PROBE)
    monkeypatch.setattr("app.services.media.probe_media", lambda _path: AUDIO_PROBE)

    pending = _capture_processing(monkeypatch)

    part_size = 256 * 1024
    payloads = [bytes([i]) * part_size for i in range(4)]
    created = []
    for payload in payloads:
        res = await auth_client.post(
            "/files",
            headers=_tus_headers(
                **{
                    "Upload-Length": str(len(payload)),
                    "Upload-Concat": "partial",
                }
            ),
        )
        assert res.status_code == 201
        created.append(res)

    results = await asyncio.gather(
        *[
            _patch(auth_client, _upload_url(res), payload, 0)
            for res, payload in zip(created, payloads, strict=True)
        ]
    )
    for patched in results:
        assert patched.status_code == 204
        assert patched.headers.get("upload-offset") == str(part_size)

    final = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Concat": "final;" + " ".join(res.headers["location"] for res in created),
                "Upload-Metadata": _meta(filename="talk.mp3", filetype="audio/mpeg"),
            }
        ),
    )
    assert final.status_code == 201
    path = _upload_url(final)
    assert final.headers.get("upload-offset") == str(part_size * 4)
    assert final.headers.get("upload-length") == str(part_size * 4)
    for res in created:
        assert load(_uid(res.headers["location"].rstrip("/"))) is None

    commit = await auth_client.post(f"{path}/commit", json={"title": "Sunday talk"})
    assert commit.status_code == 204
    media_id = commit.headers.get("x-media-id")
    assert media_id
    media_id_arg, temp_path = pending[0]
    await process_media(session, media_id_arg, temp_path)

    result = await session.execute(select(MediaItem).where(MediaItem.id == int(media_id)))
    item = result.scalar_one()
    assert item.title == "Sunday talk"
    assert item.file_size == part_size * 4


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
    r = await _patch(auth_client, path, payload, 0)
    assert r.status_code == 204
    commit = await auth_client.post(f"{path}/commit", json={"title": "Bad"})
    assert commit.status_code == 400
    assert "Could not read file" in commit.json()["error"]


@pytest.mark.asyncio
async def test_tus_concat_then_commit(auth_client, session, monkeypatch):
    monkeypatch.setattr("app.routers.upload.probe_media", lambda _path: AUDIO_PROBE)
    monkeypatch.setattr("app.services.media.probe_media", lambda _path: AUDIO_PROBE)

    pending = _capture_processing(monkeypatch)

    payload = b"abcdefgh"
    parts = []
    for chunk in (payload[:4], payload[4:]):
        created = await auth_client.post(
            "/files",
            headers=_tus_headers(
                **{
                    "Upload-Length": str(len(chunk)),
                    "Upload-Concat": "partial",
                }
            ),
        )
        assert created.status_code == 201
        path = _upload_url(created)
        head = await auth_client.head(path, headers=_tus_headers())
        assert head.headers.get("upload-concat") == "partial"
        patched = await _patch(auth_client, path, chunk, 0)
        assert patched.status_code == 204
        parts.append(created.headers["location"])

    final = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Concat": "final;" + " ".join(parts),
                "Upload-Metadata": _meta(filename="talk.mp3", filetype="audio/mpeg"),
            }
        ),
    )
    assert final.status_code == 201
    path = _upload_url(final)
    assert final.headers.get("upload-offset") == "8"
    assert final.headers.get("upload-length") == "8"
    assert (final.headers.get("upload-concat") or "").startswith("final;")
    assert load(_uid(parts[0].rstrip("/"))) is None
    assert load(_uid(parts[1].rstrip("/"))) is None

    commit = await auth_client.post(f"{path}/commit", json={"title": "Sunday talk"})
    assert commit.status_code == 204
    media_id = commit.headers.get("x-media-id")
    assert media_id
    media_id_arg, temp_path = pending[0]
    await process_media(session, media_id_arg, temp_path)

    result = await session.execute(select(MediaItem).where(MediaItem.id == int(media_id)))
    item = result.scalar_one()
    assert item.title == "Sunday talk"
    assert item.file_size == 8


@pytest.mark.asyncio
async def test_tus_concat_rejects_incomplete_parts(auth_client):
    created = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": "4",
                "Upload-Concat": "partial",
            }
        ),
    )
    path = _upload_url(created)
    location = created.headers["location"]
    await _patch(auth_client, path, b"ab", 0)
    final = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Concat": f"final;{location}",
                "Upload-Metadata": _meta(filename="talk.mp3"),
            }
        ),
    )
    assert final.status_code == 400
    assert "incomplete" in final.json()["error"]


@pytest.mark.asyncio
async def test_tus_concat_rejects_other_users_parts(client):
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
            "Upload-Length": "4",
            "Upload-Concat": "partial",
        },
    )
    path = _upload_url(created)
    await client.patch(
        path,
        content=b"abcd",
        headers={
            **user_headers,
            "Upload-Offset": "0",
            "Content-Type": "application/offset+octet-stream",
        },
    )
    final = await client.post(
        "/files",
        headers={
            **admin_headers,
            "Upload-Concat": f"final;{created.headers['location']}",
            "Upload-Metadata": _meta(filename="talk.mp3"),
        },
    )
    assert final.status_code == 404


@pytest.mark.asyncio
async def test_tus_commit_rejects_partial(auth_client):
    created = await auth_client.post(
        "/files",
        headers=_tus_headers(
            **{
                "Upload-Length": "4",
                "Upload-Concat": "partial",
            }
        ),
    )
    path = _upload_url(created)
    await _patch(auth_client, path, b"abcd", 0)
    r = await auth_client.post(f"{path}/commit", json={"title": "Nope"})
    assert r.status_code == 409
    assert "Partial" in r.json()["error"]


@pytest.mark.asyncio
async def test_upload_page_includes_tus(auth_client):
    r = await auth_client.get("/upload")
    assert r.status_code == 200
    assert "/static/vendor/tus/tus.min.js" in r.text
    assert "/static/js/upload.js?v=3" in r.text
    assert "Transfer starts when you choose a file" in r.text
