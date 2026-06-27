from __future__ import annotations

from decimal import Decimal
from typing import Any

from carbuyer.db.models import AuctionLot
from carbuyer.wants.criteria import WantCriteria
from carbuyer.wants.deal import score_want_deal


def _lot(**over: Any) -> AuctionLot:
    base: dict[str, Any] = {
        "expected_value_cad": Decimal("10000"),
        "value_mid_cad": Decimal("10000"),
        "comp_count": 9,
    }
    base.update(over)
    return AuctionLot(**base)


def test_score_is_fraction_below_reference() -> None:
    deal = score_want_deal(_lot(), WantCriteria(), offer_price_cad=Decimal("8000"))
    assert deal.score == 0.2  # noqa: PLR2004 -- (10000 - 8000) / 10000
    assert deal.reference_value_cad == Decimal("10000")
    assert deal.dollars_below_market_cad == Decimal("2000")
    assert deal.comp_count == 9  # noqa: PLR2004 -- explicit fixture value


def test_negative_score_when_overpriced() -> None:
    deal = score_want_deal(_lot(), WantCriteria(), offer_price_cad=Decimal("12000"))
    assert deal.score == -0.2  # noqa: PLR2004 -- explicit fixture value
    assert deal.dollars_below_market_cad == Decimal("-2000")


def test_falls_back_to_value_mid_when_expected_value_missing() -> None:
    lot = _lot(expected_value_cad=None, value_mid_cad=Decimal("10000"))
    deal = score_want_deal(lot, WantCriteria(), offer_price_cad=Decimal("9000"))
    assert deal.reference_value_cad == Decimal("10000")
    assert deal.score == 0.1  # noqa: PLR2004 -- explicit fixture value


def test_score_none_when_no_reference_value() -> None:
    lot = _lot(expected_value_cad=None, value_mid_cad=None)
    deal = score_want_deal(lot, WantCriteria(), offer_price_cad=Decimal("9000"))
    assert deal.score is None
    assert deal.dollars_below_market_cad is None
    assert deal.comp_count == 9  # noqa: PLR2004 -- explicit fixture value


def test_score_none_when_price_unknown() -> None:
    deal = score_want_deal(_lot(), WantCriteria(), offer_price_cad=None)
    assert deal.score is None
    assert deal.dollars_below_market_cad is None


def test_zero_reference_does_not_divide_by_zero() -> None:
    lot = _lot(expected_value_cad=Decimal("0"), value_mid_cad=None)
    deal = score_want_deal(lot, WantCriteria(), offer_price_cad=Decimal("9000"))
    assert deal.score is None


def test_dollars_under_ceiling_uses_want_ceiling() -> None:
    want = WantCriteria(price_ceiling_cad=15000)
    deal = score_want_deal(_lot(), want, offer_price_cad=Decimal("8000"))
    assert deal.dollars_under_ceiling_cad == Decimal("7000")  # 15000 - 8000


def test_dollars_under_ceiling_none_without_ceiling_or_price() -> None:
    assert score_want_deal(
        _lot(), WantCriteria(), offer_price_cad=Decimal("8000")
    ).dollars_under_ceiling_cad is None
    assert score_want_deal(
        _lot(), WantCriteria(price_ceiling_cad=15000), offer_price_cad=None
    ).dollars_under_ceiling_cad is None
