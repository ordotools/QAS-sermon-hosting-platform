from app.storage.b2 import B2Storage, _boto_config


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
