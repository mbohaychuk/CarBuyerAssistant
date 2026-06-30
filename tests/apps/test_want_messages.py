from __future__ import annotations

from decimal import Decimal
from typing import Any

from carbuyer.apps.bot.channels import select_channel
from carbuyer.apps.bot.messages import (
    DigestRow,
    LotEmbedData,
    render_digest_text,
    render_want_match_text,
)


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
        "value_low_cad": None,
        "value_high_cad": None,
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


def test_render_want_match_shows_price_drop() -> None:
    text = render_want_match_text(
        _data(previous_asking_cad=Decimal("10000"), current_high_bid_cad=Decimal("8000")),
        want_name="GX470",
        pct_below_market=0.2,
        dollars_below_market_cad=Decimal("2000"),
        dollars_under_ceiling_cad=None,
        comp_count=9,
    )
    assert "Price drop" in text
    assert "$10,000" in text  # was
    assert "$8,000" in text   # now


def test_render_want_match_no_drop_line_without_previous() -> None:
    text = render_want_match_text(
        _data(),  # previous_asking_cad defaults to None
        want_name="w",
        pct_below_market=0.2,
        dollars_below_market_cad=Decimal("2000"),
        dollars_under_ceiling_cad=None,
        comp_count=9,
    )
    assert "Price drop" not in text


def test_render_want_match_no_drop_line_on_increase() -> None:
    # previous < current (price went up) → no drop line.
    text = render_want_match_text(
        _data(previous_asking_cad=Decimal("8000"), current_high_bid_cad=Decimal("9000")),
        want_name="w",
        pct_below_market=None,
        dollars_below_market_cad=None,
        dollars_under_ceiling_cad=None,
        comp_count=None,
    )
    assert "Price drop" not in text


def test_render_want_match_shows_reliability() -> None:
    text = render_want_match_text(
        _data(recall_count=2, complaint_count=47),
        want_name="w",
        pct_below_market=0.2,
        dollars_below_market_cad=Decimal("2000"),
        dollars_under_ceiling_cad=None,
        comp_count=9,
    )
    assert "NHTSA" in text
    assert "2 recalls" in text
    assert "47 complaints" in text


def test_render_want_match_no_reliability_line_when_absent() -> None:
    text = render_want_match_text(
        _data(),  # recall_count/complaint_count default None
        want_name="w",
        pct_below_market=0.2,
        dollars_below_market_cad=Decimal("2000"),
        dollars_under_ceiling_cad=None,
        comp_count=9,
    )
    assert "NHTSA" not in text


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


def test_render_digest_groups_by_want() -> None:
    groups = [
        ("4runner platform", [
            DigestRow(title="2005 Lexus GX 470", price_cad=Decimal("12000"),
                      pct_below_market=0.08, url="http://x/1"),
        ]),
        ("manual xterra", [
            DigestRow(title="2012 Nissan Xterra", price_cad=Decimal("9900"),
                      pct_below_market=0.01, url="http://x/2"),
            DigestRow(title="2010 Nissan Xterra", price_cad=None,
                      pct_below_market=None, url="http://x/3"),
        ]),
    ]
    text = render_digest_text(groups)
    assert "4runner platform" in text
    assert "manual xterra" in text
    assert "GX 470" in text
    assert "http://x/2" in text
    assert "8%" in text
    assert "None" not in text


def test_render_digest_empty_groups_is_empty_string() -> None:
    assert render_digest_text([]) == ""


def test_render_digest_flips_sign_for_above_market() -> None:
    text = render_digest_text([
        ("w", [DigestRow(title="2012 Nissan Xterra", price_cad=Decimal("11000"),
                         pct_below_market=-0.05, url="http://x/1")]),
    ])
    assert "5% above market" in text
    assert "below market" not in text
