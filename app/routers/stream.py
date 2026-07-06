from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import MediaStatus
from app.services.media import get_media
from app.storage import get_storage

router = APIRouter(tags=["stream"])


def _parse_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header.removeprefix("bytes=").split(",")[0].strip()
    if "-" not in spec:
        return None
    start_str, end_str = spec.split("-", 1)
    start = int(start_str) if start_str else 0
    end = int(end_str) if end_str else file_size - 1
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        return None
    return start, end


@router.get("/stream/{media_id}")
async def stream_media(
    media_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    item = await get_media(session, media_id)
    if not item or item.status != MediaStatus.ready:
        raise HTTPException(status_code=404, detail="Not found")

    storage = get_storage()
    if not await storage.exists(item.storage_key):
        raise HTTPException(status_code=404, detail="File not found")

    file_size = await storage.get_size(item.storage_key)
    byte_range = _parse_range(request.headers.get("range"), file_size)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": item.mime_type,
        "Cache-Control": "private, max-age=3600",
    }

    if byte_range:
        start, end = byte_range
        content_length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        headers["Content-Length"] = str(content_length)

        async def ranged():
            async for chunk in storage.read_range(item.storage_key, start, end):
                yield chunk

        return StreamingResponse(
            ranged(),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers=headers,
            media_type=item.mime_type,
        )

    headers["Content-Length"] = str(file_size)

    async def full():
        async for chunk in storage.open_stream(item.storage_key):
            yield chunk

    return StreamingResponse(full(), headers=headers, media_type=item.mime_type)


@router.get("/thumbnail/{media_id}")
async def stream_thumbnail(
    media_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    item = await get_media(session, media_id)
    if not item or not item.thumbnail_key:
        raise HTTPException(status_code=404, detail="Not found")

    storage = get_storage()
    if not await storage.exists(item.thumbnail_key):
        raise HTTPException(status_code=404, detail="Not found")

    async def gen():
        async for chunk in storage.open_stream(item.thumbnail_key):
            yield chunk

    return StreamingResponse(gen(), media_type="image/jpeg")
