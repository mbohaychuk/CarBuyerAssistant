from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from carbuyer.db.models import AuctionLot, PrivateListing
from carbuyer.wants.leverage import buyer_leverage_line, effective_days_on_market

NOW = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


def test_effective_dom_prefers_source() -> None:
    assert effective_days_on_market(30, NOW - timedelta(days=5), NOW) == 30  # noqa: PLR2004


def test_effective_dom_falls_back_to_first_seen() -> None:
    assert effective_days_on_market(None, NOW - timedelta(days=12), NOW) == 12  # noqa: PLR2004


def test_effective_dom_none_when_no_data() -> None:
    assert effective_days_on_market(None, None, NOW) is None


def _listing(**over: object) -> PrivateListing:
    base: dict[str, object] = dict(
        source="kijiji", source_listing_id="K1", url="http://k/1",
        make="Nissan", model="Xterra", year=2010,
        asking_price_cad=Decimal("15000"),
        original_asking_price_cad=Decimal("18000"),
        price_drop_count=2, days_on_market=90,
        first_seen_at=NOW - timedelta(days=90), listing_status="active",
    )
    base.update(over)
    return PrivateListing(**base)  # type: ignore[arg-type]


def test_leverage_full_line() -> None:
    line = buyer_leverage_line(_listing(), NOW)
    assert line == "listed 90 days · down $3,000 (17%) from $18,000 · 2 drops"


def test_leverage_one_drop_singular() -> None:
    line = buyer_leverage_line(_listing(price_drop_count=1), NOW)
    assert line is not None and "1 drop" in line and "1 drops" not in line


def test_leverage_no_drop_shows_only_days() -> None:
    line = buyer_leverage_line(
        _listing(original_asking_price_cad=None, price_drop_count=0), NOW,
    )
    assert line == "listed 90 days"


def test_leverage_none_for_auction() -> None:
    lot = AuctionLot(source_lot_id="L1", url="x", make="Nissan", model="Xterra")
    assert buyer_leverage_line(lot, NOW) is None


def test_leverage_none_when_no_data() -> None:
    line = buyer_leverage_line(
        _listing(days_on_market=None, first_seen_at=None,
                 original_asking_price_cad=None, price_drop_count=0), NOW,
    )
    assert line is None
