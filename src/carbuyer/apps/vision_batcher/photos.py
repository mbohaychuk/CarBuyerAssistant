"""Download auction-lot photos and resize them to a bounded resolution.

Callers own the tempdir lifecycle: create a ``tempfile.TemporaryDirectory``
context per lot, pass its path as ``tmp_dir``, and clean-up is automatic on
context-manager exit.  This keeps disk usage bounded across long nightly runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

from PIL import Image

from carbuyer.shared.logging import get_logger
from carbuyer.sources.http import make_client

log = get_logger("vision_photos")

_DEFAULT_MAX_DIM = 1024
_DEFAULT_MAX_COUNT = 8

# Pillow's default decompression-bomb threshold (~178MP) only raises an error
# at ~358MP and merely warns below that — a ~178MP image still allocates 1-2GB
# of RAM during decode. Auction sites are arbitrary input, so cap aggressively
# at 50MP (well above any legitimate phone camera shot, well below the OOM
# regime). The DecompressionBombError raised when an image exceeds this is
# caught by the existing per-URL except below.
Image.MAX_IMAGE_PIXELS = 50_000_000


async def download_and_resize(
    urls: list[str],
    *,
    tmp_dir: Path,
    max_dim: int = _DEFAULT_MAX_DIM,
    max_count: int = _DEFAULT_MAX_COUNT,
    lot_id: int | None = None,
) -> list[Path]:
    """Download up to *max_count* photos, resize to fit *max_dim* on the long edge.

    Files are written into *tmp_dir*, which the caller is responsible for
    cleaning up.  URLs that fail to download or cannot be decoded by PIL are
    skipped with a warning; they do not abort the rest of the batch.

    *lot_id* is forwarded into per-URL warning logs so a "this lot fails every
    night" pattern is greppable without a DB join. Optional because the helper
    is also used in standalone evaluations not tied to a DB row.
    """
    out: list[Path] = []
    async with make_client() as client:
        for url in urls[:max_count]:
            try:
                r = await client.get(url)
                r.raise_for_status()
            except Exception:
                log.warning("photo download failed", url=url, lot_id=lot_id)
                continue

            # Stable filename derived from the URL so re-runs within the same
            # tempdir are idempotent (matters for retry logic in Task 38).
            digest = hashlib.sha256(url.encode()).hexdigest()[:16]
            jpg_path = tmp_dir / f"{digest}.jpg"

            try:
                # Pillow decode/resize/encode is CPU-bound and blocks the
                # event loop. A 4-8MP source × downscale to 1024px on the long
                # edge + JPEG re-encode is tens-to-hundreds of ms each, and
                # MAX_IMAGE_PIXELS=50M means a near-cap image can pin the loop
                # for seconds. asyncio.to_thread offloads to a worker thread.
                await asyncio.to_thread(
                    _decode_resize_save, r.content, jpg_path, max_dim,
                )
                out.append(jpg_path)
            except Exception:
                log.warning("photo resize failed", url=url, lot_id=lot_id)

    return out


def _decode_resize_save(content: bytes, jpg_path: Path, max_dim: int) -> None:
    """Pure sync helper — runs in a worker thread via asyncio.to_thread.

    Kept as a module-level function (not a closure) so the thread doesn't
    capture the caller's locals; cheaper to ship to the executor.
    """
    img = Image.open(io.BytesIO(content))
    img.thumbnail((max_dim, max_dim))
    img.convert("RGB").save(jpg_path, format="JPEG", quality=85)
