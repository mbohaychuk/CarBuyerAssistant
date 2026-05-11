"""Tests for vision_batcher.photos — download, resize, and failure handling."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import respx
from httpx import Response
from PIL import Image

from carbuyer.apps.vision_batcher.photos import download_and_resize


def _png_bytes(w: int, h: int) -> bytes:
    """Return minimal PNG bytes for an *w* x *h* grey image."""
    img = Image.new("RGB", (w, h), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_download_and_resize_caps_count_and_size() -> None:
    urls = [f"https://x.test/p{i}.png" for i in range(12)]
    with respx.mock(base_url="https://x.test") as mock, tempfile.TemporaryDirectory() as td:
        mock.get(url__regex=r"/p\d+\.png").mock(
            return_value=Response(200, content=_png_bytes(2048, 2048)),
        )
        out = await download_and_resize(urls, max_dim=1024, max_count=5, tmp_dir=Path(td))
        assert len(out) == 5  # noqa: PLR2004
        for p in out:
            img = Image.open(p)
            assert max(img.size) <= 1024  # noqa: PLR2004


async def test_http_error_skips_url_and_continues() -> None:
    """A 404 on one URL must not abort the remaining downloads."""
    urls = [
        "https://x.test/ok.png",
        "https://x.test/missing.png",
        "https://x.test/ok2.png",
    ]
    with respx.mock(base_url="https://x.test") as mock, tempfile.TemporaryDirectory() as td:
        mock.get("/ok.png").mock(return_value=Response(200, content=_png_bytes(100, 100)))
        mock.get("/missing.png").mock(return_value=Response(404))
        mock.get("/ok2.png").mock(return_value=Response(200, content=_png_bytes(100, 100)))
        out = await download_and_resize(urls, tmp_dir=Path(td))
    assert len(out) == 2  # noqa: PLR2004


async def test_corrupt_image_bytes_skips_url_and_continues() -> None:
    """Bad image data on one URL must not abort the remaining downloads."""
    urls = [
        "https://x.test/ok.png",
        "https://x.test/bad.png",
    ]
    with respx.mock(base_url="https://x.test") as mock, tempfile.TemporaryDirectory() as td:
        mock.get("/ok.png").mock(return_value=Response(200, content=_png_bytes(100, 100)))
        mock.get("/bad.png").mock(return_value=Response(200, content=b"not a png"))
        out = await download_and_resize(urls, tmp_dir=Path(td))
    assert len(out) == 1


async def test_empty_url_list_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as td:
        # respx not needed — no HTTP calls expected
        out = await download_and_resize([], tmp_dir=Path(td))
    assert out == []
