import json
import logging
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import MediaItem, MediaStatus, MediaType
from app.storage import get_storage

AUDIO_MIMES = {"audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/ogg", "audio/webm"}
VIDEO_MIMES = {"video/mp4", "video/webm", "video/ogg", "video/quicktime"}


def detect_media_type(mime_type: str) -> MediaType:
    if mime_type in VIDEO_MIMES or mime_type.startswith("video/"):
        return MediaType.video
    return MediaType.audio


def _run_ffprobe(path: str) -> dict:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return {}


def _extract_duration(probe: dict) -> float | None:
    fmt = probe.get("format", {})
    duration = fmt.get("duration")
    if duration:
        return float(duration)
    for stream in probe.get("streams", []):
        if stream.get("duration"):
            return float(stream["duration"])
    return None


def _generate_thumbnail(src_path: str, media_type: MediaType, duration: float | None) -> str | None:
    if media_type == MediaType.audio:
        return None
    thumb_path = tempfile.mktemp(suffix=".jpg")
    seek = 5.0
    if duration and duration < 10:
        seek = max(duration / 2, 0.5)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(seek),
                "-i",
                src_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                thumb_path,
            ],
            capture_output=True,
            check=True,
        )
        return thumb_path
    except (subprocess.CalledProcessError, OSError):
        return None


async def list_media(session: AsyncSession, ready_only: bool = True) -> list[MediaItem]:
    stmt = select(MediaItem).order_by(MediaItem.published_at.desc())
    if ready_only:
        stmt = stmt.where(MediaItem.status == MediaStatus.ready)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_media(session: AsyncSession, media_id: int) -> MediaItem | None:
    result = await session.execute(select(MediaItem).where(MediaItem.id == media_id))
    return result.scalar_one_or_none()


async def create_media_record(
    session: AsyncSession,
    *,
    title: str,
    description: str | None,
    published_at: datetime,
    mime_type: str,
    file_size: int,
    uploaded_by_id: int,
    storage_key: str,
) -> MediaItem:
    item = MediaItem(
        title=title,
        description=description or None,
        published_at=published_at,
        media_type=detect_media_type(mime_type),
        mime_type=mime_type,
        file_size=file_size,
        uploaded_by_id=uploaded_by_id,
        storage_key=storage_key,
        status=MediaStatus.processing,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def process_media(session: AsyncSession, media_id: int, temp_path: str) -> None:
    storage = get_storage()
    result = await session.execute(select(MediaItem).where(MediaItem.id == media_id))
    item = result.scalar_one_or_none()
    if not item:
        Path(temp_path).unlink(missing_ok=True)
        return

    try:
        probe = _run_ffprobe(temp_path)
        duration = _extract_duration(probe)
        await storage.save_file(item.storage_key, temp_path)

        thumb_local = _generate_thumbnail(temp_path, item.media_type, duration)
        if thumb_local:
            thumb_key = f"thumbnails/{item.id}.jpg"
            await storage.save_file(thumb_key, thumb_local)
            Path(thumb_local).unlink(missing_ok=True)
            item.thumbnail_key = thumb_key

        item.duration_seconds = duration
        item.status = MediaStatus.ready
        item.updated_at = datetime.utcnow()
    except Exception:
        logger.exception("Media processing failed for item %s", media_id)
        item.status = MediaStatus.failed
        item.updated_at = datetime.utcnow()
    finally:
        Path(temp_path).unlink(missing_ok=True)
        session.add(item)
        await session.commit()


async def delete_media(session: AsyncSession, item: MediaItem) -> None:
    storage = get_storage()
    await storage.delete(item.storage_key)
    if item.thumbnail_key:
        await storage.delete(item.thumbnail_key)
    await session.delete(item)
    await session.commit()


def new_storage_key(filename: str) -> str:
    ext = Path(filename).suffix.lower() or ".bin"
    return f"media/{uuid.uuid4().hex}{ext}"
