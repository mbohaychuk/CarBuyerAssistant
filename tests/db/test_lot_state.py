"""Unit tests for carbuyer.db.lot_state.apply_user_action.

Covers the transition truth table from the four-state spec. In-memory
AuctionLot instances are mutated; a fake session captures appended
LotActionHistory rows via session.add().
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from carbuyer.db.enums import UserAction
from carbuyer.db.lot_state import apply_user_action
from carbuyer.db.models import AuctionLot, LotActionHistory

FROZEN = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
EARLIER = datetime(2026, 5, 18, 9, 0, 0, tzinfo=UTC)


class FakeSession:
    """Stand-in for AsyncSession that records session.add() calls."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def _lot(**overrides: Any) -> AuctionLot:
    # AuctionLot has no `source` column — source lives on Auction.
    lot = AuctionLot(
        id=1,
        auction_id=1,
        source_lot_id="L1",
        url="https://example.com",
    )
    for k, v in overrides.items():
        setattr(lot, k, v)
    return lot


@pytest.mark.parametrize(
    "starting,target,starting_extras,expected",
    [
        # any → INTERESTED clears bound fields
        (None, UserAction.INTERESTED, {}, {
            "user_action": UserAction.INTERESTED,
            "max_bid_cad": None, "bid_placed_at": None, "won_at": None,
        }),
        # INTERESTED → PASSED
        (UserAction.INTERESTED, UserAction.PASSED, {}, {
            "user_action": UserAction.PASSED,
            "max_bid_cad": None, "bid_placed_at": None, "won_at": None,
        }),
        # any → BID_PLACED (with amt) stamps timestamp on first entry
        (UserAction.INTERESTED, UserAction.BID_PLACED, {}, {
            "user_action": UserAction.BID_PLACED,
            "max_bid_cad": Decimal("500"),
            "bid_placed_at": FROZEN,
            "won_at": None,
        }),
        # any → PURCHASED stamps won_at, clears bid fields
        (UserAction.BID_PLACED, UserAction.PURCHASED, {
            "max_bid_cad": Decimal("500"),
            "bid_placed_at": EARLIER,
        }, {
            "user_action": UserAction.PURCHASED,
            "max_bid_cad": None,
            "bid_placed_at": None,
            "won_at": FROZEN,
        }),
        # toggle-off clears everything
        (UserAction.BID_PLACED, None, {
            "max_bid_cad": Decimal("500"),
            "bid_placed_at": EARLIER,
        }, {
            "user_action": None,
            "max_bid_cad": None,
            "bid_placed_at": None,
            "won_at": None,
        }),
    ],
)
def test_transitions(
    starting: UserAction | None,
    target: UserAction | None,
    starting_extras: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    lot = _lot(user_action=starting, **starting_extras)
    session = FakeSession()

    kwargs: dict[str, Any] = {"source": "test", "now": FROZEN}
    if target == UserAction.BID_PLACED:
        kwargs["max_bid_cad"] = Decimal("500")

    apply_user_action(session, lot, target, **kwargs)

    for attr, want in expected.items():
        assert getattr(lot, attr) == want, f"{attr!r}: got {getattr(lot, attr)!r}, want {want!r}"


def test_bid_placed_requires_max_bid() -> None:
    lot = _lot(user_action=UserAction.INTERESTED)
    session = FakeSession()
    with pytest.raises(ValueError, match="max_bid_cad"):
        apply_user_action(
            session, lot, UserAction.BID_PLACED, source="test", now=FROZEN,
        )


def test_bid_placed_reconfirm_preserves_timestamp_overwrites_amount() -> None:
    lot = _lot(
        user_action=UserAction.BID_PLACED,
        max_bid_cad=Decimal("500"),
        bid_placed_at=EARLIER,
    )
    session = FakeSession()
    apply_user_action(
        session, lot, UserAction.BID_PLACED,
        max_bid_cad=Decimal("600"), source="test", now=FROZEN,
    )
    assert lot.max_bid_cad == Decimal("600")
    assert lot.bid_placed_at == EARLIER  # preserved


def test_purchased_reconfirm_preserves_won_at() -> None:
    lot = _lot(user_action=UserAction.PURCHASED, won_at=EARLIER)
    session = FakeSession()
    apply_user_action(
        session, lot, UserAction.PURCHASED, source="test", now=FROZEN,
    )
    assert lot.won_at == EARLIER  # preserved


def test_allow_downgrade_false_blocks_purchased_to_interested() -> None:
    lot = _lot(user_action=UserAction.PURCHASED, won_at=EARLIER)
    session = FakeSession()
    with pytest.raises(ValueError, match="downgrade"):
        apply_user_action(
            session, lot, UserAction.INTERESTED,
            source="discord_bot", now=FROZEN, allow_downgrade=False,
        )


def test_allow_downgrade_false_blocks_bid_placed_to_passed() -> None:
    lot = _lot(
        user_action=UserAction.BID_PLACED,
        max_bid_cad=Decimal("500"),
        bid_placed_at=EARLIER,
    )
    session = FakeSession()
    with pytest.raises(ValueError, match="downgrade"):
        apply_user_action(
            session, lot, UserAction.PASSED,
            source="discord_bot", now=FROZEN, allow_downgrade=False,
        )


def test_allow_downgrade_false_allows_lateral_purchased_to_purchased() -> None:
    lot = _lot(user_action=UserAction.PURCHASED, won_at=EARLIER)
    session = FakeSession()
    apply_user_action(
        session, lot, UserAction.PURCHASED,
        source="discord_bot", now=FROZEN, allow_downgrade=False,
    )
    assert lot.user_action == UserAction.PURCHASED


def test_history_row_appended_on_bid_placed_with_amount() -> None:
    lot = _lot(user_action=UserAction.INTERESTED)
    session = FakeSession()
    apply_user_action(
        session, lot, UserAction.BID_PLACED,
        max_bid_cad=Decimal("500"), source="dashboard", now=FROZEN,
    )
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, LotActionHistory)
    assert row.lot_id == 1
    assert row.user_action == UserAction.BID_PLACED
    assert row.max_bid_cad == Decimal("500")
    assert row.changed_at == FROZEN
    assert row.source == "dashboard"


def test_history_row_max_bid_cad_null_when_not_bid_placed() -> None:
    lot = _lot(user_action=UserAction.INTERESTED)
    session = FakeSession()
    apply_user_action(
        session, lot, UserAction.PASSED, source="dashboard", now=FROZEN,
    )
    assert len(session.added) == 1
    row = session.added[0]
    assert row.user_action == UserAction.PASSED
    assert row.max_bid_cad is None


def test_history_row_appended_on_toggle_off() -> None:
    lot = _lot(user_action=UserAction.INTERESTED)
    session = FakeSession()
    apply_user_action(session, lot, None, source="dashboard", now=FROZEN)
    assert len(session.added) == 1
    row = session.added[0]
    assert row.user_action is None
    assert row.max_bid_cad is None
