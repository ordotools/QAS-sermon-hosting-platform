"""Media format probing, validation, and transcoding helpers."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.models import MediaType

logger = logging.getLogger(__name__)

_COMMON_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
)

ALLOWED_EXTENSIONS = frozenset({".mp4", ".mov", ".m4a", ".mp3", ".webm", ".ogg"})

TARGET_VIDEO_MIME = "video/mp4"
TARGET_AUDIO_MIME = "audio/mp4"

VIDEO_MIME_HINTS = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/ogg",
        "video/quicktime",
    }
)

AUDIO_MIME_HINTS = frozenset(
    {
        "audio/mpeg",
        "audio/mp4",
        "audio/x-m4a",
        "audio/ogg",
        "audio/webm",
        "audio/wav",
    }
)

PASSTHROUGH_AUDIO_CODECS = frozenset({"aac", "mp3"})
AAC_COMPATIBLE_AUDIO_CODECS = frozenset({"aac"})
PASSTHROUGH_VIDEO_CODECS = frozenset({"h264"})
IMAGE_VIDEO_CODECS = frozenset({"mjpeg", "png", "bmp", "gif", "webp", "apng"})
TRANSCODE_CONTAINER_FORMATS = frozenset(
    {"webm", "matroska", "ogg", "flv", "avi", "wmv", "asf", "mpegts", "mpeg", "wav"}
)


@dataclass(frozen=True)
class MediaProbe:
    media_type: MediaType | None
    video_codec: str | None
    audio_codec: str | None
    duration: float | None
    container: str | None
    has_video: bool
    has_audio: bool
    probe_ok: bool


@lru_cache
def _resolve_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for directory in _COMMON_BIN_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def ffprobe_available() -> bool:
    return _resolve_binary("ffprobe") is not None


def ffmpeg_available() -> bool:
    return _resolve_binary("ffmpeg") is not None


def media_tools_error() -> str:
    return (
        "Media processing is unavailable: ffmpeg/ffprobe is not installed "
        "or not on PATH. Install ffmpeg and restart the server."
    )


def _run_ffprobe(path: str) -> dict:
    ffprobe = _resolve_binary("ffprobe")
    if ffprobe is None:
        logger.error("ffprobe not found; cannot probe %s", path)
        return {}

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        logger.debug("ffprobe failed for %s: %s", path, exc)
        return {}


def _is_attached_pic(stream: dict) -> bool:
    return stream.get("disposition", {}).get("attached_pic", 0) == 1


def _video_streams(streams: list[dict]) -> list[dict]:
    return [
        stream
        for stream in streams
        if stream.get("codec_type") == "video" and not _is_attached_pic(stream)
    ]


def _audio_streams(streams: list[dict]) -> list[dict]:
    return [stream for stream in streams if stream.get("codec_type") == "audio"]


def _extract_duration(probe: dict) -> float | None:
    fmt = probe.get("format", {})
    duration = fmt.get("duration")
    if duration:
        return float(duration)
    for stream in probe.get("streams", []):
        if stream.get("duration"):
            return float(stream["duration"])
    return None


def _primary_container(format_name: str | None) -> str | None:
    if not format_name:
        return None
    return format_name.split(",")[0].strip().lower()


def probe_media(path: str) -> MediaProbe:
    raw = _run_ffprobe(path)
    if not raw:
        return MediaProbe(
            media_type=None,
            video_codec=None,
            audio_codec=None,
            duration=None,
            container=None,
            has_video=False,
            has_audio=False,
            probe_ok=False,
        )

    streams = raw.get("streams", [])
    videos = _video_streams(streams)
    audios = _audio_streams(streams)
    container = raw.get("format", {}).get("format_name")

    video_codec = videos[0].get("codec_name") if videos else None
    audio_codec = audios[0].get("codec_name") if audios else None

    if videos:
        media_type = MediaType.video
    elif audios:
        media_type = MediaType.audio
    else:
        media_type = None

    return MediaProbe(
        media_type=media_type,
        video_codec=video_codec,
        audio_codec=audio_codec,
        duration=_extract_duration(raw),
        container=container,
        has_video=bool(videos),
        has_audio=bool(audios),
        probe_ok=True,
    )


def validate_extension(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        label = ext or "no extension"
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        return f"Unsupported file type ({label}). Allowed: {allowed}"
    return None


def validate_probe(probe: MediaProbe) -> str | None:
    if not probe.probe_ok:
        if not ffprobe_available():
            return media_tools_error()
        return "Could not read file. It may be corrupt or not a supported media format."

    if not probe.has_video and not probe.has_audio:
        return "File has no audio or video content."

    if probe.media_type is None:
        return "Could not determine media type."

    if (
        probe.has_video
        and not probe.has_audio
        and probe.video_codec in IMAGE_VIDEO_CODECS
    ):
        return "Images are not supported. Upload audio or video files only."

    return None


def _needs_audio_transcode(codec: str | None) -> bool:
    if not codec:
        return False
    if codec in PASSTHROUGH_AUDIO_CODECS:
        return False
    if codec.startswith(("pcm_s", "pcm_f", "pcm_u", "pcm_alaw", "pcm_mulaw")):
        return True
    return codec in {"alac", "flac", "opus", "vorbis", "wmav2", "wma", "aac3", "eac3"}


def _container_needs_transcode(container: str | None) -> bool:
    primary = _primary_container(container)
    if not primary:
        return True
    if primary == "mov":
        return True
    return primary in TRANSCODE_CONTAINER_FORMATS


def needs_transcode(probe: MediaProbe) -> bool:
    if probe.media_type == MediaType.video:
        if probe.video_codec not in PASSTHROUGH_VIDEO_CODECS:
            return True
        if probe.audio_codec and probe.audio_codec not in AAC_COMPATIBLE_AUDIO_CODECS:
            return True
        if _container_needs_transcode(probe.container):
            return True
        return False

    if probe.media_type == MediaType.audio:
        return _needs_audio_transcode(probe.audio_codec)

    return True


def can_remux_video_to_mp4(probe: MediaProbe) -> bool:
    if probe.media_type != MediaType.video:
        return False
    if probe.video_codec not in PASSTHROUGH_VIDEO_CODECS:
        return False
    if probe.audio_codec and probe.audio_codec not in AAC_COMPATIBLE_AUDIO_CODECS:
        return False
    return _container_needs_transcode(probe.container)


def remux_video_to_mp4(src: str, dest: str) -> None:
    ffmpeg = _resolve_binary("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found")

    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            src,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            dest,
        ],
        capture_output=True,
        check=True,
    )


def transcode_video(src: str, dest: str) -> None:
    ffmpeg = _resolve_binary("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found")

    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            src,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            dest,
        ],
        capture_output=True,
        check=True,
    )


def transcode_audio(src: str, dest: str) -> None:
    ffmpeg = _resolve_binary("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found")

    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            src,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            dest,
        ],
        capture_output=True,
        check=True,
    )


def normalize_storage_key(storage_key: str, media_type: MediaType) -> tuple[str, str]:
    path = Path(storage_key)
    if media_type == MediaType.video:
        return str(path.with_suffix(".mp4")), TARGET_VIDEO_MIME
    return str(path.with_suffix(".m4a")), TARGET_AUDIO_MIME


def target_mime_type(media_type: MediaType) -> str:
    if media_type == MediaType.video:
        return TARGET_VIDEO_MIME
    return TARGET_AUDIO_MIME
