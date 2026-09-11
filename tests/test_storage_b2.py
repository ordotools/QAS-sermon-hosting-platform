from app.storage.b2 import B2Storage, _boto_config, _friendly_b2_error, region_from_endpoint


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
