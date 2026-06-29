"""Channel router + message renderer tests.

Pure-Python; no Discord runtime, no DB. The renderers take a frozen
``LotEmbedData`` snapshot (built by the notifier) and emit the plaintext
fallback used when an embed cannot render.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import (
    LotEmbedData,
    render_closing_soon_text,
    render_lot_extended_text,
)


def test_select_channel_routes() -> None:
    assert select_channel(trigger="want_match", score=None) == "wants"
    assert select_channel(trigger="closing_soon", score=None) == "auction_closing"
    assert select_channel(trigger="lot_extended", score=None) == "auction_closing"
    assert select_channel(trigger="vision_update", score=None) == "vision_updates"
    assert select_channel(trigger="system", score=None) == "system_health"
    assert select_channel(trigger="needs_plugin", score=None) == "needs_plugin"


def test_select_channel_unknown_trigger_falls_back() -> None:
    # Unknown triggers default to the general auction-watch channel; the score
    # argument is ignored now that the score-gated going_cheap route is gone.
    assert select_channel(trigger="mystery", score=0.9) == "auction_watch"


def _lot_data(**overrides: object) -> LotEmbedData:
    base: dict[str, object] = dict(
        lot_id=1, url="u", title="t",
        year=1985, make="Toyota", model="Land Cruiser", trim="FJ60",
        location="Edmonton, AB",
        current_high_bid_cad=Decimal("5500"),
        all_in_cad=None,
        value_low_cad=Decimal("18000"), value_high_cad=Decimal("28000"),
        scheduled_end_at=datetime(2026, 6, 1),
    )
    base.update(overrides)
    return LotEmbedData(**base)  # type: ignore[arg-type]


# ─── Phase 13 H6: closing_soon + lot_extended renderers ─────────────────────


def test_render_closing_soon_includes_vehicle_and_url() -> None:
    d = _lot_data(
        url="https://hibid.com/lot/777",
        current_high_bid_cad=Decimal("9000"),
        all_in_cad=Decimal("11500"),
    )
    text = render_closing_soon_text(d)
    assert "Closes in 1h" in text
    assert "Land Cruiser" in text
    assert "$9,000" in text
    assert "$11,500" in text
    assert text.endswith("https://hibid.com/lot/777")


def test_render_closing_soon_handles_no_bid_yet() -> None:
    d = _lot_data(
        current_high_bid_cad=None,
        all_in_cad=None,
        value_low_cad=None,
        value_high_cad=None,
    )
    text = render_closing_soon_text(d)
    assert "no bid yet" in text
    assert "uncomped" in text
    assert "None" not in text


def test_render_lot_extended_includes_new_end_time() -> None:
    d = _lot_data(
        url="https://hibid.com/lot/777",
        current_high_bid_cad=Decimal("12500"),
        scheduled_end_at=datetime(2026, 6, 1, 18, 5),
    )
    text = render_lot_extended_text(d)
    assert "Soft-close" in text
    assert "Jun 01 18:05" in text  # new end-time present
    assert "$12,500" in text
    assert text.endswith("https://hibid.com/lot/777")


def test_render_lot_extended_handles_no_bid() -> None:
    d = _lot_data(
        current_high_bid_cad=None,
        scheduled_end_at=None,
    )
    text = render_lot_extended_text(d)
    assert "no bid" in text
    assert "None" not in text
