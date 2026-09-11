import asyncio
import base64
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.deps import require_user
from app.models import User
from app.routers.upload import create_item_from_temp_file, schedule_processing
from app.services.media_formats import validate_extension
from app.services.rate_limit import rate_limit
from app.services.tus_store import TusUpload, create, data_path, delete, load, save, write_concat

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tus"])

TUS_VERSION = "1.0.0"
_HEX = set("0123456789abcdef")
_locks: dict[str, asyncio.Lock] = {}


class CommitBody(BaseModel):
    title: str = ""
    description: str = ""
    published_at: str = ""


def _lock_for(uid: str) -> asyncio.Lock:
    lock = _locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _locks[uid] = lock
    return lock


async def _acquire_locks(uids: list[str]) -> list[asyncio.Lock]:
    locks = [_lock_for(uid) for uid in sorted(set(uids))]
    for lock in locks:
        await lock.acquire()
    return locks


def _release_locks(locks: list[asyncio.Lock]) -> None:
    for lock in reversed(locks):
        lock.release()


def _tus_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    settings = get_settings()
    headers = {
        "Tus-Resumable": TUS_VERSION,
        "Tus-Version": TUS_VERSION,
        "Tus-Max-Size": str(settings.max_upload_bytes),
        "Tus-Extension": "creation,termination,concatenation",
        "Cache-Control": "no-store",
        "Access-Control-Expose-Headers": (
            "Location, Upload-Offset, Upload-Length, Upload-Concat, Tus-Resumable, "
            "Tus-Version, Tus-Extension, Tus-Max-Size, X-Media-Id"
        ),
    }
    if extra:
        headers.update(extra)
    return headers


def _tus_response(status_code: int, extra: dict[str, str] | None = None) -> Response:
    return Response(status_code=status_code, headers=_tus_headers(extra))


def _tus_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": message},
        status_code=status_code,
        headers=_tus_headers(),
    )


def _require_tus_resumable(request: Request) -> JSONResponse | None:
    value = request.headers.get("tus-resumable")
    if value != TUS_VERSION:
        return _tus_error("Missing or unsupported Tus-Resumable header", 412)
    return None


def decode_upload_metadata(header: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not header:
        return result
    for pair in header.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if " " not in pair:
            result[pair] = ""
            continue
        key, b64 = pair.split(" ", 1)
        try:
            result[key] = base64.b64decode(b64.strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            result[key] = ""
    return result


def _parse_length(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    return int(value)


def _parse_concat(header: str | None) -> tuple[str | None, list[str], str | None]:
    if not header or not header.strip():
        return None, [], None
    value = header.strip()
    lower = value.lower()
    if lower == "partial":
        return "partial", [], None
    if lower.startswith("final;"):
        rest = value.split(";", 1)[1].strip()
        urls = [part.strip() for part in rest.split() if part.strip()]
        if not urls:
            return None, [], "Upload-Concat final list is empty"
        return "final", urls, None
    return None, [], "Invalid Upload-Concat header"


def _uid_from_concat_url(url: str) -> str | None:
    path = urlparse(url).path if "://" in url else url
    marker = "/files/"
    idx = path.rfind(marker)
    if idx < 0:
        return None
    uid = path[idx + len(marker) :].strip("/")
    if uid and len(uid) == 32 and all(c in _HEX for c in uid):
        return uid
    return None


def _location(request: Request, uid: str) -> str:
    return str(request.base_url).rstrip("/") + f"/files/{uid}"


class PatchIncomplete(Exception):
    """Client disconnected or sent fewer bytes than Content-Length."""


class PatchTooLarge(ValueError):
    def __init__(self) -> None:
        super().__init__("chunk too large")


async def _drain_request(request: Request) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return
        if message["type"] == "http.request" and not message.get("more_body", False):
            return


def _truncate_upload(path: Path, size: int) -> None:
    if not path.is_file():
        return
    with path.open("r+b") as handle:
        handle.truncate(size)
        handle.flush()


async def _stream_patch_to_file(
    request: Request,
    path: Path,
    offset: int,
    max_bytes: int,
) -> int:
    expected = _parse_length(request.headers.get("content-length"))
    if expected is not None and expected > max_bytes:
        await _drain_request(request)
        raise PatchTooLarge()

    written = 0
    disconnected = False
    too_large = False
    try:
        with path.open("r+b") as handle:
            handle.seek(offset)
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    break
                if message["type"] != "http.request":
                    continue
                piece = message.get("body", b"")
                if piece:
                    if written + len(piece) > max_bytes:
                        too_large = True
                    elif not too_large:
                        handle.write(piece)
                        written += len(piece)
                if not message.get("more_body", False):
                    break
            if too_large or disconnected or (
                expected is not None and written != expected
            ):
                handle.truncate(offset)
                handle.flush()
            else:
                handle.flush()
    except Exception:
        _truncate_upload(path, offset)
        raise

    if too_large:
        raise PatchTooLarge()
    if disconnected or (expected is not None and written != expected):
        raise PatchIncomplete()
    return written


def _offset_headers(upload: TusUpload, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Upload-Offset": str(upload.offset),
        "Upload-Length": str(upload.size),
    }
    if upload.concat:
        headers["Upload-Concat"] = upload.concat
    if upload.media_id is not None:
        headers["X-Media-Id"] = str(upload.media_id)
    if extra:
        headers.update(extra)
    return headers


def _owned_upload(uid: str, user: User) -> tuple[TusUpload | None, JSONResponse | None]:
    upload = load(uid)
    if not upload or upload.user_id != user.id:
        return None, _tus_error("Upload not found", status.HTTP_404_NOT_FOUND)
    return upload, None


def _validate_metadata(metadata: dict[str, str], *, require_filename: bool) -> JSONResponse | None:
    filename = Path(metadata.get("filename") or metadata.get("name") or "").name
    if require_filename:
        if not filename:
            return _tus_error("filename metadata is required", status.HTTP_400_BAD_REQUEST)
        ext_error = validate_extension(filename)
        if ext_error:
            return _tus_error(ext_error, status.HTTP_400_BAD_REQUEST)
    published_at = (metadata.get("published_at") or "").strip()
    if published_at:
        try:
            datetime.fromisoformat(published_at)
        except ValueError:
            return _tus_error("Invalid published date format", status.HTTP_400_BAD_REQUEST)
    return None


async def _create_final(
    request: Request,
    user: User,
    metadata: dict[str, str],
    part_urls: list[str],
) -> Response:
    settings = get_settings()
    part_uids: list[str] = []
    for url in part_urls:
        uid = _uid_from_concat_url(url)
        if not uid:
            return _tus_error("Invalid concatenation URL", status.HTTP_400_BAD_REQUEST)
        part_uids.append(uid)

    dest: TusUpload | None = None
    locks = await _acquire_locks(part_uids)
    try:
        total = 0
        for uid in part_uids:
            upload, err = _owned_upload(uid, user)
            if err:
                return err
            assert upload is not None
            if (upload.concat or "").lower() != "partial":
                return _tus_error(
                    "Concatenation source is not a partial upload",
                    status.HTTP_400_BAD_REQUEST,
                )
            if upload.offset != upload.size:
                return _tus_error("Concatenation source is incomplete", status.HTTP_400_BAD_REQUEST)
            if not data_path(uid).is_file():
                return _tus_error("Concatenation source is missing", status.HTTP_404_NOT_FOUND)
            total += upload.size

        if total > settings.max_upload_bytes:
            return _tus_error(
                f"File exceeds {settings.max_upload_size_mb} MB limit",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        dest_uid = uuid.uuid4().hex
        dest = TusUpload(
            uid=dest_uid,
            size=total,
            offset=0,
            metadata=metadata,
            user_id=user.id,
            concat="final;" + " ".join(part_urls),
        )
        try:
            await asyncio.to_thread(write_concat, dest, part_uids)
        except Exception:
            delete(dest_uid)
            logger.exception("tus concat failed for %s", dest_uid)
            return _tus_error("Could not concatenate upload", status.HTTP_500_INTERNAL_SERVER_ERROR)

        for uid in part_uids:
            delete(uid)
            _locks.pop(uid, None)
    finally:
        _release_locks(locks)

    assert dest is not None
    return _tus_response(
        status.HTTP_201_CREATED,
        _offset_headers(dest, {"Location": _location(request, dest.uid)}),
    )


@router.options("/files")
@router.options("/files/{uid}")
async def tus_options():
    return _tus_response(status.HTTP_204_NO_CONTENT)


@router.post("/files")
async def tus_create(
    request: Request,
    user: User = Depends(require_user),
):
    precondition = _require_tus_resumable(request)
    if precondition:
        return precondition

    concat_kind, part_urls, concat_error = _parse_concat(request.headers.get("upload-concat"))
    if concat_error:
        return _tus_error(concat_error, status.HTTP_400_BAD_REQUEST)

    metadata = decode_upload_metadata(request.headers.get("upload-metadata"))
    meta_error = _validate_metadata(metadata, require_filename=concat_kind != "partial")
    if meta_error:
        return meta_error

    if concat_kind == "final":
        return await _create_final(request, user, metadata, part_urls)

    settings = get_settings()
    size = _parse_length(request.headers.get("upload-length"))
    if size is None:
        return _tus_error("Upload-Length is required", status.HTTP_400_BAD_REQUEST)
    if size > settings.max_upload_bytes:
        return _tus_error(
            f"File exceeds {settings.max_upload_size_mb} MB limit",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    uid = uuid.uuid4().hex
    upload = TusUpload(
        uid=uid,
        size=size,
        offset=0,
        metadata=metadata,
        user_id=user.id,
        concat="partial" if concat_kind == "partial" else None,
    )
    create(upload)
    return _tus_response(
        status.HTTP_201_CREATED,
        _offset_headers(upload, {"Location": _location(request, uid)}),
    )


@router.head("/files/{uid}")
async def tus_head(
    uid: str,
    request: Request,
    user: User = Depends(require_user),
):
    precondition = _require_tus_resumable(request)
    if precondition:
        return precondition
    upload, err = _owned_upload(uid, user)
    if err:
        return err
    assert upload is not None
    return _tus_response(status.HTTP_204_NO_CONTENT, _offset_headers(upload))


@router.patch("/files/{uid}")
async def tus_patch(
    uid: str,
    request: Request,
    user: User = Depends(require_user),
):
    precondition = _require_tus_resumable(request)
    if precondition:
        await _drain_request(request)
        return precondition

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    if content_type != "application/offset+octet-stream":
        await _drain_request(request)
        return _tus_error(
            "Content-Type must be application/offset+octet-stream",
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    offset = _parse_length(request.headers.get("upload-offset"))
    if offset is None:
        await _drain_request(request)
        return _tus_error("Upload-Offset is required", status.HTTP_400_BAD_REQUEST)

    async with _lock_for(uid):
        upload, err = _owned_upload(uid, user)
        if err:
            await _drain_request(request)
            return err
        assert upload is not None

        if offset != upload.offset:
            await _drain_request(request)
            return _tus_response(
                status.HTTP_409_CONFLICT,
                _offset_headers(upload),
            )

        path = data_path(uid)
        if not path.is_file():
            await _drain_request(request)
            return _tus_error("Upload not found", status.HTTP_404_NOT_FOUND)

        prev = upload.offset
        remaining = upload.size - prev
        try:
            written = await _stream_patch_to_file(request, path, prev, remaining)
        except PatchTooLarge:
            return _tus_error(
                "Chunk exceeds remaining upload length",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        except PatchIncomplete:
            return _tus_error("Upload interrupted", status.HTTP_400_BAD_REQUEST)
        except Exception:
            _truncate_upload(path, prev)
            logger.exception("tus PATCH failed for %s", uid)
            return _tus_error("Upload interrupted", status.HTTP_500_INTERNAL_SERVER_ERROR)

        upload.offset = prev + written
        save(upload)
        return _tus_response(status.HTTP_204_NO_CONTENT, _offset_headers(upload))


@router.post("/files/{uid}/commit")
async def tus_commit(
    uid: str,
    request: Request,
    body: CommitBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User = Depends(require_user),
):
    rate_limit(request, "upload", max_requests=20, window_seconds=3600)

    async with _lock_for(uid):
        upload, err = _owned_upload(uid, user)
        if err:
            return err
        assert upload is not None

        if (upload.concat or "").lower() == "partial":
            return _tus_error("Partial uploads cannot be committed", status.HTTP_409_CONFLICT)

        if upload.media_id is not None:
            return _tus_response(status.HTTP_204_NO_CONTENT, _offset_headers(upload))

        if upload.offset != upload.size:
            return _tus_error("Upload is not complete", status.HTTP_409_CONFLICT)

        path = data_path(uid)
        if not path.is_file():
            return _tus_error("Upload not found", status.HTTP_404_NOT_FOUND)

        title = body.title.strip() or (upload.metadata.get("title") or "").strip()
        description = body.description if body.description else (upload.metadata.get("description") or "")
        published_at = body.published_at.strip() or (upload.metadata.get("published_at") or "")
        filename = Path(upload.metadata.get("filename") or upload.metadata.get("name") or "upload.bin").name
        mime_type = upload.metadata.get("filetype") or "application/octet-stream"

        item, error, code = await create_item_from_temp_file(
            temp_path=str(path),
            filename=filename,
            mime_type=mime_type,
            title=title,
            description=description,
            published_at=published_at,
            user=user,
            session=session,
        )
        if error or item is None:
            return _tus_error(error or "Upload failed", code)

        upload.media_id = item.id
        upload.metadata["title"] = title
        upload.metadata["description"] = description
        upload.metadata["published_at"] = published_at
        save(upload)
        schedule_processing(item.id, str(path))
        return _tus_response(status.HTTP_204_NO_CONTENT, _offset_headers(upload))


@router.delete("/files/{uid}")
async def tus_delete(
    uid: str,
    request: Request,
    user: User = Depends(require_user),
):
    precondition = _require_tus_resumable(request)
    if precondition:
        return precondition
    upload, err = _owned_upload(uid, user)
    if err:
        return err
    delete(uid)
    _locks.pop(uid, None)
    return _tus_response(status.HTTP_204_NO_CONTENT)
