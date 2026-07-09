"""Service worker shell precache and offline range (seek) behavior."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SW_PATH = REPO_ROOT / "app" / "static" / "sw.js"

VIDEOJS_SHELL_PATHS = [
    "/static/js/player.js",
    "/static/vendor/videojs/video.min.js",
    "/static/vendor/videojs/video-js.min.css",
]


def _read_sw() -> str:
    return SW_PATH.read_text()


def test_sw_precaches_videojs_and_player():
    sw = _read_sw()
    assert "qas-shell-v7" in sw
    for url in VIDEOJS_SHELL_PATHS:
        assert url in sw, f"missing from SHELL_URLS: {url}"
        static_path = REPO_ROOT / "app" / url.lstrip("/")
        assert static_path.is_file(), f"precache target missing on disk: {static_path}"


@pytest.mark.asyncio
async def test_sw_shell_assets_served(client):
    for url in VIDEOJS_SHELL_PATHS:
        r = await client.get(url)
        assert r.status_code == 200
        assert r.content


@pytest.mark.asyncio
async def test_sw_js_served(client):
    r = await client.get("/sw.js")
    assert r.status_code == 200
    assert "qas-shell-v7" in r.text
    for url in VIDEOJS_SHELL_PATHS:
        assert url in r.text


def _serve_range(blob: bytes, content_type: str, range_header: str):
    """Mirror app/static/sw.js serveRangeFromCached for seek verification."""
    import re

    size = len(blob)
    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not m:
        return 200, blob, {}

    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return 416, b"", {}

    slice_ = blob[start : end + 1]
    return (
        206,
        slice_,
        {
            "Content-Type": content_type,
            "Content-Length": str(end - start + 1),
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
        },
    )


@pytest.mark.parametrize(
    "range_header,expected_status,expected_len,expected_range",
    [
        ("bytes=0-99", 206, 100, "bytes 0-99/1000"),
        ("bytes=500-799", 206, 300, "bytes 500-799/1000"),
        ("bytes=900-", 206, 100, "bytes 900-999/1000"),
        ("bytes=1000-", 416, 0, None),
    ],
)
def test_offline_seek_range_slices(range_header, expected_status, expected_len, expected_range):
    blob = b"\x00" * 1000
    status, body, headers = _serve_range(blob, "audio/mpeg", range_header)
    assert status == expected_status
    assert len(body) == expected_len
    if expected_range:
        assert headers["Content-Range"] == expected_range
        assert headers["Accept-Ranges"] == "bytes"
