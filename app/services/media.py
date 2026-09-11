import asyncio
import logging
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import get_settings
from app.models import MediaItem, MediaStatus, MediaType
from app.services.media_formats import (
    _resolve_binary,
    can_remux_video_to_mp4,
    needs_transcode,
    normalize_storage_key,
    probe_media,
    remux_video_to_mp4,
    transcode_audio,
    transcode_video,
    validate_probe,
)
from app.storage import get_storage

AUDIO_MIMES = {"audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/ogg", "audio/webm"}
VIDEO_MIMES = {"video/mp4", "video/webm", "video/ogg", "video/quicktime"}


def detect_media_type(mime_type: str) -> MediaType:
    if mime_type in VIDEO_MIMES or mime_type.startswith("video/"):
        return MediaType.video
    return MediaType.audio


def scratch_dir() -> Path:
    path = Path(get_settings().local_storage_path) / ".scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _scratch_temp(suffix: str) -> str:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=scratch_dir())
    handle.close()
    return handle.name


def _processing_error_message(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno == 28:
        return "Disk full while processing the file."
    text = str(exc).strip() or type(exc).__name__
    return text[:500]


def _generate_thumbnail(src_path: str, media_type: MediaType, duration: float | None) -> str | None:
    if media_type == MediaType.audio:
        return None
    thumb_path = _scratch_temp(".jpg")
    seek = 5.0
    if duration and duration < 10:
        seek = max(duration / 2, 0.5)
    try:
        ffmpeg = _resolve_binary("ffmpeg")
        if ffmpeg is None:
            Path(thumb_path).unlink(missing_ok=True)
            return None
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
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
            check=False,
        )
        if result.returncode != 0:
            Path(thumb_path).unlink(missing_ok=True)
            return None
        return thumb_path
    except OSError:
        Path(thumb_path).unlink(missing_ok=True)
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


@dataclass
class _PrepareResult:
    final_path: str
    transcode_path: str | None = None
    thumb_path: str | None = None
    duration: float | None = None
    media_type: MediaType | None = None
    storage_key: str | None = None
    mime_type: str | None = None
    error: str | None = None


def _prepare_output(
    temp_path: str,
    storage_key: str,
    media_type: MediaType,
    media_id: int,
) -> _PrepareResult:
    transcode_path: str | None = None
    thumb_path: str | None = None
    try:
        probe = probe_media(temp_path)
        validation_error = validate_probe(probe)
        if validation_error:
            logger.error(
                "Media validation failed for item %s: %s", media_id, validation_error
            )
            return _PrepareResult(final_path=temp_path, error=validation_error)

        if probe.media_type:
            media_type = probe.media_type

        final_path = temp_path
        out_key = storage_key
        out_mime: str | None = None
        if needs_transcode(probe):
            suffix = ".mp4" if probe.media_type == MediaType.video else ".m4a"
            transcode_path = _scratch_temp(suffix)
            if probe.media_type == MediaType.video:
                if can_remux_video_to_mp4(probe):
                    remux_video_to_mp4(temp_path, transcode_path)
                else:
                    transcode_video(temp_path, transcode_path)
            else:
                transcode_audio(temp_path, transcode_path)
            final_path = transcode_path
            out_key, out_mime = normalize_storage_key(storage_key, probe.media_type)

        final_probe = probe_media(final_path)
        duration = final_probe.duration if final_probe.probe_ok else probe.duration
        thumb_path = _generate_thumbnail(final_path, media_type, duration)
        return _PrepareResult(
            final_path=final_path,
            transcode_path=transcode_path,
            thumb_path=thumb_path,
            duration=duration,
            media_type=media_type,
            storage_key=out_key,
            mime_type=out_mime,
        )
    except Exception:
        if transcode_path:
            Path(transcode_path).unlink(missing_ok=True)
        if thumb_path:
            Path(thumb_path).unlink(missing_ok=True)
        raise


async def process_media(session: AsyncSession, media_id: int, temp_path: str) -> None:
    storage = get_storage()
    result = await session.execute(select(MediaItem).where(MediaItem.id == media_id))
    item = result.scalar_one_or_none()
    if not item:
        Path(temp_path).unlink(missing_ok=True)
        return

    storage_key = item.storage_key
    media_type = item.media_type
    await session.rollback()

    prepared: _PrepareResult | None = None
    try:
        prepared = await asyncio.to_thread(
            _prepare_output, temp_path, storage_key, media_type, media_id
        )
        result = await session.execute(select(MediaItem).where(MediaItem.id == media_id))
        item = result.scalar_one_or_none()
        if not item:
            return

        if prepared.error:
            item.status = MediaStatus.failed
            item.processing_error = prepared.error[:500]
            item.updated_at = datetime.utcnow()
            return

        if prepared.media_type:
            item.media_type = prepared.media_type
        if prepared.storage_key:
            item.storage_key = prepared.storage_key
        if prepared.mime_type:
            item.mime_type = prepared.mime_type

        await storage.save_file(item.storage_key, prepared.final_path)
        item.file_size = Path(prepared.final_path).stat().st_size

        if prepared.thumb_path:
            thumb_key = f"thumbnails/{item.id}.jpg"
            await storage.save_file(thumb_key, prepared.thumb_path)
            item.thumbnail_key = thumb_key

        item.duration_seconds = prepared.duration
        item.status = MediaStatus.ready
        item.processing_error = None
        item.updated_at = datetime.utcnow()
    except Exception as exc:
        logger.exception("Media processing failed for item %s", media_id)
        result = await session.execute(select(MediaItem).where(MediaItem.id == media_id))
        item = result.scalar_one_or_none()
        if item:
            item.status = MediaStatus.failed
            item.processing_error = _processing_error_message(exc)
            item.updated_at = datetime.utcnow()
    finally:
        Path(temp_path).unlink(missing_ok=True)
        if prepared:
            if prepared.transcode_path:
                Path(prepared.transcode_path).unlink(missing_ok=True)
            if prepared.thumb_path:
                Path(prepared.thumb_path).unlink(missing_ok=True)
        if item:
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
