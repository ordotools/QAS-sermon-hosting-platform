import pytest
from starlette.requests import Request

from app.services.rate_limit import rate_limit


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_index_public(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "QAS Media" in r.text


@pytest.mark.asyncio
async def test_login_success(client):
    r = await client.post(
        "/login",
        data={"email": "admin@test.com", "password": "testpass123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "qas_session" in r.cookies
    flags = {p.strip().lower() for p in r.headers.get("set-cookie", "").split(";")[1:]}
    assert "secure" not in flags


@pytest.mark.asyncio
async def test_login_sets_secure_cookie_behind_https_proxy(client):
    r = await client.post(
        "/login",
        data={"email": "admin@test.com", "password": "testpass123"},
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    flags = {p.strip().lower() for p in r.headers.get("set-cookie", "").split(";")[1:]}
    assert "secure" in flags


@pytest.mark.asyncio
async def test_login_failure(client):
    r = await client.post(
        "/login",
        data={"email": "admin@test.com", "password": "wrong"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_logout_clears_cookie(client):
    r = await client.post(
        "/login",
        data={"email": "admin@test.com", "password": "testpass123"},
        follow_redirects=False,
    )
    assert "qas_session" in r.cookies

    r = await client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    set_cookie = r.headers.get("set-cookie", "")
    assert "qas_session=" in set_cookie
    assert "Max-Age=0" in set_cookie or 'qas_session=""' in set_cookie


@pytest.mark.asyncio
async def test_deactivated_user_cannot_login(client, session):
    from app.services.auth import get_user_by_email

    user = await get_user_by_email(session, "user@test.com")
    user.is_active = False
    session.add(user)
    await session.commit()

    r = await client.post(
        "/login",
        data={"email": "user@test.com", "password": "userpass123"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_upload_requires_auth(client):
    r = await client.get("/upload")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_upload_page_auth(auth_client):
    r = await auth_client.get("/upload")
    assert r.status_code == 200
    assert "Upload" in r.text


@pytest.mark.asyncio
async def test_upload_status_partial(auth_client):
    r = await auth_client.get("/upload/status")
    assert r.status_code == 200
    assert "status-list" in r.text


@pytest.mark.asyncio
async def test_upload_rejects_invalid_date(auth_client):
    r = await auth_client.post(
        "/upload",
        data={
            "title": "Bad date",
            "description": "",
            "published_at": "not-a-date",
        },
        files={"file": ("test.mp3", b"fake", "audio/mpeg")},
    )
    assert r.status_code == 400
    assert "Invalid published date" in r.text


@pytest.mark.asyncio
async def test_upload_rejects_disallowed_extension(auth_client):
    r = await auth_client.post(
        "/upload",
        data={"title": "Doc", "description": "", "published_at": ""},
        files={"file": ("notes.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert r.status_code == 400
    assert "Unsupported file type" in r.text


@pytest.mark.asyncio
async def test_upload_rejects_disallowed_extension_json(auth_client):
    r = await auth_client.post(
        "/upload",
        data={"title": "Doc", "description": "", "published_at": ""},
        files={"file": ("notes.pdf", b"%PDF-1.4", "application/pdf")},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "Unsupported file type" in body["error"]


@pytest.mark.asyncio
async def test_upload_rejects_garbage_bytes(auth_client):
    r = await auth_client.post(
        "/upload",
        data={"title": "Bad audio", "description": "", "published_at": ""},
        files={"file": ("test.mp3", b"not-valid-media-bytes", "audio/mpeg")},
    )
    assert r.status_code == 400
    assert "Could not read file" in r.text


@pytest.mark.asyncio
async def test_upload_rejects_garbage_bytes_json(auth_client):
    r = await auth_client.post(
        "/upload",
        data={"title": "Bad audio", "description": "", "published_at": ""},
        files={"file": ("test.mp3", b"not-valid-media-bytes", "audio/mpeg")},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "Could not read file" in body["error"]


@pytest.mark.asyncio
async def test_upload_valid_mp3_creates_ready_record(auth_client, session, monkeypatch):
    from fastapi import BackgroundTasks
    from sqlmodel import select

    from app.models import MediaItem, MediaStatus, MediaType
    from app.services.media import process_media
    from app.services.media_formats import MediaProbe

    mp3_probe = MediaProbe(
        media_type=MediaType.audio,
        video_codec=None,
        audio_codec="mp3",
        duration=1.0,
        container="mp3",
        has_video=False,
        has_audio=True,
        probe_ok=True,
    )
    monkeypatch.setattr("app.routers.upload.probe_media", lambda _path: mp3_probe)
    monkeypatch.setattr("app.services.media.probe_media", lambda _path: mp3_probe)

    pending: list[tuple] = []

    def capture_task(self, func, *args, **kwargs):
        pending.append((func, args, kwargs))

    monkeypatch.setattr(BackgroundTasks, "add_task", capture_task)

    r = await auth_client.post(
        "/upload",
        data={"title": "Sermon", "description": "", "published_at": ""},
        files={"file": ("test.mp3", b"\x00" * 64, "audio/mpeg")},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    media_id = body["media_id"]

    assert len(pending) == 1
    _, args, _ = pending[0]
    await process_media(session, args[0], args[1])

    result = await session.execute(select(MediaItem).where(MediaItem.id == media_id))
    item = result.scalar_one()
    assert item.status == MediaStatus.ready
    assert item.media_type == MediaType.audio


@pytest.mark.asyncio
async def test_admin_requires_superuser(auth_client):
    r = await auth_client.get("/admin")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_access(admin_client):
    r = await admin_client.get("/admin")
    assert r.status_code == 200
    assert "Admin" in r.text


@pytest.mark.asyncio
async def test_create_invite(admin_client):
    r = await admin_client.post(
        "/admin/invites",
        data={"max_uses": "1", "expires_days": "7"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Invite+created" in r.headers.get("location", "")


@pytest.mark.asyncio
async def test_register_with_invite(client, admin_client):
    r = await admin_client.post(
        "/admin/invites",
        data={"max_uses": "1", "expires_days": "7"},
        follow_redirects=False,
    )
    location = r.headers.get("location", "")
    token = location.split("token=")[-1]

    r = await client.post(
        "/register",
        data={
            "token": token,
            "email": "newuser@test.com",
            "password": "newpass123",
            "password_confirm": "newpass123",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "qas_session" in r.cookies


@pytest.mark.asyncio
async def test_register_duplicate_email(client, admin_client):
    r = await admin_client.post(
        "/admin/invites",
        data={"max_uses": "1", "expires_days": "7"},
        follow_redirects=False,
    )
    token = r.headers.get("location", "").split("token=")[-1]

    r = await client.post(
        "/register",
        data={
            "token": token,
            "email": "user@test.com",
            "password": "otherpass123",
            "password_confirm": "otherpass123",
        },
    )
    assert r.status_code == 400
    assert "already exists" in r.text


@pytest.mark.asyncio
async def test_admin_reactivate_user(admin_client, session):
    from app.services.auth import get_user_by_email

    user = await get_user_by_email(session, "user@test.com")
    user.is_active = False
    session.add(user)
    await session.commit()

    r = await admin_client.post(f"/admin/users/{user.id}/reactivate", follow_redirects=False)
    assert r.status_code == 303
    assert "reactivated" in r.headers.get("location", "").lower()

    await session.refresh(user)
    assert user.is_active is True


@pytest.mark.asyncio
async def test_stream_range(client, session, tmp_path):
    from datetime import datetime

    from app.models import MediaItem, MediaStatus, MediaType, User
    from app.services.auth import get_user_by_email
    from app.storage.local import LocalStorage

    storage = LocalStorage(str(tmp_path / "media"))
    key = "media/test.mp3"
    data = b"\x00" * 1000
    await storage.save(key, data)

    user = await get_user_by_email(session, "admin@test.com")
    item = MediaItem(
        title="Test Audio",
        media_type=MediaType.audio,
        published_at=datetime.utcnow(),
        storage_key=key,
        mime_type="audio/mpeg",
        file_size=len(data),
        uploaded_by_id=user.id,
        status=MediaStatus.ready,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)

    r = await client.get(f"/stream/{item.id}", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert len(r.content) == 100
    assert r.headers.get("content-range") == "bytes 0-99/1000"


def test_rate_limit_blocks_excess_requests():
    from fastapi import HTTPException

    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 12345)}
    request = Request(scope)

    for _ in range(10):
        rate_limit(request, "test-login", max_requests=10, window_seconds=300)

    with pytest.raises(HTTPException) as exc:
        rate_limit(request, "test-login", max_requests=10, window_seconds=300)
    assert exc.value.status_code == 429
