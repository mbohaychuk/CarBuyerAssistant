"""ORM models for the auction MVP.

Worker column-ownership rule: each block under AuctionLot is annotated with
the worker that owns the columns. Workers UPDATE only their own columns; never
session.merge(lot) the whole row back. Bid-poller updates bid-state; enricher
updates enrichment + rarity (LLM); valuator updates valuation +
historical_comp_count; vision-batcher updates vision_*; notifier updates
*_notified_at; user actions come from the dashboard.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from carbuyer.db.base import Base, TimestampMixin


class Auction(Base, TimestampMixin):
    __tablename__ = "auctions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_auction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    auction_subtype: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="estate",
        server_default="estate",
    )
    auctioneer_name: Mapped[str | None] = mapped_column(String(255))
    auctioneer_external_id: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    terms_text: Mapped[str | None] = mapped_column(Text)
    scheduled_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pickup_address: Mapped[str | None] = mapped_column(Text)
    pickup_city: Mapped[str | None] = mapped_column(String(128))
    pickup_province: Mapped[str | None] = mapped_column(String(8))
    pickup_window_text: Mapped[str | None] = mapped_column(Text)
    buyer_premium_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    # Cap and floor on the *premium amount* (not the percent). Both nullable.
    # When NULL, premium is purely linear (pct * bid) -- current HiBid behavior.
    # McDougall states "15% to a Max $2000, Min $20" so max=2000, min=20.
    buyer_premium_max_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    buyer_premium_min_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    online_bidding_fee_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    gst_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    pst_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="upcoming",
        server_default="upcoming",
        index=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    discovery_confidence: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="high",
        server_default="high",
    )
    needs_plugin_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    routing_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Indexed for dashboard hrefs / admin lookup; NOT unique — multi-router can
    # produce two rows with the same canonical_url under different `source`
    # values when one router has a real plugin and another fell back to
    # `unknown:{host}`. Dedup is via (source, source_auction_id).
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # Names of Source plugins/routers that have surfaced this auction.
    # text[] (not JSONB) so we can append + dedupe atomically inside ON CONFLICT.
    discovered_via: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        default=list,
        server_default=text("'{}'::text[]"),
        nullable=False,
    )

    lots: Mapped[list[AuctionLot]] = relationship(back_populates="auction", lazy="raise")

    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_auction_id",
            name="uq_auctions_source_source_auction_id",
        ),
    )


class AuctionLot(Base, TimestampMixin):
    __tablename__ = "auction_lots"

    # ── Owned by: lot-scraper (initial insert + URL/photo refresh) ──────────
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    auction_id: Mapped[int] = mapped_column(
        ForeignKey("auctions.id", ondelete="CASCADE"),
        index=True,
    )
    source_lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # HiBid exposes two ids per lot: stable `itemId` (vehicle identity, stored
    # in source_lot_id, used as upsert key) and a per-listing row `id` (used
    # by HiBid's eventItemIds filter for single-lot lookups in bid_poller).
    # Other sources don't need this and leave it NULL.
    source_lot_row_id: Mapped[int | None] = mapped_column(BigInteger)
    lot_number: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # parser_version: the source plugin's parser version at the time this row
    # was scraped. Used to detect rows that need re-enrichment after a parser
    # change (Source.version on the plugin → propagated here on every upsert).
    parser_version: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    photos: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        default=list,
        server_default=text("'{}'::text[]"),
        nullable=False,
    )
    # Per-lot end time. NULL for HiBid (auction.scheduled_end_at covers the
    # whole event); populated for McDougall where each lot in an auction-event
    # has its own close time. bid_poller coalesces auction-then-lot when
    # priority-sorting and force-close-checking.
    scheduled_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True,
    )

    # ── Owned by: lot-scraper (initial), description-enricher (LLM normalization) ──
    year: Mapped[int | None] = mapped_column(Integer)
    make: Mapped[str | None] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(64), index=True)
    trim: Mapped[str | None] = mapped_column(String(64))
    engine: Mapped[str | None] = mapped_column(String(64))
    transmission: Mapped[str | None] = mapped_column(String(16))
    drivetrain: Mapped[str | None] = mapped_column(String(16))
    mileage_km: Mapped[int | None] = mapped_column(Integer)
    vin: Mapped[str | None] = mapped_column(String(32))
    title_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="UNKNOWN",
        server_default="UNKNOWN",
    )
    province_of_origin: Mapped[str | None] = mapped_column(String(8))

    # ── Owned by: description-enricher (LLM description pass) ───────────────
    condition_categorical: Mapped[str | None] = mapped_column(String(16))
    condition_confidence: Mapped[float | None] = mapped_column()
    red_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    green_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    showstopper_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    summary: Mapped[str | None] = mapped_column(Text)
    carfax_url: Mapped[str | None] = mapped_column(Text)
    carfax_findings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # ── Owned by: description-enricher (rarity/desirability fields, LLM) ────
    desirable_trim_or_spec: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    classic_or_collector: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    desirability_signals: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    desirability_evidence: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    # historical_comp_count: written by valuator (DB-derived signal).
    historical_comp_count: Mapped[int | None] = mapped_column(Integer)
    recent_appreciation: Mapped[float | None] = mapped_column()
    # rarity_score: combined LLM + DB; written by valuator.
    rarity_score: Mapped[float | None] = mapped_column()

    # ── Owned by: vision-batcher (nightly two-pass) ─────────────────────────
    vision_findings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    vision_condition_overall: Mapped[str | None] = mapped_column(String(16))
    vision_confidence: Mapped[float | None] = mapped_column()
    vision_contradictions: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )

    # ── Owned by: bid-poller (continuous tiered cadence) ────────────────────
    current_high_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    last_bid_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bid_count_visible: Mapped[int | None] = mapped_column(Integer)
    reserve_met: Mapped[bool | None] = mapped_column(Boolean)
    lot_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="open",
        server_default="open",
        index=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # ── Owned by: valuator ──────────────────────────────────────────────────
    comp_count: Mapped[int | None] = mapped_column(Integer)
    value_low_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    value_mid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    value_high_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    expected_value_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    landed_cost_premium_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    all_in_at_current_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    recommended_max_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_deal_score: Mapped[float | None] = mapped_column()
    flag_score: Mapped[int | None] = mapped_column(Integer)
    confidence_bucket: Mapped[str | None] = mapped_column(String(16))
    suspicious_underprice_flag: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    scoring_version: Mapped[str | None] = mapped_column(String(32))
    weights_hash: Mapped[str | None] = mapped_column(String(64))

    # ── Owned by: pipeline workers (each writes its own status column) ──────
    # See carbuyer.db.enums for valid values; column is String(16) so PG enum
    # migration churn is avoided. Workers compare against StrEnum members.
    enrichment_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    valuation_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    vision_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    notification_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    enrichment_version: Mapped[str | None] = mapped_column(String(32))
    # Retry counter for the description-enricher. Incremented on every attempt;
    # transient failures (rate limits, 5xx, network) leave status at PENDING for
    # re-claim until attempts >= settings.enrichment_max_attempts. Schema /
    # validation errors fail-fast (FAILED at attempts=1).
    enrichment_attempts: Mapped[int] = mapped_column(
        Integer,
        server_default=text("0"),
        nullable=False,
    )
    last_enrichment_error: Mapped[str | None] = mapped_column(Text)
    # Phase 4 overlay #6: separate per-stage retry counter so failure modes are
    # diagnosable. Same idiom as enrichment_attempts — exceptions inside the
    # valuator increment this; status returns to PENDING for re-claim until
    # attempts >= settings.valuation_max_attempts; bad-shape lots (no
    # make/model/year) skip directly via valuation_status='skipped' without
    # consuming an attempt.
    valuation_attempts: Mapped[int] = mapped_column(
        Integer,
        server_default=text("0"),
        nullable=False,
    )
    last_valuation_error: Mapped[str | None] = mapped_column(Text)
    # Phase 13 (review fix): retry counter for the notifier. Mirrors
    # enrichment_attempts / valuation_attempts. A Discord POST that returns
    # False (429-after-retry, 4xx, network blip, missing channel) increments
    # this and leaves notification_status at PENDING for re-claim until
    # attempts >= settings.notification_max_attempts, at which point the
    # status is flipped to FAILED. Without this, every transient Discord blip
    # silently lost the notification (lot looked DONE in the DB; no message).
    notification_attempts: Mapped[int] = mapped_column(
        Integer,
        server_default=text("0"),
        nullable=False,
    )
    last_notification_error: Mapped[str | None] = mapped_column(Text)
    # Inferred-from-sparse-listing flag: when condition_confidence < 0.5 the
    # enricher coerces condition_categorical to "decent" but sets this True so
    # Phase 4 valuation can apply a separate sparse-listing pessimism penalty
    # (vs. a genuinely confident "decent" rating).
    condition_inferred_from_sparse_listing: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    # LLM's self-assessment of how thin/detailed the listing description was.
    # Values: "thin" | "adequate" | "detailed". Phase 4 uses to dampen scoring
    # of low-evidence listings.
    description_quality: Mapped[str | None] = mapped_column(String(16))

    # ── Owned by: notifier (one timestamp per trigger type) ─────────────────
    early_warning_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cheap_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closing_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trajectory_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extended_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_notified_channel: Mapped[str | None] = mapped_column(String(64))

    # ── Owned by: dashboard (user input) ────────────────────────────────────
    user_action: Mapped[str | None] = mapped_column(String(16), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    was_purchased_by_us: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
        index=True,
    )

    auction: Mapped[Auction] = relationship(back_populates="lots", lazy="raise")
    bid_history: Mapped[list[AuctionBidHistory]] = relationship(
        back_populates="lot",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,  # delegate child DELETE to DB FK ondelete=CASCADE; skip child SELECT
    )

    __table_args__ = (
        UniqueConstraint(
            "auction_id",
            "source_lot_id",
            name="uq_auction_lots_auction_source_lot",
        ),
        Index("ix_auction_lots_make_model_year", "make", "model", "year"),
        Index("ix_auction_lots_price_deal_score", "price_deal_score", "lot_status"),
        # rarity_score + auction.scheduled_end_at would be ideal but spans tables;
        # the lot-level partial index works for early-warning triage at query time.
        Index("ix_auction_lots_rarity_score", "rarity_score"),
        # Partial indexes for queue claims — most rows are non-pending, so a
        # full-table b-tree on the status column is mostly dead weight.
        Index(
            "ix_auction_lots_enrichment_pending",
            "id",
            postgresql_where=text("enrichment_status = 'pending'"),
        ),
        Index(
            "ix_auction_lots_valuation_pending",
            "id",
            postgresql_where=text("valuation_status = 'pending'"),
        ),
        Index(
            "ix_auction_lots_vision_pending",
            "id",
            postgresql_where=text("vision_status = 'pending'"),
        ),
        Index(
            "ix_auction_lots_notification_pending",
            "id",
            postgresql_where=text("notification_status = 'pending'"),
        ),
    )


class AuctionBidHistory(Base):
    __tablename__ = "auction_bid_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lot_id: Mapped[int] = mapped_column(
        ForeignKey("auction_lots.id", ondelete="CASCADE"),
        index=True,
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_high_bid_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    end_time_at_observation: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_at_observation: Mapped[str | None] = mapped_column(String(32))

    lot: Mapped[AuctionLot] = relationship(back_populates="bid_history", lazy="raise")

    __table_args__ = (Index("ix_bid_history_lot_observed", "lot_id", "observed_at"),)


class HistoricalSale(Base, TimestampMixin):
    __tablename__ = "historical_sales"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    make: Mapped[str | None] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(64), index=True)
    trim: Mapped[str | None] = mapped_column(String(64))
    engine: Mapped[str | None] = mapped_column(String(64))
    transmission: Mapped[str | None] = mapped_column(String(16))
    drivetrain: Mapped[str | None] = mapped_column(String(16))
    mileage_km: Mapped[int | None] = mapped_column(Integer)
    vin: Mapped[str | None] = mapped_column(String(32))
    title_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="UNKNOWN",
        server_default="UNKNOWN",
    )
    province_of_origin: Mapped[str | None] = mapped_column(String(8))
    condition_categorical: Mapped[str | None] = mapped_column(String(16))
    final_listed_price_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    days_listed: Mapped[int | None] = mapped_column(Integer)
    buyer_premium_pct_at_sale: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    final_price_with_premium_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    sale_channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sale_platform: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seller_province: Mapped[str | None] = mapped_column(String(8))
    seller_city: Mapped[str | None] = mapped_column(String(128))
    observed_first_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disappeared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disposition_reason: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    was_notified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    was_purchased_by_us: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )


class Purchase(Base, TimestampMixin):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    sale_date: Mapped[date | None] = mapped_column(Date)
    make: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_price_cad: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    sale_price_cad: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    province_of_purchase: Mapped[str | None] = mapped_column(String(8))
    province_of_sale: Mapped[str | None] = mapped_column(String(8))
    transport_cost_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    inspection_cost_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    repair_cost_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    linked_lot_id: Mapped[int | None] = mapped_column(ForeignKey("auction_lots.id"))


class Search(Base, TimestampMixin):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="me",
        server_default="me",
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        nullable=False,
    )


class SourceAlertState(Base):
    """Last-alerted-at timestamp per source for the stale-source watchdog.

    A separate tiny table rather than a column on auctions because freshness
    is computed via aggregation over auctions.last_seen_at — the watchdog
    needs only a dedup window, not freshness state. One row per registered
    source; rows materialize on first alert and are upserted thereafter.
    """

    __tablename__ = "source_alert_state"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_alerted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
