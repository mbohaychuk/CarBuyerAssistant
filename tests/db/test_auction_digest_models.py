from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.models import Auction


@pytest.mark.asyncio
async def test_auction_digest_sent_at_defaults_null(session: AsyncSession) -> None:
    a = Auction(
        source="t", source_auction_id="A1", url="u", canonical_url="u",
        auction_subtype="estate", first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    session.add(a)
    await session.flush()
    await session.refresh(a)
    assert a.digest_sent_at is None


@pytest.mark.asyncio
async def test_auction_digest_sent_at_settable(session: AsyncSession) -> None:
    a = Auction(
        source="t", source_auction_id="A2", url="u", canonical_url="u",
        auction_subtype="estate", first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        digest_sent_at=datetime(2026, 5, 29, tzinfo=UTC),
    )
    session.add(a)
    await session.flush()
    await session.refresh(a)
    assert a.digest_sent_at == datetime(2026, 5, 29, tzinfo=UTC)
