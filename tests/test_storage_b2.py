import pytest
from botocore.exceptions import ConnectionClosedError

from app.storage.b2 import (
    B2Storage,
    _boto_config,
    _friendly_b2_error,
    region_from_endpoint,
)


def _b2_settings(**overrides):
    from app.config import Settings

    values = dict(
        _env_file=None,
        storage_backend="b2",
        b2_key_id="id",
        b2_app_key="key",
        b2_bucket="sermons",
        b2_endpoint="https://s3.us-west-004.backblazeb2.com",
    )
    values.update(overrides)
    return Settings(**values)


def _storage(monkeypatch, **overrides) -> B2Storage:
    from app.storage import b2 as b2_mod

    settings = _b2_settings(**overrides)
    monkeypatch.setattr(b2_mod, "get_settings", lambda: settings)
    return B2Storage()


def test_region_from_b2_endpoint() -> None:
    assert region_from_endpoint("https://s3.us-west-004.backblazeb2.com") == "us-west-004"
    assert region_from_endpoint("https://s3.eu-central-003.backblazeb2.com") == "eu-central-003"


def test_friendly_error_for_invalid_key() -> None:
    from botocore.exceptions import ClientError

    exc = ClientError(
        {
            "Error": {
                "Code": "InvalidAccessKeyId",
                "Message": "The key '0052d2ac64d45d400000000003' is not valid",
            }
        },
        "CreateMultipartUpload",
    )
    err = _friendly_b2_error(exc)
    assert "B2_ENDPOINT" in str(err)
    assert "0052" not in str(err)


def test_boto_config_disables_default_checksums() -> None:
    config = _boto_config()
    assert config.s3.get("addressing_style") == "path"
    assert config.s3.get("payload_signing_enabled") is False
    checksum = getattr(config, "request_checksum_calculation", None)
    if checksum is not None:
        assert checksum == "when_required"
    validation = getattr(config, "response_checksum_validation", None)
    if validation is not None:
        assert validation == "when_required"
    retries = getattr(config, "retries", None) or {}
    assert retries.get("max_attempts") == 8
    assert retries.get("mode") == "standard"


def test_friendly_error_for_closed_connection() -> None:
    exc = ConnectionClosedError(endpoint_url="https://s3.us-east-005.backblazeb2.com/bucket/key")
    err = _friendly_b2_error(exc)
    assert isinstance(err, RuntimeError)
    assert not isinstance(err, ConnectionClosedError)
    assert "Connection was closed" in str(err)


def test_b2_storage_uses_bucket_and_endpoint(monkeypatch) -> None:
    storage = _storage(monkeypatch)
    assert storage.bucket == "sermons"
    assert storage._client.meta.endpoint_url == "https://s3.us-west-004.backblazeb2.com"
    assert storage._client.meta.region_name == "us-west-004"


@pytest.mark.asyncio
async def test_save_file_wraps_closed_connection(monkeypatch, tmp_path) -> None:
    storage = _storage(
        monkeypatch,
        b2_endpoint="https://s3.us-east-005.backblazeb2.com",
    )

    def _fail(*_args, **_kwargs):
        raise ConnectionClosedError(
            endpoint_url="https://s3.us-east-005.backblazeb2.com/sermons/media/a.mp4"
        )

    storage._client.put_object = _fail
    src = tmp_path / "a.mp4"
    src.write_bytes(b"not-a-real-video")

    with pytest.raises(RuntimeError, match="Connection was closed") as caught:
        await storage.save_file("media/a.mp4", str(src))
    assert not isinstance(caught.value, ConnectionClosedError)


@pytest.mark.asyncio
async def test_save_file_small_uses_put_object(monkeypatch, tmp_path) -> None:
    storage = _storage(monkeypatch)
    calls: list[str] = []

    def _put_object(**kwargs):
        calls.append("put_object")
        assert kwargs["Body"] == b"tiny"

    storage._client.put_object = _put_object
    storage._client.create_multipart_upload = lambda **_k: (_ for _ in ()).throw(
        AssertionError("multipart should not start")
    )
    src = tmp_path / "tiny.mp4"
    src.write_bytes(b"tiny")
    await storage.save_file("media/tiny.mp4", str(src))
    assert calls == ["put_object"]


@pytest.mark.asyncio
async def test_save_file_large_uses_multipart(monkeypatch, tmp_path) -> None:
    from app.storage import b2 as b2_mod

    monkeypatch.setattr(b2_mod, "_MULTIPART_THRESHOLD", 8)
    monkeypatch.setattr(b2_mod, "_MULTIPART_CHUNKSIZE", 8)
    storage = _storage(monkeypatch)
    calls: list[tuple] = []

    def _create(**kwargs):
        calls.append(("create", kwargs["Key"]))
        return {"UploadId": "u1"}

    def _part(**kwargs):
        calls.append(("part", kwargs["PartNumber"], kwargs["Body"], kwargs["ContentLength"]))
        return {"ETag": f'"{kwargs["PartNumber"]}"'}

    def _complete(**kwargs):
        calls.append(("complete", kwargs["UploadId"], kwargs["MultipartUpload"]["Parts"]))

    storage._client.create_multipart_upload = _create
    storage._client.upload_part = _part
    storage._client.complete_multipart_upload = _complete
    storage._client.put_object = lambda **_k: (_ for _ in ()).throw(
        AssertionError("put_object should not run")
    )
    src = tmp_path / "big.mp4"
    src.write_bytes(b"abcdefghijklmnop")  # 16 bytes -> two 8-byte parts
    await storage.save_file("media/big.mp4", str(src))
    assert calls[0] == ("create", "media/big.mp4")
    assert calls[1] == ("part", 1, b"abcdefgh", 8)
    assert calls[2] == ("part", 2, b"ijklmnop", 8)
    assert calls[3][0] == "complete"
    assert calls[3][2] == [
        {"ETag": '"1"', "PartNumber": 1},
        {"ETag": '"2"', "PartNumber": 2},
    ]


@pytest.mark.asyncio
async def test_save_file_aborts_multipart_on_part_failure(monkeypatch, tmp_path) -> None:
    from app.storage import b2 as b2_mod

    monkeypatch.setattr(b2_mod, "_MULTIPART_THRESHOLD", 8)
    monkeypatch.setattr(b2_mod, "_MULTIPART_CHUNKSIZE", 8)
    storage = _storage(monkeypatch)
    aborted: list[str] = []

    storage._client.create_multipart_upload = lambda **_k: {"UploadId": "u1"}
    storage._client.upload_part = lambda **_k: (_ for _ in ()).throw(
        ConnectionClosedError(endpoint_url="https://s3.example/part")
    )

    def _abort(**kwargs):
        aborted.append(kwargs["UploadId"])

    storage._client.abort_multipart_upload = _abort
    src = tmp_path / "big.mp4"
    src.write_bytes(b"abcdefghijklmnop")
    with pytest.raises(RuntimeError, match="Connection was closed"):
        await storage.save_file("media/big.mp4", str(src))
    assert aborted == ["u1"]
