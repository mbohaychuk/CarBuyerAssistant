from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from carbuyer.wants.delivery import delivery_tier

NOW = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)


def _tier(**over: object) -> str:
    base: dict[str, object] = dict(
        want_relative_score=0.0, offer_price_cad=Decimal("8000"),
        previous_asking_price_cad=None, scheduled_end_at=None,
        now=NOW, deal_threshold=0.15, closing_hours=24,
    )
    base.update(over)
    return delivery_tier(**base)  # type: ignore[arg-type]


def test_great_deal_at_or_above_threshold_is_instant() -> None:
    assert _tier(want_relative_score=0.15) == "instant"
    assert _tier(want_relative_score=0.30) == "instant"


def test_ordinary_below_threshold_is_digest() -> None:
    assert _tier(want_relative_score=0.14) == "digest"
    assert _tier(want_relative_score=0.0) == "digest"


def test_uncomped_none_score_is_instant() -> None:
    # WG4: a wanted vehicle we can't price still surfaces (instantly, not digest).
    assert _tier(want_relative_score=None) == "instant"


def test_price_drop_is_instant() -> None:
    assert _tier(
        want_relative_score=0.0,
        previous_asking_price_cad=Decimal("9000"),
        offer_price_cad=Decimal("8000"),
    ) == "instant"


def test_price_increase_is_not_a_drop() -> None:
    assert _tier(
        previous_asking_price_cad=Decimal("8000"),
        offer_price_cad=Decimal("9000"),
    ) == "digest"


def test_closing_within_window_is_instant() -> None:
    assert _tier(scheduled_end_at=NOW + timedelta(hours=12)) == "instant"
    assert _tier(scheduled_end_at=NOW + timedelta(hours=24)) == "instant"


def test_closing_beyond_window_or_past_is_digest() -> None:
    assert _tier(scheduled_end_at=NOW + timedelta(hours=25)) == "digest"
    assert _tier(scheduled_end_at=NOW - timedelta(hours=1)) == "digest"
