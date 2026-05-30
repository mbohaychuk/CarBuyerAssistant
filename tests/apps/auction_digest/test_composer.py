from __future__ import annotations

from datetime import UTC, datetime

from carbuyer.apps.auction_digest.composer import (
    DigestHeader,
    DigestLot,
    compose_digest,
)


def _header(**ov: object) -> DigestHeader:
    base: dict[str, object] = dict(
        auction_id=1, title="Graham Auctions", location="Headingley, MB",
        starts_at=datetime(2026, 3, 7, 16, 0, tzinfo=UTC),
        lot_count=47, vehicle_count=12, url="https://x/auction/1",
    )
    base.update(ov)
    return DigestHeader(**base)  # type: ignore[arg-type]


def _lot(i: int, *, search: str | None = None) -> DigestLot:
    return DigestLot(lot_id=i, summary=f"1968 Ford Mustang #{i}", search_name=search)


def test_empty_both_sections_returns_none() -> None:
    assert compose_digest(_header(), matches=[], rare=[]) is None


def test_only_matches() -> None:
    out = compose_digest(_header(), matches=[_lot(1, search="60s Mustang")], rare=[])
    assert out is not None
    assert "saved searches" in out.lower()
    assert "1968 Ford Mustang #1" in out
    assert "/lots/1" in out
    assert "rare" not in out.lower()  # no rare section when empty


def test_only_rare() -> None:
    out = compose_digest(_header(), matches=[], rare=[_lot(2)])
    assert out is not None
    assert "rare" in out.lower()
    assert "/lots/2" in out
    assert "saved searches" not in out.lower()


def test_both_sections_and_header() -> None:
    out = compose_digest(_header(), matches=[_lot(1, search="x")], rare=[_lot(2)])
    assert out is not None
    assert "Graham Auctions" in out
    assert "Headingley, MB" in out
    assert "/lots/1" in out and "/lots/2" in out


def test_truncates_each_section_at_ten() -> None:
    matches = [_lot(i) for i in range(1, 16)]  # 15
    out = compose_digest(_header(), matches=matches, rare=[])
    assert out is not None
    assert "/lots/10" in out
    assert "/lots/11" not in out      # capped at 10
    assert "5 more" in out            # "... and 5 more"


def test_respects_discord_2000_char_limit() -> None:
    matches = [_lot(i, search="search") for i in range(1, 11)]
    rare = [_lot(i) for i in range(11, 21)]
    out = compose_digest(_header(title="A" * 200), matches=matches, rare=rare)
    assert out is not None
    assert len(out) <= 2000  # noqa: PLR2004
