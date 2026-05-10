from __future__ import annotations

from datetime import UTC, datetime, timedelta

from carbuyer.apps.bid_poller.scheduler import next_poll_delay


NOW = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)


def test_far_lot_polls_hourly() -> None:
    assert next_poll_delay(
        scheduled_end=NOW + timedelta(days=2), now=NOW, status="open"
    ) == timedelta(minutes=60)  # noqa: PLR2004


def test_closing_window_speeds_up() -> None:
    assert next_poll_delay(
        scheduled_end=NOW + timedelta(minutes=30), now=NOW, status="open"
    ) == timedelta(minutes=1)  # noqa: PLR2004
    assert next_poll_delay(
        scheduled_end=NOW + timedelta(minutes=5), now=NOW, status="open"
    ) == timedelta(seconds=30)  # noqa: PLR2004


def test_closed_lot_throttles() -> None:
    assert next_poll_delay(
        scheduled_end=NOW + timedelta(minutes=5), now=NOW, status="closed"
    ) == timedelta(hours=24)  # noqa: PLR2004


def test_no_scheduled_end_polls_hourly() -> None:
    assert next_poll_delay(
        scheduled_end=None, now=NOW, status="open"
    ) == timedelta(minutes=60)  # noqa: PLR2004


def test_unsold_and_sold_throttle() -> None:
    """All terminal statuses (not just 'closed') should throttle to 24h."""
    for status in ("unsold", "sold"):
        assert next_poll_delay(
            scheduled_end=NOW + timedelta(minutes=5), now=NOW, status=status
        ) == timedelta(hours=24)  # noqa: PLR2004


def test_one_to_two_hours_polls_every_five_minutes() -> None:
    assert next_poll_delay(
        scheduled_end=NOW + timedelta(hours=1, minutes=30), now=NOW, status="open"
    ) == timedelta(minutes=5)  # noqa: PLR2004


def test_two_to_twenty_four_hours_polls_every_fifteen_minutes() -> None:
    assert next_poll_delay(
        scheduled_end=NOW + timedelta(hours=12), now=NOW, status="open"
    ) == timedelta(minutes=15)  # noqa: PLR2004
