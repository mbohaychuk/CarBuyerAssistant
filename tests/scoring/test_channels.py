from decimal import Decimal

from carbuyer.scoring.channels import (
    CHANNEL_MULTIPLIERS,
    CONDITION_POSITION,
    condition_position,
    normalize_to_private,
)


def test_normalize_auction_estate_to_private() -> None:
    # Estate auctions clear below private-party retail; multiply up.
    assert normalize_to_private(Decimal("10000"), "auction_estate") == Decimal("12000")


def test_normalize_dealer_to_private() -> None:
    # Dealer asking prices include retail markup; back it off.
    assert normalize_to_private(Decimal("10000"), "dealer") == Decimal("9200")


def test_normalize_unknown_falls_back_to_identity() -> None:
    assert normalize_to_private(Decimal("10000"), "weird") == Decimal("10000")


def test_normalize_private_is_identity() -> None:
    assert normalize_to_private(Decimal("10000"), "private") == Decimal("10000")


def test_condition_position_returns_canonical_values() -> None:
    assert condition_position("bad") == 0.0
    assert condition_position("poor") == 0.25
    assert condition_position("decent") == 0.5
    assert condition_position("good") == 0.75
    assert condition_position("great") == 1.0


def test_condition_position_unknown_falls_back_to_midpoint() -> None:
    assert condition_position("garbage-string") == 0.5


def test_condition_position_with_sparse_flag_shifts_decent_toward_p25() -> None:
    # Phase 4 overlay #8: a coerced "decent" (because the enricher saw
    # condition_confidence < 0.5 and sparse listings historically run worse
    # than honestly-decent comps) should value below midpoint.
    assert condition_position("decent", sparse=True) == 0.35
    # Confident "decent" stays at midpoint.
    assert condition_position("decent", sparse=False) == 0.5


def test_condition_position_sparse_only_affects_decent() -> None:
    # The enricher only coerces TO "decent" — other categorical values come
    # with their own confidence and the sparse flag does not apply.
    assert condition_position("good", sparse=True) == 0.75
    assert condition_position("bad", sparse=True) == 0.0


def test_channel_multipliers_table_is_sane() -> None:
    # Sanity: estate >> private > dealer; salvage is the floor.
    assert CHANNEL_MULTIPLIERS["auction_estate"] > Decimal("1")
    assert CHANNEL_MULTIPLIERS["private"] == Decimal("1")
    assert CHANNEL_MULTIPLIERS["dealer"] < Decimal("1")
    assert CHANNEL_MULTIPLIERS["auction_salvage"] < CHANNEL_MULTIPLIERS["dealer"]


def test_condition_position_table_is_monotonic() -> None:
    order = ["bad", "poor", "decent", "good", "great"]
    values = [CONDITION_POSITION[c] for c in order]
    assert values == sorted(values)
