"""Phase 5 Task 31 — channel router + message renderer tests.

Pure-Python; no Discord runtime, no DB. The renderers take a frozen
``LotEmbedData`` snapshot (built by the notifier in Phase 6) and emit
the plaintext fallback used when an embed cannot render.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import (
    LotEmbedData,
    render_early_warning_text,
    render_going_cheap_text,
)


def test_select_channel_routes() -> None:
    assert select_channel(trigger="early_warning", score=None) == "early_warning"
    assert select_channel(trigger="going_cheap", score=0.25) == "hot_deals"
    assert select_channel(trigger="going_cheap", score=0.16) == "watchlist"
    assert select_channel(trigger="closing_soon", score=None) == "auction_closing"


def test_select_channel_going_cheap_at_threshold() -> None:
    # 0.20 boundary belongs to hot_deals (>= 0.20).
    assert select_channel(trigger="going_cheap", score=0.20) == "hot_deals"
    assert select_channel(trigger="going_cheap", score=0.1999) == "watchlist"
    # No score on a going_cheap trigger falls back to watchlist (cautious).
    assert select_channel(trigger="going_cheap", score=None) == "watchlist"


def test_select_channel_other_triggers() -> None:
    assert select_channel(trigger="bid_trajectory", score=None) == "auction_closing"
    assert select_channel(trigger="lot_extended", score=None) == "auction_closing"
    assert select_channel(trigger="vision_update", score=None) == "vision_updates"
    assert select_channel(trigger="system", score=None) == "system_health"
    # Unknown triggers default to watchlist.
    assert select_channel(trigger="mystery", score=None) == "watchlist"


def _early_warning_data(**overrides: object) -> LotEmbedData:
    base: dict[str, object] = dict(
        lot_id=1, url="u", title="t",
        year=1985, make="Toyota", model="Land Cruiser", trim="FJ60",
        location="Edmonton, AB",
        current_high_bid_cad=Decimal("5500"),
        all_in_cad=None, expected_value_cad=Decimal("23000"),
        value_low_cad=Decimal("18000"), value_high_cad=Decimal("28000"),
        price_deal_score=None, rarity_score=4.0,
        confidence_bucket="high", condition_categorical="good",
        top_red_flags=(), top_green_flags=("classic Land Cruiser", "Western Canada origin"),
        suspicious_underprice=False,
        scheduled_end_at=datetime(2026, 6, 1),
    )
    base.update(overrides)
    return LotEmbedData(**base)  # type: ignore[arg-type]


def test_render_early_warning() -> None:
    text = render_early_warning_text(_early_warning_data())
    assert "RARE FIND" in text
    assert "Land Cruiser" in text
    assert "u" in text  # url appears in output (d.url from fixture)


def test_render_early_warning_url_appended() -> None:
    text = render_early_warning_text(_early_warning_data(url="https://example.com/lot/42"))
    assert text.endswith("https://example.com/lot/42")


def test_render_early_warning_no_bid_no_comps() -> None:
    text = render_early_warning_text(_early_warning_data(
        current_high_bid_cad=None,
        value_low_cad=None,
        value_high_cad=None,
        top_green_flags=(),
        scheduled_end_at=None,
    ))
    assert "(no bid yet)" in text
    assert "(uncomped)" in text
    # Falls back to a generic descriptor when no green flags present.
    assert "rare/desirable" in text
    assert "Closes ?" in text


def test_render_going_cheap_includes_margin() -> None:
    d = LotEmbedData(
        lot_id=2, url="https://example.com/lot/2", title="t",
        year=2018, make="Toyota", model="Tacoma", trim="TRD Off-Road",
        location="Saskatoon, SK",
        current_high_bid_cad=Decimal("14500"),
        all_in_cad=Decimal("17400"), expected_value_cad=Decimal("24000"),
        value_low_cad=Decimal("20000"), value_high_cad=Decimal("28000"),
        price_deal_score=0.27, rarity_score=1.0,
        confidence_bucket="high", condition_categorical="good",
        top_red_flags=(), top_green_flags=("recent timing chain",),
        suspicious_underprice=False,
        scheduled_end_at=datetime(2026, 6, 1),
    )
    text = render_going_cheap_text(d)
    assert "Going cheap" in text
    assert "$6,600" in text  # margin
    assert "https://example.com/lot/2" in text  # url appended


def test_render_going_cheap_suspicious_underprice() -> None:
    d = LotEmbedData(
        lot_id=3, url="u", title="t",
        year=2018, make="Toyota", model="Tacoma", trim=None,
        location="Calgary, AB",
        current_high_bid_cad=Decimal("8000"),
        all_in_cad=Decimal("9600"), expected_value_cad=Decimal("24000"),
        value_low_cad=Decimal("20000"), value_high_cad=Decimal("28000"),
        price_deal_score=0.60, rarity_score=1.0,
        confidence_bucket="medium", condition_categorical="good",
        top_red_flags=(), top_green_flags=(),
        suspicious_underprice=True,
        scheduled_end_at=datetime(2026, 6, 1),
    )
    text = render_going_cheap_text(d)
    assert "PRICED BELOW TYPICAL LOW END" in text
    # No green flags → no checkmark line at the end (rstrip removes blank tail).
    assert not text.endswith("\n")


def test_render_going_cheap_handles_missing_pricing() -> None:
    d = LotEmbedData(
        lot_id=4, url="u", title="t",
        year=None, make=None, model=None, trim=None,
        location="Unknown",
        current_high_bid_cad=None,
        all_in_cad=None, expected_value_cad=None,
        value_low_cad=None, value_high_cad=None,
        price_deal_score=None, rarity_score=None,
        confidence_bucket=None, condition_categorical=None,
        top_red_flags=(), top_green_flags=(),
        suspicious_underprice=False,
        scheduled_end_at=None,
    )
    text = render_going_cheap_text(d)
    assert "no bid" in text
    # Margin line should not appear when pricing is missing.
    assert "Margin" not in text
    # None-valued optional fields must render as "?" rather than the literal "None".
    assert "None" not in text
