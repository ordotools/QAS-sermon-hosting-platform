import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import get_settings
from app.database import async_session, get_session
from app.deps import require_user, template_context
from app.models import MediaItem, User
from app.services.media import create_media_record, new_storage_key, process_media
from app.services.media_formats import (
    ffprobe_available,
    media_tools_error,
    probe_media,
    validate_extension,
    validate_probe,
)
from app.services.rate_limit import rate_limit
from app.templating import templates

router = APIRouter(tags=["upload"])

CHUNK_SIZE = 1024 * 1024  # 1 MiB


async def _run_processing(media_id: int, temp_path: str) -> None:
    async with async_session() as session:
        await process_media(session, media_id, temp_path)


async def _recent_uploads(session: AsyncSession, user_id: int) -> list[MediaItem]:
    result = await session.execute(
        select(MediaItem)
        .where(MediaItem.uploaded_by_id == user_id)
        .order_by(MediaItem.created_at.desc())
        .limit(10)
    )
    return list(result.scalars().all())


def _wants_json(request: Request) -> bool:
    if request.headers.get("x-requested-with", "").lower() == "xmlhttprequest":
        return True
    return "application/json" in request.headers.get("accept", "")


async def _upload_error(
    request: Request,
    session: AsyncSession,
    user: User,
    error: str,
    status_code: int,
) -> Response:
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": error}, status_code=status_code)
    recent = await _recent_uploads(session, user.id)
    return templates.TemplateResponse(
        request,
        "upload.html",
        {**template_context(request, user), "recent": recent, "error": error},
        status_code=status_code,
    )


async def _stream_upload_to_temp(
    upload: UploadFile,
    *,
    max_bytes: int,
    suffix: str,
) -> tuple[str | None, int, str | None]:
    total = 0
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        while True:
            chunk = await upload.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                Path(tmp.name).unlink(missing_ok=True)
                settings = get_settings()
                return None, 0, f"File exceeds {settings.max_upload_size_mb} MB limit"
            tmp.write(chunk)
        return tmp.name, total, None


@router.get("/upload", response_class=HTMLResponse)
async def upload_form(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User = Depends(require_user),
):
    recent = await _recent_uploads(session, user.id)
    return templates.TemplateResponse(
        request,
        "upload.html",
        {**template_context(request, user), "recent": recent, "error": None},
    )


@router.get("/upload/status", response_class=HTMLResponse)
async def upload_status(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User = Depends(require_user),
):
    recent = await _recent_uploads(session, user.id)
    return templates.TemplateResponse(
        request,
        "partials/upload_status.html",
        {**template_context(request, user), "recent": recent},
    )


@router.post("/upload")
async def upload_media(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User = Depends(require_user),
    title: str = Form(...),
    description: str = Form(""),
    published_at: str = Form(""),
    file: UploadFile = File(...),
):
    rate_limit(request, "upload", max_requests=20, window_seconds=3600)
    settings = get_settings()
    filename = file.filename or "upload.bin"

    ext_error = validate_extension(filename)
    if ext_error:
        return await _upload_error(
            request, session, user, ext_error, status.HTTP_400_BAD_REQUEST
        )

    pub_dt = datetime.utcnow()
    if published_at:
        try:
            pub_dt = datetime.fromisoformat(published_at)
        except ValueError:
            return await _upload_error(
                request,
                session,
                user,
                "Invalid published date format",
                status.HTTP_400_BAD_REQUEST,
            )

    suffix = Path(filename).suffix
    temp_path, file_size, size_error = await _stream_upload_to_temp(
        file, max_bytes=settings.max_upload_bytes, suffix=suffix
    )
    if size_error:
        return await _upload_error(
            request,
            session,
            user,
            size_error,
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    assert temp_path is not None
    try:
        if not ffprobe_available():
            return await _upload_error(
                request,
                session,
                user,
                media_tools_error(),
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        probe = probe_media(temp_path)
        validation_error = validate_probe(probe)
        if validation_error:
            Path(temp_path).unlink(missing_ok=True)
            return await _upload_error(
                request,
                session,
                user,
                validation_error,
                status.HTTP_400_BAD_REQUEST,
            )

        mime_type = file.content_type or "application/octet-stream"
        storage_key = new_storage_key(filename)

        item = await create_media_record(
            session,
            title=title.strip(),
            description=description.strip() or None,
            published_at=pub_dt,
            mime_type=mime_type,
            file_size=file_size,
            uploaded_by_id=user.id,
            storage_key=storage_key,
        )
    except Exception:
        Path(temp_path).unlink(missing_ok=True)
        raise

    background_tasks.add_task(_run_processing, item.id, temp_path)

    if _wants_json(request):
        return JSONResponse({"ok": True, "media_id": item.id})

    return RedirectResponse("/upload", status_code=status.HTTP_303_SEE_OTHER)
