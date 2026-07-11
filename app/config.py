from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.db_url import normalize_async_database_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "dev-secret-change-me"
    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_async_database_url(value)
    storage_backend: str = "local"
    local_storage_path: str = "./data/media"
    b2_key_id: str = ""
    b2_app_key: str = ""
    b2_bucket: str = ""
    b2_endpoint: str = "https://s3.us-west-004.backblazeb2.com"
    superuser_email: str = ""
    superuser_password: str = ""
    max_upload_size_mb: int = 500
    session_max_age: int = 604800
    debug: bool = False

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def secure_cookies(self) -> bool:
        return not self.debug


@lru_cache
def get_settings() -> Settings:
    return Settings()
