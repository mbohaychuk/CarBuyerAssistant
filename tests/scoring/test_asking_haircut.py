from decimal import Decimal

from carbuyer.scoring.asking_haircut import (
    asking_haircut_pct,
    effective_acquisition_price,
)


def test_private_seller_gets_the_private_haircut() -> None:
    assert effective_acquisition_price(Decimal("10000"), "private") == Decimal("9500.00")


def test_dealer_gets_the_smaller_haircut() -> None:
    assert effective_acquisition_price(Decimal("10000"), "Dealer") == Decimal("9700.00")


def test_unknown_seller_defaults_to_private() -> None:
    assert asking_haircut_pct(None) == Decimal("0.05")
    assert effective_acquisition_price(Decimal("10000"), None) == Decimal("9500.00")
