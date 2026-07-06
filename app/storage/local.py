import os
from collections.abc import AsyncIterator
from pathlib import Path

import aiofiles

from app.config import get_settings


class LocalStorage:
    def __init__(self, base_path: str | None = None) -> None:
        settings = get_settings()
        self.base_path = Path(base_path or settings.local_storage_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = self.base_path / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def save(self, key: str, data: bytes) -> None:
        path = self._path(key)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)

    async def save_file(self, key: str, src_path: str) -> None:
        dest = self._path(key)
        async with aiofiles.open(src_path, "rb") as src:
            async with aiofiles.open(dest, "wb") as dst:
                while chunk := await src.read(1024 * 1024):
                    await dst.write(chunk)

    async def open_stream(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        async with aiofiles.open(path, "rb") as f:
            while chunk := await f.read(1024 * 1024):
                yield chunk

    async def read_range(self, key: str, start: int, end: int | None) -> AsyncIterator[bytes]:
        path = self._path(key)
        file_size = path.stat().st_size
        if end is None or end >= file_size:
            end = file_size - 1
        async with aiofiles.open(path, "rb") as f:
            await f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk_size = min(1024 * 1024, remaining)
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)

    async def get_size(self, key: str) -> int:
        return self._path(key).stat().st_size

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            os.remove(path)
