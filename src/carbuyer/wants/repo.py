"""CRUD for want-list entries (rows in the `searches` table).

Plain async functions over a caller-owned session (the convention used by
db.upserts / dashboard query modules), not a repository class. Mutators flush to
populate PKs but leave the commit to the caller so a router/bot/worker controls
the transaction boundary. Criteria are stored as the JSONB `config`; callers read
them back with WantCriteria.model_validate(search.config).
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from carbuyer.db.enums import ListingStatus, LotStatus
from carbuyer.db.models import AuctionLot, PrivateListing, Search, VehicleOffer, WantMatch
from carbuyer.wants.criteria import WantCriteria

_MAX_NAME_LEN = 128  # matches Search.name String(128)

# Auction-lot statuses where the lot is still biddable (a live sibling that can
# legitimately suppress a same-VIN duplicate). Mirrors wants.service._OPEN_STATUSES.
_LIVE_LOT_STATUSES = (
    LotStatus.OPEN.value,
    LotStatus.CLOSING_SOON.value,
    LotStatus.EXTENDED.value,
)


async def create_want(
    session: AsyncSession,
    *,
    name: str,
    criteria: WantCriteria,
    user_id: str = "me",
) -> Search:
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > _MAX_NAME_LEN:
        raise ValueError(f"name too long (max {_MAX_NAME_LEN} characters)")
    want = Search(
        user_id=user_id,
        name=name,
        config=criteria.model_dump(mode="json"),
    )
    session.add(want)
    await session.flush()
    return want


async def get_want(session: AsyncSession, want_id: int) -> Search | None:
    return await session.get(Search, want_id)


async def list_wants(
    session: AsyncSession,
    *,
    user_id: str = "me",
    enabled_only: bool = False,
) -> list[Search]:
    stmt = select(Search).where(Search.user_id == user_id)
    if enabled_only:
        stmt = stmt.where(Search.enabled.is_(True))
    stmt = stmt.order_by(Search.id)
    return list((await session.execute(stmt)).scalars())


async def update_want(
    session: AsyncSession,
    want_id: int,
    *,
    name: str | None = None,
    criteria: WantCriteria | None = None,
    enabled: bool | None = None,
) -> Search | None:
    want = await session.get(Search, want_id)
    if want is None:
        return None
    if name is not None:
        want.name = name
    if criteria is not None:
        want.config = criteria.model_dump(mode="json")
    if enabled is not None:
        want.enabled = enabled
    await session.flush()
    return want


async def delete_want(session: AsyncSession, want_id: int) -> bool:
    want = await session.get(Search, want_id)
    if want is None:
        return False
    await session.delete(want)
    await session.flush()
    return True


async def upsert_want_match(
    session: AsyncSession,
    *,
    search_id: int,
    lot_id: int,
    want_relative_score: float | None,
) -> tuple[WantMatch, bool]:
    """Insert a (search, lot) match or refresh its score. Returns (row, created).
    On an existing row, only the score is updated — notified_at and dismissed are
    preserved, so a re-evaluation (e.g. a new bid) re-scores without re-alerting.
    """
    existing = (
        await session.execute(
            select(WantMatch).where(
                WantMatch.search_id == search_id,
                WantMatch.lot_id == lot_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.want_relative_score = want_relative_score
        await session.flush()
        return existing, False
    match = WantMatch(
        search_id=search_id, lot_id=lot_id, want_relative_score=want_relative_score
    )
    # Cross-source VIN dedup: the same physical vehicle can be listed on several
    # sources (separate offer rows, one VIN). Auto-dismiss this match if the same
    # VIN already matched this want via another *live* offer, so the vehicle alerts
    # once. dismissed is what the notifier + dashboard already filter on, so no new
    # state is needed.
    # Known limits of reusing `dismissed` (revisit if cross-source listings get
    # common): (1) it can't be told apart from a user dismissal, so a price drop on
    # the suppressed copy won't re-alert (upsert_private_listing's clear filters
    # dismissed); (2) if the live winner later sells, nothing re-opens an
    # already-suppressed duplicate — a failover reconciliation would be a separate
    # piece. The live-sibling check below covers the common auction->retail flip
    # where the winner is already dead when the duplicate first appears.
    if await _vin_already_matched(session, search_id=search_id, lot_id=lot_id):
        match.dismissed = True
    session.add(match)
    await session.flush()
    return match, True


async def _vin_already_matched(
    session: AsyncSession, *, search_id: int, lot_id: int,
) -> bool:
    """True if a non-dismissed match for this want already covers this offer's VIN
    via a different, still-LIVE offer (open auction lot or active listing). VIN-less
    offers can't be deduped (returns False); a sold/removed sibling doesn't count."""
    vin = (
        await session.execute(select(VehicleOffer.vin).where(VehicleOffer.id == lot_id))
    ).scalar_one_or_none()
    if not vin:
        return False
    vo, al, pl, wm = (
        VehicleOffer.__table__, AuctionLot.__table__, PrivateListing.__table__,
        WantMatch.__table__,
    )
    stmt = (
        select(wm.c.id)
        .select_from(
            wm.join(vo, vo.c.id == wm.c.lot_id)
            .outerjoin(al, al.c.id == vo.c.id)
            .outerjoin(pl, pl.c.id == vo.c.id)
        )
        .where(
            wm.c.search_id == search_id,
            wm.c.lot_id != lot_id,
            wm.c.dismissed.is_(False),
            func.upper(vo.c.vin) == vin.upper(),
            or_(
                al.c.lot_status.in_(_LIVE_LOT_STATUSES),
                pl.c.listing_status == ListingStatus.ACTIVE.value,
            ),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None
