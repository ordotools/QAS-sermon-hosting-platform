"""Unit tests for media format probing, validation, and transcode decisions."""

from __future__ import annotations

import pytest

from pathlib import Path

from app.models import MediaType
from app.services.media_formats import (
    MediaProbe,
    _resolve_binary,
    ffprobe_available,
    media_tools_error,
    needs_transcode,
    normalize_storage_key,
    probe_media,
    validate_extension,
    validate_probe,
)

HEVC_MOV_PROBE = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.5"},
    "streams": [
        {"codec_type": "video", "codec_name": "hevc"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}

H264_MP4_PROBE = {
    "format": {"format_name": "mp4", "duration": "30.0"},
    "streams": [
        {"codec_type": "video", "codec_name": "h264"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}

H264_MOV_PROBE = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "30.0"},
    "streams": [
        {"codec_type": "video", "codec_name": "h264"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}

ALAC_M4A_PROBE = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "5.0"},
    "streams": [
        {"codec_type": "audio", "codec_name": "alac"},
    ],
}

AAC_M4A_PROBE = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "5.0"},
    "streams": [
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}

MP3_PROBE = {
    "format": {"format_name": "mp3", "duration": "180.0"},
    "streams": [
        {"codec_type": "audio", "codec_name": "mp3"},
    ],
}

WEBM_PROBE = {
    "format": {"format_name": "webm", "duration": "10.0"},
    "streams": [
        {"codec_type": "video", "codec_name": "vp9"},
        {"codec_type": "audio", "codec_name": "opus"},
    ],
}

MJPEG_PROBE = {
    "format": {"format_name": "image2", "duration": None},
    "streams": [
        {"codec_type": "video", "codec_name": "mjpeg"},
    ],
}

ATTACHED_PIC_PROBE = {
    "format": {"format_name": "mp3", "duration": "60.0"},
    "streams": [
        {"codec_type": "audio", "codec_name": "mp3"},
        {
            "codec_type": "video",
            "codec_name": "png",
            "disposition": {"attached_pic": 1},
        },
    ],
}


def _probe(**kwargs: object) -> MediaProbe:
    defaults: dict = {
        "media_type": None,
        "video_codec": None,
        "audio_codec": None,
        "duration": None,
        "container": None,
        "has_video": False,
        "has_audio": False,
        "probe_ok": True,
    }
    defaults.update(kwargs)
    return MediaProbe(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("sermon.mp3", None),
        ("clip.MOV", None),
        ("notes.pdf", "Unsupported file type (.pdf)"),
        ("image.heic", "Unsupported file type (.heic)"),
        ("noext", "Unsupported file type (no extension)"),
    ],
)
def test_validate_extension(filename: str, expected: str | None) -> None:
    result = validate_extension(filename)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert expected in result


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (_probe(probe_ok=False), "Could not read file"),
        (_probe(has_audio=False, has_video=False), "no audio or video"),
        (_probe(
            media_type=MediaType.video,
            has_video=True,
            video_codec="mjpeg",
            has_audio=False,
        ), "Images are not supported"),
        (_probe(media_type=MediaType.audio, has_audio=True, audio_codec="mp3"), None),
    ],
)
def test_validate_probe(probe: MediaProbe, expected: str | None) -> None:
    result = validate_probe(probe)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert expected.lower() in result.lower()


@pytest.mark.parametrize(
    ("fixture", "transcode"),
    [
        (HEVC_MOV_PROBE, True),
        (H264_MOV_PROBE, True),
        (H264_MP4_PROBE, False),
        (ALAC_M4A_PROBE, True),
        (AAC_M4A_PROBE, False),
        (MP3_PROBE, False),
        (WEBM_PROBE, True),
    ],
)
def test_needs_transcode_from_ffprobe_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    fixture: dict,
    transcode: bool,
) -> None:
    monkeypatch.setattr(
        "app.services.media_formats._run_ffprobe",
        lambda _path: fixture,
    )
    probe = probe_media("/fake/input")
    assert needs_transcode(probe) is transcode


def test_probe_media_ignores_attached_picture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.media_formats._run_ffprobe",
        lambda _path: ATTACHED_PIC_PROBE,
    )
    probe = probe_media("/fake/cover.mp3")
    assert probe.media_type == MediaType.audio
    assert probe.audio_codec == "mp3"
    assert probe.has_video is False
    assert validate_probe(probe) is None
    assert needs_transcode(probe) is False


def test_probe_media_failed_ffprobe() -> None:
    probe = probe_media("/dev/null/does-not-exist")
    assert probe.probe_ok is False
    assert validate_probe(probe) is not None


def test_validate_probe_when_ffprobe_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.media_formats._resolve_binary", lambda _name: None)
    probe = _probe(probe_ok=False)
    result = validate_probe(probe)
    assert result is not None
    assert "ffmpeg/ffprobe" in result


def test_resolve_binary_falls_back_to_homebrew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _resolve_binary.cache_clear()
    monkeypatch.setattr("app.services.media_formats.shutil.which", lambda _name: None)
    bin_dir = tmp_path / "brew" / "bin"
    bin_dir.mkdir(parents=True)
    ffprobe = bin_dir / "ffprobe"
    ffprobe.write_text("#!/bin/sh\necho ok\n")
    ffprobe.chmod(0o755)
    monkeypatch.setattr(
        "app.services.media_formats._COMMON_BIN_DIRS",
        (str(bin_dir),),
    )
    assert _resolve_binary("ffprobe") == str(ffprobe)
    _resolve_binary.cache_clear()


@pytest.mark.skipif(not ffprobe_available(), reason="ffprobe not installed")
def test_probe_real_mov_file() -> None:
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mov", delete=False) as tmp:
        path = tmp.name
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=320x240:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:duration=1",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-t",
                "1",
                path,
            ],
            capture_output=True,
            check=True,
        )
        probe = probe_media(path)
        assert probe.probe_ok is True
        assert probe.media_type == MediaType.video
        assert validate_probe(probe) is None
    finally:
        Path(path).unlink(missing_ok=True)


def test_media_tools_error_message() -> None:
    assert "ffmpeg" in media_tools_error().lower()


def test_normalize_storage_key() -> None:
    key, mime = normalize_storage_key("media/abc123.mov", MediaType.video)
    assert key.endswith(".mp4")
    assert mime == "video/mp4"

    key, mime = normalize_storage_key("media/abc123.mp3", MediaType.audio)
    assert key.endswith(".m4a")
    assert mime == "audio/mp4"
