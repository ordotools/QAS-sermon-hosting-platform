import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch):
    for key in (
        "STORAGE_BACKEND",
        "B2_KEY_ID",
        "B2_APP_KEY",
        "B2_BUCKET",
        "MAX_UPLOAD_SIZE_MB",
        "COOLIFY_RESOURCE_UUID",
        "COOLIFY_URL",
        "COOLIFY_FQDN",
        "SERVICE_URL_APP_8000",
    ):
        monkeypatch.delenv(key, raising=False)


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_local_storage_does_not_need_b2_credentials():
    settings = _settings(storage_backend="local", b2_key_id="", b2_app_key="", b2_bucket="")
    assert settings.storage_backend == "local"
    assert settings.max_upload_size_mb == 1500


def test_b2_storage_requires_credentials():
    with pytest.raises(ValidationError, match="B2_KEY_ID"):
        _settings(storage_backend="b2", b2_key_id="", b2_app_key="key", b2_bucket="bucket")


def test_b2_storage_rejects_whitespace_credentials():
    with pytest.raises(ValidationError, match="B2_APP_KEY"):
        _settings(storage_backend="b2", b2_key_id="id", b2_app_key="  ", b2_bucket="bucket")


def test_b2_storage_accepts_complete_credentials():
    settings = _settings(
        storage_backend="b2",
        b2_key_id="id",
        b2_app_key="key",
        b2_bucket="bucket",
    )
    assert settings.storage_backend == "b2"


def test_b2_credentials_strip_quotes_and_whitespace():
    settings = _settings(
        storage_backend="b2",
        b2_key_id=' "0052id" ',
        b2_app_key=" 'secret' ",
        b2_bucket=" bucket ",
        b2_endpoint=" https://s3.eu-central-003.backblazeb2.com ",
    )
    assert settings.b2_key_id == "0052id"
    assert settings.b2_app_key == "secret"
    assert settings.b2_bucket == "bucket"
    assert settings.b2_endpoint == "https://s3.eu-central-003.backblazeb2.com"


def test_storage_backend_normalizes_case():
    settings = _settings(
        storage_backend="B2",
        b2_key_id="id",
        b2_app_key="key",
        b2_bucket="bucket",
    )
    assert settings.storage_backend == "b2"


def test_storage_backend_rejects_unknown():
    with pytest.raises(ValidationError, match="STORAGE_BACKEND"):
        _settings(storage_backend="s3")


def test_coolify_forces_b2_even_if_storage_backend_is_local(monkeypatch):
    monkeypatch.setenv("COOLIFY_RESOURCE_UUID", "resource-id")
    settings = _settings(
        storage_backend="local",
        b2_key_id="id",
        b2_app_key="key",
        b2_bucket="bucket",
    )
    assert settings.storage_backend == "b2"


def test_coolify_still_requires_b2_credentials(monkeypatch):
    monkeypatch.setenv("COOLIFY_RESOURCE_UUID", "resource-id")
    with pytest.raises(ValidationError, match="B2_KEY_ID"):
        _settings(storage_backend="local", b2_key_id="", b2_app_key="key", b2_bucket="bucket")
