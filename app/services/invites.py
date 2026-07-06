from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Invite
from app.services.auth import generate_token, hash_token


async def create_invite(
    session: AsyncSession,
    created_by_id: int,
    max_uses: int = 1,
    expires_days: int | None = 7,
) -> tuple[Invite, str]:
    token = generate_token()
    expires_at = datetime.utcnow() + timedelta(days=expires_days) if expires_days else None
    invite = Invite(
        token_hash=hash_token(token),
        created_by_id=created_by_id,
        max_uses=max_uses,
        expires_at=expires_at,
    )
    session.add(invite)
    await session.commit()
    await session.refresh(invite)
    return invite, token


async def get_invite_by_token(session: AsyncSession, token: str) -> Invite | None:
    result = await session.execute(select(Invite).where(Invite.token_hash == hash_token(token)))
    return result.scalar_one_or_none()


def invite_is_valid(invite: Invite | None) -> bool:
    if not invite or invite.revoked:
        return False
    if invite.expires_at and invite.expires_at < datetime.utcnow():
        return False
    if invite.uses >= invite.max_uses:
        return False
    return True


async def consume_invite(session: AsyncSession, invite: Invite) -> None:
    invite.uses += 1
    session.add(invite)
    await session.commit()


async def revoke_invite(session: AsyncSession, invite_id: int) -> bool:
    result = await session.execute(select(Invite).where(Invite.id == invite_id))
    invite = result.scalar_one_or_none()
    if not invite:
        return False
    invite.revoked = True
    session.add(invite)
    await session.commit()
    return True


async def list_invites(session: AsyncSession) -> list[Invite]:
    result = await session.execute(select(Invite).order_by(Invite.created_at.desc()))
    return list(result.scalars().all())
