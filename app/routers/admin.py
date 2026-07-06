from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.deps import require_superuser, template_context
from app.models import User, UserRole
from app.services.invites import create_invite, list_invites, revoke_invite
from app.services.media import delete_media, get_media, list_media
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: User = Depends(require_superuser),
):
    users_result = await session.execute(select(User).order_by(User.created_at.desc()))
    users = list(users_result.scalars().all())
    invites = await list_invites(session)
    media = await list_media(session, ready_only=False)
    base_url = str(request.base_url).rstrip("/")

    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            **template_context(request, user),
            "users": users,
            "invites": invites,
            "media_items": media,
            "base_url": base_url,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/users/{user_id}/deactivate")
async def deactivate_user(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: User = Depends(require_superuser),
):
    if user_id == admin.id:
        return RedirectResponse("/admin?message=Cannot+deactivate+yourself", status_code=303)
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target:
        target.is_active = False
        session.add(target)
        await session.commit()
    return RedirectResponse("/admin?message=User+deactivated", status_code=303)


@router.post("/users/{user_id}/reactivate")
async def reactivate_user(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: User = Depends(require_superuser),
):
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target:
        target.is_active = True
        session.add(target)
        await session.commit()
    return RedirectResponse("/admin?message=User+reactivated", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: User = Depends(require_superuser),
):
    if user_id == admin.id:
        return RedirectResponse("/admin?message=Cannot+delete+yourself", status_code=303)
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target and target.role != UserRole.superuser:
        await session.delete(target)
        await session.commit()
    return RedirectResponse("/admin?message=User+deleted", status_code=303)


@router.post("/invites")
async def create_invite_link(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: User = Depends(require_superuser),
    max_uses: int = Form(1),
    expires_days: int = Form(7),
):
    _, token = await create_invite(session, admin.id, max_uses=max_uses, expires_days=expires_days or None)
    base_url = str(request.base_url).rstrip("/")
    link = f"{base_url}/register?token={token}"
    return RedirectResponse(f"/admin?message=Invite+created:+{link}", status_code=303)


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite_link(
    invite_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: User = Depends(require_superuser),
):
    await revoke_invite(session, invite_id)
    return RedirectResponse("/admin?message=Invite+revoked", status_code=303)


@router.post("/media/{media_id}/delete")
async def admin_delete_media(
    media_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: User = Depends(require_superuser),
):
    item = await get_media(session, media_id)
    if item:
        await delete_media(session, item)
    return RedirectResponse("/admin?message=Media+deleted", status_code=303)
