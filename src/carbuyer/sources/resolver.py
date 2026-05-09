from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from carbuyer.sources.base import SOURCES, AuctionRef

# Tracking params we strip universally. Platform-specific overrides come via
# Source.canonicalize_url when a real platform needs different handling.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "ref", "fbclid", "gclid", "mc_eid", "mc_cid", "_ga",
})


def canonicalize_url(url: str) -> str:
    """Strip tracking params and fragment; lowercase host. Idempotent.

    The result is suitable for use as a lookup key and for storage in
    `auctions.canonical_url`. Two URLs that point to the same auction with
    different tracking-param noise canonicalize to the same string.
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(pairs)
    path = parsed.path
    # Strip a single trailing slash unless the path is just "/"
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    if path == "/":
        path = ""
    return urlunparse((parsed.scheme, netloc, path, parsed.params, query, ""))


def resolve_auction_url(url: str) -> AuctionRef | None:
    """Walk SOURCES (sorted by name for determinism); return the first plugin
    that recognizes the URL, or None.
    """
    for name in sorted(SOURCES.keys()):
        ref = SOURCES[name].parse_auction_url(url)
        if ref is not None:
            return ref
    return None


def unknown_platform_ref(url: str) -> AuctionRef:
    """Build an AuctionRef for a URL no plugin recognizes.

    Source name is ``unknown:{host}``; the auction id is a stable sha1 hash
    prefix of the canonicalized URL, so multiple routers all converge on the
    same identity for the same external auction.
    """
    canonical = canonicalize_url(url)
    host = urlparse(canonical).hostname or "unknown"
    digest = hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return AuctionRef(source=f"unknown:{host}", source_auction_id=digest, url=canonical)
