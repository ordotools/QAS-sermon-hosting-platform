from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class UserRole(str, Enum):
    user = "user"
    superuser = "superuser"


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    password_hash: str
    role: UserRole = UserRole.user
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Invite(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    created_by_id: int = Field(foreign_key="user.id")
    max_uses: int = 1
    uses: int = 0
    expires_at: Optional[datetime] = None
    revoked: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MediaStatus(str, Enum):
    processing = "processing"
    ready = "ready"
    failed = "failed"


class MediaType(str, Enum):
    video = "video"
    audio = "audio"


class MediaItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: Optional[str] = None
    media_type: MediaType
    published_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    storage_key: str
    thumbnail_key: Optional[str] = None
    mime_type: str
    file_size: int = 0
    duration_seconds: Optional[float] = None
    uploaded_by_id: int = Field(foreign_key="user.id")
    status: MediaStatus = MediaStatus.processing
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
