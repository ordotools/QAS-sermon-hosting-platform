import pytest
from botocore.exceptions import ConnectionClosedError

from app.storage.b2 import (
    B2Storage,
    _boto_config,
    _friendly_b2_error,
    _transfer_config,
    region_from_endpoint,
)


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
    checksum = getattr(config, "request_checksum_calculation", None)
    if checksum is not None:
        assert checksum == "when_required"
    validation = getattr(config, "response_checksum_validation", None)
    if validation is not None:
        assert validation == "when_required"
    retries = getattr(config, "retries", None) or {}
    assert retries.get("max_attempts") == 8
    assert retries.get("mode") == "standard"


def test_transfer_config_uses_multipart_below_sermon_size() -> None:
    config = _transfer_config()
    assert config.multipart_threshold < 100 * 1024 * 1024
    assert config.multipart_chunksize >= 5 * 1024 * 1024
    assert config.max_concurrency == 4


def test_friendly_error_for_closed_connection() -> None:
    exc = ConnectionClosedError(endpoint_url="https://s3.us-east-005.backblazeb2.com/bucket/key")
    err = _friendly_b2_error(exc)
    assert isinstance(err, RuntimeError)
    assert not isinstance(err, ConnectionClosedError)
    assert "Connection was closed" in str(err)


def test_b2_storage_uses_bucket_and_endpoint(monkeypatch) -> None:
    from app.config import Settings
    from app.storage import b2 as b2_mod

    settings = Settings(
        _env_file=None,
        storage_backend="b2",
        b2_key_id="id",
        b2_app_key="key",
        b2_bucket="sermons",
        b2_endpoint="https://s3.us-west-004.backblazeb2.com",
    )
    monkeypatch.setattr(b2_mod, "get_settings", lambda: settings)
    storage = B2Storage()
    assert storage.bucket == "sermons"
    assert storage._client.meta.endpoint_url == "https://s3.us-west-004.backblazeb2.com"
    assert storage._client.meta.region_name == "us-west-004"


@pytest.mark.asyncio
async def test_save_file_wraps_closed_connection(monkeypatch, tmp_path) -> None:
    from app.config import Settings
    from app.storage import b2 as b2_mod

    settings = Settings(
        _env_file=None,
        storage_backend="b2",
        b2_key_id="id",
        b2_app_key="key",
        b2_bucket="sermons",
        b2_endpoint="https://s3.us-east-005.backblazeb2.com",
    )
    monkeypatch.setattr(b2_mod, "get_settings", lambda: settings)
    storage = B2Storage()

    def _fail(*_args, **_kwargs):
        raise ConnectionClosedError(
            endpoint_url="https://s3.us-east-005.backblazeb2.com/sermons/media/a.mp4"
        )

    storage._client.upload_fileobj = _fail
    src = tmp_path / "a.mp4"
    src.write_bytes(b"not-a-real-video")

    with pytest.raises(RuntimeError, match="Connection was closed") as caught:
        await storage.save_file("media/a.mp4", str(src))
    assert not isinstance(caught.value, ConnectionClosedError)
