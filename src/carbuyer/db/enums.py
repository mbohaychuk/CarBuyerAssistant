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
    INSUFFICIENT_COMPS = "insufficient_comps"


class VisionStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"


class LotStatus(StrEnum):
    OPEN = "open"
    CLOSING_SOON = "closing_soon"
    EXTENDED = "extended"
    CLOSED = "closed"
    UNSOLD = "unsold"
    SOLD = "sold"


class AuctionStatus(StrEnum):
    UPCOMING = "upcoming"
    LIVE = "live"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class UserAction(StrEnum):
    INTERESTED = "interested"
    MAYBE = "maybe"
    NOT_INTERESTED = "not_interested"


class AuctionSubtype(StrEnum):
    ESTATE = "estate"
    COMMERCIAL = "commercial"  # phase-2 RB / Michener Allen
