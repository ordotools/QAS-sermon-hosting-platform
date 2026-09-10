import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import get_settings


@dataclass
class TusUpload:
    uid: str
    size: int
    offset: int
    metadata: dict[str, str]
    user_id: int
    media_id: int | None = None
    concat: str | None = None


def tus_dir() -> Path:
    path = Path(get_settings().local_storage_path) / ".tus"
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_path(uid: str) -> Path:
    return tus_dir() / uid


def info_path(uid: str) -> Path:
    return tus_dir() / f"{uid}.info"


def save(upload: TusUpload) -> None:
    info_path(upload.uid).write_text(json.dumps(asdict(upload)), encoding="utf-8")


def load(uid: str) -> TusUpload | None:
    path = info_path(uid)
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return TusUpload(
        uid=raw["uid"],
        size=int(raw["size"]),
        offset=int(raw["offset"]),
        metadata=dict(raw.get("metadata") or {}),
        user_id=int(raw["user_id"]),
        media_id=raw.get("media_id"),
        concat=raw.get("concat"),
    )


def create(upload: TusUpload) -> None:
    data_path(upload.uid).touch()
    save(upload)


def write_concat(dest: TusUpload, part_uids: list[str]) -> None:
    dest_path = data_path(dest.uid)
    with dest_path.open("wb") as out:
        for uid in part_uids:
            with data_path(uid).open("rb") as inp:
                shutil.copyfileobj(inp, out, length=1024 * 1024)
    dest.offset = dest.size
    save(dest)


def delete(uid: str) -> None:
    data_path(uid).unlink(missing_ok=True)
    info_path(uid).unlink(missing_ok=True)
