import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.deps import get_current_user_optional, require_user, template_context
from app.models import User
from app.services.media import delete_media, get_media, list_media
from app.templating import templates

router = APIRouter(tags=["public"])


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User | None = Depends(get_current_user_optional),
):
    items = await list_media(session, ready_only=True)
    return templates.TemplateResponse(
        request,
        "index.html",
        {**template_context(request, user), "items": items},
    )


@router.get("/media/{media_id}", response_class=HTMLResponse)
async def player_page(
    request: Request,
    media_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User | None = Depends(get_current_user_optional),
):
    item = await get_media(session, media_id)
    if not item or item.status.value != "ready":
        return templates.TemplateResponse(
            request,
            "error.html",
            {**template_context(request, user), "message": "Media not found"},
            status_code=404,
        )

    back_url = "/admin?tab=media" if request.query_params.get("from") == "admin" else "/"

    return templates.TemplateResponse(
        request,
        "player.html",
        {**template_context(request, user), "item": item, "back_url": back_url},
    )


@router.post("/media/{media_id}/delete")
async def delete_own_media(
    media_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User = Depends(require_user),
):
    from app.models import UserRole

    item = await get_media(session, media_id)
    if not item:
        return RedirectResponse("/", status_code=303)
    if item.uploaded_by_id != user.id and user.role != UserRole.superuser:
        raise HTTPException(status_code=403, detail="Not allowed")
    await delete_media(session, item)
    return RedirectResponse("/", status_code=303)


async def _offline_catalog(session: AsyncSession) -> list[dict]:
    items = await list_media(session, ready_only=True)
    return [
        {
            "id": item.id,
            "title": item.title,
            "media_type": item.media_type.value,
            "duration_seconds": item.duration_seconds,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "file_size": item.file_size,
            "thumbnail_key": item.thumbnail_key,
        }
        for item in items
    ]


@router.get("/offline/catalog.json")
async def offline_catalog_json(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User | None = Depends(get_current_user_optional),
):
    return await _offline_catalog(session)


@router.get("/offline", response_class=HTMLResponse)
async def offline_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User | None = Depends(get_current_user_optional),
):
    catalog = await _offline_catalog(session)
    return templates.TemplateResponse(
        request,
        "offline.html",
        {**template_context(request, user), "catalog_json": json.dumps(catalog)},
    )
