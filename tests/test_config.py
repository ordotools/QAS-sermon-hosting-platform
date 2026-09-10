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
