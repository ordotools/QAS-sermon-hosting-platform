import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import get_settings
from app.database import async_session, get_session
from app.deps import require_user, template_context
from app.models import MediaItem, User
from app.services.media import create_media_record, new_storage_key, process_media
from app.services.rate_limit import rate_limit
from app.templating import templates

router = APIRouter(tags=["upload"])


async def _run_processing(media_id: int, temp_path: str) -> None:
    async with async_session() as session:
        await process_media(session, media_id, temp_path)


@router.get("/upload", response_class=HTMLResponse)
async def upload_form(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User = Depends(require_user),
):
    result = await session.execute(
        select(MediaItem)
        .where(MediaItem.uploaded_by_id == user.id)
        .order_by(MediaItem.created_at.desc())
        .limit(10)
    )
    recent = list(result.scalars().all())

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
    result = await session.execute(
        select(MediaItem)
        .where(MediaItem.uploaded_by_id == user.id)
        .order_by(MediaItem.created_at.desc())
        .limit(10)
    )
    recent = list(result.scalars().all())
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

    content = await file.read()
    if len(content) > settings.max_upload_bytes:
        result = await session.execute(
            select(MediaItem)
            .where(MediaItem.uploaded_by_id == user.id)
            .order_by(MediaItem.created_at.desc())
            .limit(10)
        )
        recent = list(result.scalars().all())
        return templates.TemplateResponse(
            request,
            "upload.html",
            {
                **template_context(request, user),
                "recent": recent,
                "error": f"File exceeds {settings.max_upload_size_mb} MB limit",
            },
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    mime_type = file.content_type or "application/octet-stream"
    storage_key = new_storage_key(file.filename or "upload.bin")

    pub_dt = datetime.utcnow()
    if published_at:
        try:
            pub_dt = datetime.fromisoformat(published_at)
        except ValueError:
            result = await session.execute(
                select(MediaItem)
                .where(MediaItem.uploaded_by_id == user.id)
                .order_by(MediaItem.created_at.desc())
                .limit(10)
            )
            recent = list(result.scalars().all())
            return templates.TemplateResponse(
                request,
                "upload.html",
                {
                    **template_context(request, user),
                    "recent": recent,
                    "error": "Invalid published date format",
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    item = await create_media_record(
        session,
        title=title.strip(),
        description=description.strip() or None,
        published_at=pub_dt,
        mime_type=mime_type,
        file_size=len(content),
        uploaded_by_id=user.id,
        storage_key=storage_key,
    )

    suffix = Path(file.filename or "upload.bin").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        temp_path = tmp.name

    background_tasks.add_task(_run_processing, item.id, temp_path)

    return RedirectResponse("/upload", status_code=status.HTTP_303_SEE_OTHER)
