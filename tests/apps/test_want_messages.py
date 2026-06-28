from __future__ import annotations

from decimal import Decimal
from typing import Any

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import LotEmbedData, render_want_match_text


def _data(**over: Any) -> LotEmbedData:
    base: dict[str, Any] = {
        "lot_id": 1,
        "url": "http://x/lot",
        "title": "t",
        "year": 2010,
        "make": "Nissan",
        "model": "Xterra",
        "trim": "PRO-4X",
        "location": "Calgary, AB",
        "current_high_bid_cad": Decimal("8000"),
        "all_in_cad": None,
        "expected_value_cad": Decimal("10000"),
        "value_low_cad": None,
        "value_high_cad": None,
        "price_deal_score": None,
        "rarity_score": None,
        "confidence_bucket": None,
        "condition_categorical": None,
        "top_red_flags": (),
        "top_green_flags": (),
        "suspicious_underprice": False,
        "scheduled_end_at": None,
    }
    base.update(over)
    return LotEmbedData(**base)


def test_want_match_routes_to_wants_channel() -> None:
    assert select_channel(trigger="want_match", score=None) == "wants"


def test_render_want_match_full() -> None:
    text = render_want_match_text(
        _data(),
        want_name="manual Xterra",
        pct_below_market=0.2,
        dollars_below_market_cad=Decimal("2000"),
        dollars_under_ceiling_cad=Decimal("7000"),
        comp_count=9,
    )
    assert "manual Xterra" in text
    assert "2010 Nissan Xterra" in text
    assert "Calgary, AB" in text
    assert "$8,000" in text
    assert "20%" in text
    assert "$2,000" in text
    assert "9 comps" in text
    assert "$7,000 under your budget" in text
    assert "http://x/lot" in text


def test_render_want_match_uncomped() -> None:
    text = render_want_match_text(
        _data(),
        want_name="w",
        pct_below_market=None,
        dollars_below_market_cad=None,
        dollars_under_ceiling_cad=None,
        comp_count=None,
    )
    assert "not enough comps" in text.lower()


def test_render_want_match_overpriced_reads_above_market() -> None:
    text = render_want_match_text(
        _data(),
        want_name="w",
        pct_below_market=-0.2,
        dollars_below_market_cad=Decimal("-2000"),
        dollars_under_ceiling_cad=None,
        comp_count=5,
    )
    assert "above market" in text
    assert "budget" not in text  # no ceiling provided


def test_render_want_match_without_ceiling_omits_budget_line() -> None:
    text = render_want_match_text(
        _data(),
        want_name="w",
        pct_below_market=0.2,
        dollars_below_market_cad=Decimal("2000"),
        dollars_under_ceiling_cad=None,
        comp_count=9,
    )
    assert "budget" not in text
