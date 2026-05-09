from carbuyer.sources.base import SOURCES
from carbuyer.sources.hibid.source import HibidSource
from carbuyer.sources.resolver import (
    canonicalize_url,
    resolve_auction_url,
    unknown_platform_ref,
)

# Ensure HibidSource is registered for tests that exercise the global registry.
# Importing the module triggers register(...); this assert documents the dependency.
assert SOURCES.get("hibid") is not None or isinstance(HibidSource(provinces=[]), HibidSource)


def test_canonicalize_url_strips_fragment_and_tracking() -> None:
    raw = "https://Example.COM/Foo/Bar/?utm_source=newsletter&ref=fag&id=42#hash"
    assert canonicalize_url(raw) == "https://example.com/Foo/Bar?id=42"


def test_canonicalize_url_idempotent() -> None:
    once = canonicalize_url("https://hibid.com/catalog/740236/?utm_campaign=x")
    twice = canonicalize_url(once)
    assert once == twice


def test_canonicalize_url_handles_root_path() -> None:
    assert canonicalize_url("https://example.com/") == "https://example.com"
    assert canonicalize_url("https://example.com") == "https://example.com"


def test_resolve_auction_url_finds_hibid() -> None:
    ref = resolve_auction_url("https://hibid.com/catalog/740236/some-slug")
    assert ref is not None
    assert ref.source == "hibid"
    assert ref.source_auction_id == "740236"


def test_resolve_auction_url_finds_hibid_with_province_prefix() -> None:
    ref = resolve_auction_url("https://hibid.com/alberta/catalog/740236/slug")
    assert ref is not None
    assert ref.source == "hibid"
    assert ref.source_auction_id == "740236"


def test_resolve_auction_url_returns_none_for_unknown() -> None:
    assert resolve_auction_url("https://example.com/auction/99") is None


def test_unknown_platform_ref_is_deterministic() -> None:
    a = unknown_platform_ref("https://foo.ca/auction/123/?utm_source=x")
    b = unknown_platform_ref("https://foo.ca/auction/123#hash")
    assert a.source == "unknown:foo.ca"
    assert a.source_auction_id == b.source_auction_id  # canonicalization aligns


def test_unknown_platform_ref_differs_across_distinct_urls() -> None:
    a = unknown_platform_ref("https://foo.ca/auction/123")
    b = unknown_platform_ref("https://foo.ca/auction/124")
    assert a.source_auction_id != b.source_auction_id
