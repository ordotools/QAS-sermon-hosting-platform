import asyncio
import logging
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import get_settings
from app.models import MediaItem, MediaStatus, MediaType
from app.services.media_formats import (
    THUMBNAIL_TIMEOUT_SECONDS,
    MediaProbe,
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
_SAVE_ATTEMPTS = 3
_SAVE_BACKOFF_SECONDS = 0.5
STALE_PROCESSING_MINUTES = 45
RETRYABLE_UPLOAD_PREFIX = "Retryable upload failure:"
TUS_TTL_SECONDS = 24 * 3600
SCRATCH_TTL_SECONDS = 2 * 3600
_in_flight_paths: set[str] = set()


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
            timeout=THUMBNAIL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            Path(thumb_path).unlink(missing_ok=True)
            return None
        return thumb_path
    except (OSError, subprocess.TimeoutExpired):
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
    probe: MediaProbe | None = None,
) -> _PrepareResult:
    transcode_path: str | None = None
    thumb_path: str | None = None
    try:
        if probe is None or not probe.probe_ok:
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
        else:
            duration = probe.duration

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


async def _save_file_with_retry(storage, key: str, src_path: str) -> None:
    last: BaseException | None = None
    for attempt in range(1, _SAVE_ATTEMPTS + 1):
        try:
            await storage.save_file(key, src_path)
            return
        except Exception as exc:
            last = exc
            logger.warning(
                "save_file failed for %s (attempt %s/%s): %s",
                key,
                attempt,
                _SAVE_ATTEMPTS,
                exc,
            )
            if attempt < _SAVE_ATTEMPTS:
                await asyncio.sleep(_SAVE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    assert last is not None
    raise last


async def process_media(
    session: AsyncSession,
    media_id: int,
    temp_path: str,
    probe: MediaProbe | None = None,
) -> None:
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
    saved_media = False
    keep_scratch = False
    try:
        prepared = await asyncio.to_thread(
            _prepare_output, temp_path, storage_key, media_type, media_id, probe
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

        await _save_file_with_retry(storage, item.storage_key, prepared.final_path)
        saved_media = True
        item.file_size = Path(prepared.final_path).stat().st_size

        if prepared.thumb_path:
            thumb_key = f"thumbnails/{item.id}.jpg"
            try:
                await _save_file_with_retry(storage, thumb_key, prepared.thumb_path)
                item.thumbnail_key = thumb_key
            except Exception:
                logger.exception(
                    "Thumbnail upload failed for item %s; publishing without thumbnail",
                    media_id,
                )

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
            if prepared is not None and not prepared.error and not saved_media:
                keep_scratch = True
                item.processing_error = (
                    RETRYABLE_UPLOAD_PREFIX + " " + _processing_error_message(exc)
                )[:500]
            else:
                item.processing_error = _processing_error_message(exc)
            item.updated_at = datetime.utcnow()
    finally:
        if not keep_scratch:
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


def _resolved(path: str | Path) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def mark_temp_in_flight(path: str) -> None:
    _in_flight_paths.add(_resolved(path))


def unmark_temp_in_flight(path: str) -> None:
    _in_flight_paths.discard(_resolved(path))


def _keep_temp_for_item(item: MediaItem) -> bool:
    if item.status == MediaStatus.processing:
        return True
    if item.status == MediaStatus.failed and (item.processing_error or "").startswith(
        RETRYABLE_UPLOAD_PREFIX
    ):
        return True
    return False


def _cleanup_temp_files(keep_media_ids: set[int]) -> None:
    from app.services.tus_store import data_path, delete, load, tus_dir

    now = time.time()
    known_uids: set[str] = set()
    for info in tus_dir().glob("*.info"):
        uid = info.stem
        known_uids.add(uid)
        upload = load(uid)
        data = data_path(uid)
        mtimes = [info.stat().st_mtime]
        if data.is_file():
            mtimes.append(data.stat().st_mtime)
        age = now - max(mtimes)
        keep = False
        if upload and upload.media_id in keep_media_ids:
            keep = True
        elif _resolved(data) in _in_flight_paths:
            keep = True
        elif age < TUS_TTL_SECONDS:
            keep = True
        if not keep:
            delete(uid)

    for data in tus_dir().iterdir():
        if not data.is_file() or data.suffix == ".info" or data.name in known_uids:
            continue
        if _resolved(data) in _in_flight_paths:
            continue
        if now - data.stat().st_mtime >= TUS_TTL_SECONDS:
            data.unlink(missing_ok=True)

    scratch = scratch_dir()
    for path in scratch.iterdir():
        if not path.is_file():
            continue
        if _resolved(path) in _in_flight_paths:
            continue
        if now - path.stat().st_mtime >= SCRATCH_TTL_SECONDS:
            path.unlink(missing_ok=True)


async def recover_stale_processing(
    session: AsyncSession,
    *,
    older_than_minutes: int = STALE_PROCESSING_MINUTES,
) -> list[tuple[int, str]]:
    from app.services.tus_store import find_path_for_media

    cutoff = datetime.utcnow() - timedelta(minutes=older_than_minutes)
    result = await session.execute(
        select(MediaItem).where(MediaItem.status == MediaStatus.processing)
    )
    retry: list[tuple[int, str]] = []
    changed = False
    for item in result.scalars().all():
        if older_than_minutes > 0 and item.updated_at > cutoff:
            continue
        path = find_path_for_media(item.id) if item.id is not None else None
        if path is not None:
            retry.append((item.id, str(path)))
            continue
        item.status = MediaStatus.failed
        item.processing_error = (
            "Processing interrupted: the worker stopped before finishing. "
            "Please upload again."
        )[:500]
        item.updated_at = datetime.utcnow()
        session.add(item)
        changed = True
    if changed:
        await session.commit()
    return retry


async def cleanup_abandoned_temps(session: AsyncSession) -> None:
    result = await session.execute(select(MediaItem))
    keep_ids = {item.id for item in result.scalars().all() if item.id and _keep_temp_for_item(item)}
    await asyncio.to_thread(_cleanup_temp_files, keep_ids)
