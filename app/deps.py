from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models import User, UserRole
from app.services.auth import get_user_by_id

SESSION_COOKIE = "qas_session"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="qas-session")


def create_session_token(user_id: int) -> str:
    return _serializer().dumps({"user_id": user_id})


def decode_session_token(token: str) -> int | None:
    try:
        data = _serializer().loads(token, max_age=get_settings().session_max_age)
        return int(data["user_id"])
    except (BadSignature, SignatureExpired, KeyError, ValueError):
        return None


async def get_current_user_optional(
    session: Annotated[AsyncSession, Depends(get_session)],
    qas_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User | None:
    if not qas_session:
        return None
    user_id = decode_session_token(qas_session)
    if not user_id:
        return None
    user = await get_user_by_id(session, user_id)
    if not user or not user.is_active:
        return None
    return user


async def require_user(
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user


async def require_superuser(
    user: Annotated[User, Depends(require_user)],
) -> User:
    if user.role != UserRole.superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superuser access required",
        )
    return user


def template_context(request: Request, user: User | None = None) -> dict:
    return {
        "request": request,
        "user": user,
        "is_superuser": user is not None and user.role == UserRole.superuser,
    }
