from functools import lru_cache

from app.config import get_settings
from app.storage.base import StorageBackend
from app.storage.b2 import B2Storage
from app.storage.local import LocalStorage


@lru_cache
def get_storage() -> StorageBackend:
    settings = get_settings()
    if settings.storage_backend == "b2":
        return B2Storage()
    return LocalStorage()
