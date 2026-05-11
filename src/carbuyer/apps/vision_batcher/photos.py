"""Download auction-lot photos and resize them to a bounded resolution.

Callers own the tempdir lifecycle: create a ``tempfile.TemporaryDirectory``
context per lot, pass its path as ``tmp_dir``, and clean-up is automatic on
context-manager exit.  This keeps disk usage bounded across long nightly runs.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

from carbuyer.shared.logging import get_logger
from carbuyer.sources.http import make_client

log = get_logger("vision_photos")

_DEFAULT_MAX_DIM = 1024
_DEFAULT_MAX_COUNT = 8


async def download_and_resize(
    urls: list[str],
    *,
    tmp_dir: Path,
    max_dim: int = _DEFAULT_MAX_DIM,
    max_count: int = _DEFAULT_MAX_COUNT,
) -> list[Path]:
    """Download up to *max_count* photos, resize to fit *max_dim* on the long edge.

    Files are written into *tmp_dir*, which the caller is responsible for
    cleaning up.  URLs that fail to download or cannot be decoded by PIL are
    skipped with a warning; they do not abort the rest of the batch.
    """
    out: list[Path] = []
    async with make_client() as client:
        for url in urls[:max_count]:
            try:
                r = await client.get(url)
                r.raise_for_status()
            except Exception:
                log.warning("photo download failed", url=url)
                continue

            # Stable filename derived from the URL so re-runs within the same
            # tempdir are idempotent (matters for retry logic in Task 38).
            digest = hashlib.sha256(url.encode()).hexdigest()[:16]
            jpg_path = tmp_dir / f"{digest}.jpg"

            try:
                img = Image.open(io.BytesIO(r.content))
                img.thumbnail((max_dim, max_dim))
                img.convert("RGB").save(jpg_path, format="JPEG", quality=85)
                out.append(jpg_path)
            except Exception:
                log.warning("photo resize failed", url=url)

    return out
