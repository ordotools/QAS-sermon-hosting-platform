from functools import lru_cache

from pydantic import field_validator, model_validator
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
    max_upload_size_mb: int = 1500
    session_max_age: int = 604800
    session_cookie_samesite: str = "lax"
    debug: bool = False

    @field_validator("session_cookie_samesite", mode="before")
    @classmethod
    def _normalize_samesite(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"lax", "strict", "none"}:
            raise ValueError("SESSION_COOKIE_SAMESITE must be lax, strict, or none")
        return normalized

    @model_validator(mode="after")
    def _require_b2_credentials(self) -> "Settings":
        if self.storage_backend != "b2":
            return self
        missing = [
            name
            for name, value in (
                ("B2_KEY_ID", self.b2_key_id),
                ("B2_APP_KEY", self.b2_app_key),
                ("B2_BUCKET", self.b2_bucket),
            )
            if not str(value).strip()
        ]
        if missing:
            raise ValueError(
                "STORAGE_BACKEND=b2 requires " + ", ".join(missing)
            )
        return self

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
