import asyncio
import base64
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.deps import require_user
from app.models import User
from app.routers.upload import create_item_from_temp_file, run_processing
from app.services.media_formats import validate_extension
from app.services.rate_limit import rate_limit
from app.services.tus_store import TusUpload, create, data_path, delete, load, save

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tus"])

TUS_VERSION = "1.0.0"
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(uid: str) -> asyncio.Lock:
    lock = _locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _locks[uid] = lock
    return lock


def _tus_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    settings = get_settings()
    headers = {
        "Tus-Resumable": TUS_VERSION,
        "Tus-Version": TUS_VERSION,
        "Tus-Max-Size": str(settings.max_upload_bytes),
        "Tus-Extension": "creation,termination",
        "Cache-Control": "no-store",
        "Access-Control-Expose-Headers": (
            "Location, Upload-Offset, Upload-Length, Tus-Resumable, "
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


@router.options("/files")
@router.options("/files/{uid}")
async def tus_options():
    return _tus_response(status.HTTP_204_NO_CONTENT)


@router.post("/files")
async def tus_create(
    request: Request,
    user: User = Depends(require_user),
):
    rate_limit(request, "upload", max_requests=20, window_seconds=3600)
    precondition = _require_tus_resumable(request)
    if precondition:
        return precondition

    settings = get_settings()
    size = _parse_length(request.headers.get("upload-length"))
    if size is None:
        return _tus_error("Upload-Length is required", status.HTTP_400_BAD_REQUEST)
    if size > settings.max_upload_bytes:
        return _tus_error(
            f"File exceeds {settings.max_upload_size_mb} MB limit",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    metadata = decode_upload_metadata(request.headers.get("upload-metadata"))
    filename = Path(metadata.get("filename") or metadata.get("name") or "").name
    if not filename:
        return _tus_error("filename metadata is required", status.HTTP_400_BAD_REQUEST)

    ext_error = validate_extension(filename)
    if ext_error:
        return _tus_error(ext_error, status.HTTP_400_BAD_REQUEST)

    title = (metadata.get("title") or "").strip()
    if not title:
        return _tus_error("Title is required", status.HTTP_400_BAD_REQUEST)

    published_at = (metadata.get("published_at") or "").strip()
    if published_at:
        try:
            datetime.fromisoformat(published_at)
        except ValueError:
            return _tus_error("Invalid published date format", status.HTTP_400_BAD_REQUEST)

    uid = uuid.uuid4().hex
    create(
        TusUpload(
            uid=uid,
            size=size,
            offset=0,
            metadata=metadata,
            user_id=user.id,
        )
    )
    location = str(request.base_url).rstrip("/") + f"/files/{uid}"
    return _tus_response(
        status.HTTP_201_CREATED,
        {"Location": location, "Upload-Offset": "0", "Upload-Length": str(size)},
    )


def _owned_upload(uid: str, user: User) -> tuple[TusUpload | None, JSONResponse | None]:
    upload = load(uid)
    if not upload or upload.user_id != user.id:
        return None, _tus_error("Upload not found", status.HTTP_404_NOT_FOUND)
    return upload, None


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
    extra = {
        "Upload-Offset": str(upload.offset),
        "Upload-Length": str(upload.size),
    }
    if upload.media_id is not None:
        extra["X-Media-Id"] = str(upload.media_id)
    return _tus_response(status.HTTP_204_NO_CONTENT, extra)


@router.patch("/files/{uid}")
async def tus_patch(
    uid: str,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User = Depends(require_user),
):
    precondition = _require_tus_resumable(request)
    if precondition:
        return precondition

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    if content_type != "application/offset+octet-stream":
        return _tus_error(
            "Content-Type must be application/offset+octet-stream",
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    offset = _parse_length(request.headers.get("upload-offset"))
    if offset is None:
        return _tus_error("Upload-Offset is required", status.HTTP_400_BAD_REQUEST)

    async with _lock_for(uid):
        upload, err = _owned_upload(uid, user)
        if err:
            return err
        assert upload is not None

        if offset != upload.offset:
            return _tus_response(
                status.HTTP_409_CONFLICT,
                {"Upload-Offset": str(upload.offset), "Upload-Length": str(upload.size)},
            )

        path = data_path(uid)
        if not path.is_file():
            return _tus_error("Upload not found", status.HTTP_404_NOT_FOUND)

        prev = upload.offset
        written = 0
        try:
            with path.open("r+b") as handle:
                handle.seek(prev)
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    written += len(chunk)
                    if prev + written > upload.size:
                        handle.truncate(prev)
                        return _tus_error(
                            "Chunk exceeds remaining upload length",
                            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        )
                    handle.write(chunk)
                handle.flush()
        except Exception:
            if path.is_file():
                with path.open("r+b") as handle:
                    handle.truncate(prev)
            logger.exception("tus PATCH failed for %s", uid)
            return _tus_error("Upload interrupted", status.HTTP_500_INTERNAL_SERVER_ERROR)

        upload.offset = prev + written
        save(upload)

        extra = {
            "Upload-Offset": str(upload.offset),
            "Upload-Length": str(upload.size),
        }
        if upload.offset < upload.size:
            return _tus_response(status.HTTP_204_NO_CONTENT, extra)

        filename = Path(upload.metadata.get("filename") or "upload.bin").name
        mime_type = upload.metadata.get("filetype") or "application/octet-stream"
        item, error, code = await create_item_from_temp_file(
            temp_path=str(path),
            filename=filename,
            mime_type=mime_type,
            title=upload.metadata.get("title") or "",
            description=upload.metadata.get("description") or "",
            published_at=upload.metadata.get("published_at") or "",
            user=user,
            session=session,
        )
        if error or item is None:
            delete(uid)
            return _tus_error(error or "Upload failed", code)

        upload.media_id = item.id
        save(upload)
        background_tasks.add_task(run_processing, item.id, str(path))
        extra["X-Media-Id"] = str(item.id)
        return _tus_response(status.HTTP_204_NO_CONTENT, extra)


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
