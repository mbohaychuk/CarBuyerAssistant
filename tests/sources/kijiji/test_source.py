"""Kijiji source scaffold: registration + criteria→URL mapping. The HTML parse
is unimplemented and must NOT touch the live site — search_listings raises
before any I/O."""
from __future__ import annotations

import pytest

from carbuyer.sources.base import SOURCES
from carbuyer.sources.kijiji import KijijiSource
from carbuyer.wants.criteria import WantCriteria


def test_kijiji_is_registered_as_a_listing_source() -> None:
    src = SOURCES.get("kijiji")
    assert src is not None
    assert src.kind == "listing"


def test_search_url_includes_make_and_model() -> None:
    url = KijijiSource()._search_url(  # pyright: ignore[reportPrivateUsage]
        WantCriteria(makes=["Nissan"], models=["Xterra"]),
    )
    assert "nissan" in url.lower()
    assert "xterra" in url.lower()


async def test_search_listings_raises_without_touching_the_network() -> None:
    src = KijijiSource()
    # No transport is injected and none is opened — the scaffold must not hit the
    # live site before its selectors exist.
    with pytest.raises(NotImplementedError, match="sample pages"):
        _ = [raw async for raw in src.search_listings(WantCriteria(makes=["Nissan"]))]
