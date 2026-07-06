import asyncio
from collections.abc import AsyncIterator
from functools import partial

import boto3
from botocore.config import Config

from app.config import get_settings


class B2Storage:
    def __init__(self) -> None:
        settings = get_settings()
        self.bucket = settings.b2_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.b2_endpoint,
            aws_access_key_id=settings.b2_key_id,
            aws_secret_access_key=settings.b2_app_key,
            config=Config(signature_version="s3v4"),
        )

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(partial(fn, *args, **kwargs))

    async def save(self, key: str, data: bytes) -> None:
        await self._run(
            self._client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
        )

    async def save_file(self, key: str, src_path: str) -> None:
        def _upload():
            with open(src_path, "rb") as f:
                self._client.upload_fileobj(f, self.bucket, key)

        await self._run(_upload)

    async def open_stream(self, key: str) -> AsyncIterator[bytes]:
        size = await self.get_size(key)
        if size == 0:
            return
        async for chunk in self.read_range(key, 0, size - 1):
            yield chunk

    async def read_range(self, key: str, start: int, end: int | None) -> AsyncIterator[bytes]:
        kwargs: dict = {"Bucket": self.bucket, "Key": key}
        if start or end is not None:
            range_end = end if end is not None else ""
            kwargs["Range"] = f"bytes={start}-{range_end}"

        def _get():
            return self._client.get_object(**kwargs)

        response = await self._run(_get)
        body = response["Body"]

        def _read_chunks():
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

        for chunk in await self._run(lambda: list(_read_chunks())):
            yield chunk

    async def get_size(self, key: str) -> int:
        def _head():
            return self._client.head_object(Bucket=self.bucket, Key=key)

        resp = await self._run(_head)
        return resp["ContentLength"]

    async def exists(self, key: str) -> bool:
        try:
            await self.get_size(key)
            return True
        except Exception:
            return False

    async def delete(self, key: str) -> None:
        await self._run(self._client.delete_object, Bucket=self.bucket, Key=key)
