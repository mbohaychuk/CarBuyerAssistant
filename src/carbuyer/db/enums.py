from __future__ import annotations

from enum import StrEnum


# Worker-pipeline stage statuses (one column per stage on auction_lots).
# StrEnum compares equal to plain strings so existing string queries keep working
# without TypeDecorator boilerplate; the DB column is still String(16).
class EnrichmentStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class ValuationStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    # Distinct from FAILED: the comp set was too thin to compute a fair value.
    # Shortened to fit the String(16) status columns (avoids a column-widen
    # migration just for a status value name).
    INSUFFICIENT = "insufficient"


class VisionStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    # Phase 13: all posts failed and attempts exceeded
    # settings.notification_max_attempts. Mirrors EnrichmentStatus.FAILED so
    # ops can grep for stuck notifications via a single status value.
    FAILED = "failed"


class LotStatus(StrEnum):
    OPEN = "open"
    CLOSING_SOON = "closing_soon"
    EXTENDED = "extended"
    CLOSED = "closed"
    UNSOLD = "unsold"
    SOLD = "sold"
    # Phase 13 review fix #3: bid-poller force-closed this lot because its
    # scheduled_end was >24h in the past with the source still returning
    # OPEN — distinct from CLOSED so an operator can grep for these and
    # re-open if the cause was a temporary source outage.
    FORCE_CLOSED = "force_closed"


class AuctionStatus(StrEnum):
    UPCOMING = "upcoming"
    LIVE = "live"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class UserAction(StrEnum):
    INTERESTED = "interested"
    BID_PLACED = "bid_placed"
    PURCHASED = "purchased"
    PASSED = "passed"


class AuctionSubtype(StrEnum):
    ESTATE = "estate"
    COMMERCIAL = "commercial"  # phase-2 RB / Michener Allen
