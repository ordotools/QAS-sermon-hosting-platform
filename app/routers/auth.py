from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.deps import (
    clear_session_cookie,
    create_session_token,
    get_current_user_optional,
    set_session_cookie,
    template_context,
)
from app.models import User
from app.services.auth import authenticate_user, create_user, get_user_by_email
from app.services.invites import consume_invite, get_invite_by_token, invite_is_valid
from app.services.rate_limit import rate_limit
from app.templating import templates

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
):
    if user:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "login.html",
        {**template_context(request), "error": None},
    )


@router.post("/login")
async def login(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    email: str = Form(...),
    password: str = Form(...),
):
    rate_limit(request, "login", max_requests=10, window_seconds=300)
    user = await authenticate_user(session, email, password)
    if not user:
        return templates.TemplateResponse(
            request,
            "login.html",
            {**template_context(request), "error": "Invalid email or password"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    token = create_session_token(user.id)
    redirect = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(redirect, token, request)
    return redirect


@router.post("/logout")
async def logout(request: Request):
    redirect = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(redirect, request)
    return redirect


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: User | None = Depends(get_current_user_optional),
    token: str = "",
):
    if user:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    invite = await get_invite_by_token(db, token) if token else None
    valid = invite_is_valid(invite)
    return templates.TemplateResponse(
        request,
        "register.html",
        {
            **template_context(request),
            "token": token,
            "valid_token": valid,
            "error": None,
        },
    )


@router.post("/register")
async def register(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    token: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    invite = await get_invite_by_token(session, token)
    if not invite_is_valid(invite):
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                **template_context(request),
                "token": token,
                "valid_token": False,
                "error": "Invalid or expired invite link",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                **template_context(request),
                "token": token,
                "valid_token": True,
                "error": "Passwords do not match",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                **template_context(request),
                "token": token,
                "valid_token": True,
                "error": "Password must be at least 8 characters",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    existing = await get_user_by_email(session, email)
    if existing:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                **template_context(request),
                "token": token,
                "valid_token": True,
                "error": "An account with this email already exists",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    user = await create_user(session, email, password)
    await consume_invite(session, invite)
    token_str = create_session_token(user.id)
    redirect = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(redirect, token_str, request)
    return redirect
