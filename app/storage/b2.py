import asyncio
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings
from app.storage.base import ObjectNotFoundError, StorageUnavailableError

logger = logging.getLogger(__name__)

_MULTIPART_THRESHOLD = 8 * 1024 * 1024
_MULTIPART_CHUNKSIZE = 8 * 1024 * 1024
_MULTIPART_WORKERS = 4
_STREAM_CHUNK = 1024 * 1024
_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webm": "video/webm",
    ".ogg": "audio/ogg",
    ".mov": "video/quicktime",
    ".wav": "audio/wav",
}
_UPLOAD_ERRORS = (BotoCoreError, ClientError, S3UploadFailedError)
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def content_type_for_key(key: str) -> str:
    return _CONTENT_TYPES.get(Path(key).suffix.lower(), "application/octet-stream")


def region_from_endpoint(endpoint: str) -> str:
    host = urlparse(endpoint).hostname or ""
    parts = host.split(".")
    if len(parts) >= 2 and parts[0] == "s3" and parts[1]:
        return parts[1]
    raise ValueError(
        "Cannot parse B2 region from B2_ENDPOINT="
        f"{endpoint!r}. Use the S3 endpoint from the B2 bucket page, "
        "e.g. https://s3.us-west-004.backblazeb2.com"
    )


def _boto_config() -> Config:
    # boto3 1.36+ sends checksum headers B2 does not accept. UNSIGNED-PAYLOAD
    # avoids aws-chunked UploadPart signatures that B2 rejects.
    kwargs: dict = {
        "signature_version": "s3v4",
        "s3": {
            "addressing_style": "path",
            "payload_signing_enabled": False,
        },
        "retries": {"max_attempts": 8, "mode": "standard"},
        "request_checksum_calculation": "when_required",
        "response_checksum_validation": "when_required",
    }
    try:
        return Config(**kwargs)
    except TypeError:
        kwargs.pop("request_checksum_calculation", None)
        kwargs.pop("response_checksum_validation", None)
        return Config(**kwargs)


def _client_error(exc: BaseException) -> ClientError | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ClientError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _friendly_b2_error(exc: BaseException) -> RuntimeError:
    client_exc = _client_error(exc)
    if client_exc is None:
        return RuntimeError(str(exc))
    error = (client_exc.response or {}).get("Error") or {}
    code = str(error.get("Code") or "")
    message = str(error.get("Message") or client_exc)
    if code == "InvalidAccessKeyId":
        return RuntimeError(
            "Backblaze rejected B2_KEY_ID. Use the application keyID (not the "
            "account ID), and set B2_ENDPOINT to this bucket's S3 endpoint "
            "from the B2 bucket page (region must match the key)."
        )
    return RuntimeError(f"Backblaze upload failed ({code or 'error'}): {message}")


def _is_not_found(exc: BaseException) -> bool:
    client_exc = _client_error(exc)
    if client_exc is None:
        return False
    response = client_exc.response or {}
    error = response.get("Error") or {}
    code = str(error.get("Code") or "")
    if code == "NoSuchBucket":
        return False
    if code in _NOT_FOUND_CODES:
        return True
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return status == 404


def _storage_error(exc: BaseException, key: str = "") -> Exception:
    if _is_not_found(exc):
        return ObjectNotFoundError(key)
    return StorageUnavailableError(str(_friendly_b2_error(exc)))


class B2Storage:
    def __init__(self) -> None:
        settings = get_settings()
        self.bucket = settings.b2_bucket
        endpoint = settings.b2_endpoint
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region_from_endpoint(endpoint),
            aws_access_key_id=settings.b2_key_id,
            aws_secret_access_key=settings.b2_app_key,
            config=_boto_config(),
        )

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(partial(fn, *args, **kwargs))

    def _put_object(self, key: str, data: bytes) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type_for_key(key),
        )

    def _upload_part_slice(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        src_path: str,
        offset: int,
        size: int,
    ) -> dict:
        with open(src_path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(size)
        resp = self._client.upload_part(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data,
            ContentLength=len(data),
        )
        return {"ETag": resp["ETag"], "PartNumber": part_number}

    def _upload_multipart(self, key: str, src_path: str) -> None:
        upload_id: str | None = None
        try:
            created = self._client.create_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                ContentType=content_type_for_key(key),
            )
            upload_id = created["UploadId"]
            file_size = Path(src_path).stat().st_size
            slices: list[tuple[int, int, int]] = []
            offset = 0
            part_number = 1
            while offset < file_size:
                length = min(_MULTIPART_CHUNKSIZE, file_size - offset)
                slices.append((part_number, offset, length))
                offset += length
                part_number += 1
            workers = max(1, min(_MULTIPART_WORKERS, len(slices)))
            parts_by_number: dict[int, dict] = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self._upload_part_slice,
                        key,
                        upload_id,
                        number,
                        src_path,
                        start,
                        length,
                    ): number
                    for number, start, length in slices
                }
                try:
                    for fut in as_completed(futures):
                        parts_by_number[futures[fut]] = fut.result()
                except Exception:
                    for fut in futures:
                        fut.cancel()
                    raise
            parts = [parts_by_number[n] for n in sorted(parts_by_number)]
            self._client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            upload_id = None
        finally:
            if upload_id is not None:
                try:
                    self._client.abort_multipart_upload(
                        Bucket=self.bucket, Key=key, UploadId=upload_id
                    )
                except Exception:
                    logger.exception("Failed to abort B2 multipart upload %s", key)

    def _upload_file(self, key: str, src_path: str) -> None:
        size = Path(src_path).stat().st_size
        if size < _MULTIPART_THRESHOLD:
            self._put_object(key, Path(src_path).read_bytes())
            return
        self._upload_multipart(key, src_path)

    async def save(self, key: str, data: bytes) -> None:
        try:
            await self._run(self._put_object, key, data)
        except _UPLOAD_ERRORS as exc:
            raise _friendly_b2_error(exc) from exc

    async def save_file(self, key: str, src_path: str) -> None:
        logger.info("Uploading %s to B2 bucket %s", key, self.bucket)
        try:
            await self._run(self._upload_file, key, src_path)
        except _UPLOAD_ERRORS as exc:
            raise _friendly_b2_error(_client_error(exc) or exc) from exc
        logger.info("Uploaded %s to B2 bucket %s", key, self.bucket)

    async def open_stream(self, key: str) -> AsyncIterator[bytes]:
        async for chunk in self.read_range(key, 0, None):
            yield chunk

    async def read_range(self, key: str, start: int, end: int | None) -> AsyncIterator[bytes]:
        kwargs: dict = {"Bucket": self.bucket, "Key": key}
        if start or end is not None:
            range_end = end if end is not None else ""
            kwargs["Range"] = f"bytes={start}-{range_end}"

        def _get():
            return self._client.get_object(**kwargs)

        try:
            response = await self._run(_get)
        except _UPLOAD_ERRORS as exc:
            raise _storage_error(exc, key) from exc

        body = response["Body"]
        try:
            while True:
                chunk = await self._run(body.read, _STREAM_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                try:
                    await self._run(close)
                except Exception:
                    logger.exception("Failed to close B2 stream for %s", key)

    async def get_size(self, key: str) -> int:
        def _head():
            return self._client.head_object(Bucket=self.bucket, Key=key)

        try:
            resp = await self._run(_head)
        except _UPLOAD_ERRORS as exc:
            raise _storage_error(exc, key) from exc
        return resp["ContentLength"]

    async def exists(self, key: str) -> bool:
        try:
            await self.get_size(key)
            return True
        except ObjectNotFoundError:
            return False

    async def check(self) -> None:
        def _head():
            self._client.head_bucket(Bucket=self.bucket)

        try:
            await self._run(_head)
        except _UPLOAD_ERRORS as exc:
            message = str(_friendly_b2_error(exc))
            logger.error(
                "B2 head_bucket failed for bucket %s endpoint check: %s",
                self.bucket,
                message,
            )
            raise StorageUnavailableError(message) from exc

    async def delete(self, key: str) -> None:
        await self._run(self._client.delete_object, Bucket=self.bucket, Key=key)
