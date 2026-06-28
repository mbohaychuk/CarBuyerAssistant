"""§4c asking→sold haircut.

A fixed-price listing's *asking* price overstates the price it will actually
clear at — private sellers typically settle a few percent below ask. The deal
score compares an acquisition cost against an expected *clearing* value, so a
listing's asking price is discounted by a per-seller-type haircut before it
enters ``all_in_cost``.

The percentages are a starting point. Calibrate later against disappeared-
listing data: the last-seen asking price of a vanished listing is a noisy
sold-price proxy (the system records ``first_seen_at`` / ``disappeared_at``).
"""
from __future__ import annotations

from decimal import Decimal

# ponytail: flat per-seller-type haircut; replace with a calibrated
# per-segment table once disappeared-listing data accumulates.
PRIVATE_HAIRCUT = Decimal("0.05")
DEALER_HAIRCUT = Decimal("0.03")


def asking_haircut_pct(seller_type: str | None) -> Decimal:
    """Fraction to shave off an asking price to estimate the clearing price."""
    if seller_type and seller_type.strip().lower() == "dealer":
        return DEALER_HAIRCUT
    return PRIVATE_HAIRCUT  # default to the private-seller haircut


def effective_acquisition_price(
    asking_price_cad: Decimal, seller_type: str | None,
) -> Decimal:
    """Asking price discounted by the seller-type haircut — the price a buyer
    is likely to actually pay, for scoring against the expected clearing value."""
    return asking_price_cad * (Decimal("1") - asking_haircut_pct(seller_type))


if __name__ == "__main__":  # pragma: no cover - runnable self-check
    assert effective_acquisition_price(Decimal("10000"), "private") == Decimal("9500.00")
    assert effective_acquisition_price(Decimal("10000"), "dealer") == Decimal("9700.00")
    assert effective_acquisition_price(Decimal("10000"), None) == Decimal("9500.00")
    print("ok")
