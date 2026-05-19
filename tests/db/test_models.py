from carbuyer.db.base import Base
from carbuyer.db.enums import (
    AuctionStatus,
    EnrichmentStatus,
    LotStatus,
    NotificationStatus,
    UserAction,
    ValuationStatus,
    VisionStatus,
)
from carbuyer.db.models import (
    Auction,
    AuctionBidHistory,
    AuctionLot,
    HistoricalSale,
    Purchase,
    Search,
)


def test_models_importable() -> None:
    assert Auction.__tablename__ == "auctions"
    assert AuctionLot.__tablename__ == "auction_lots"
    assert AuctionBidHistory.__tablename__ == "auction_bid_history"
    assert HistoricalSale.__tablename__ == "historical_sales"
    assert Purchase.__tablename__ == "purchases"
    assert Search.__tablename__ == "searches"


def test_status_enums_have_expected_values() -> None:
    # StrEnum: comparable to plain strings; persisted as String(16) in DB.
    assert EnrichmentStatus.PENDING == "pending"
    assert ValuationStatus.DONE == "done"
    assert VisionStatus.SKIPPED == "skipped"
    assert NotificationStatus.PENDING == "pending"
    assert LotStatus.OPEN == "open"
    assert AuctionStatus.UPCOMING == "upcoming"
    assert UserAction.INTERESTED == "interested"


def test_auction_lot_has_required_columns() -> None:
    cols = {c.name for c in AuctionLot.__table__.columns}
    expected = {
        "id", "auction_id", "source_lot_id", "lot_number", "url",
        "parser_version",
        "title", "description", "photos",
        "year", "make", "model", "trim", "engine", "transmission", "drivetrain",
        "mileage_km", "vin", "title_status", "province_of_origin",
        "condition_categorical", "condition_confidence",
        "red_flags", "green_flags", "showstopper_flags",
        "summary", "carfax_url", "carfax_findings",
        "desirable_trim_or_spec", "classic_or_collector",
        "desirability_signals", "desirability_evidence",
        "historical_comp_count", "recent_appreciation", "rarity_score",
        "vision_findings", "vision_condition_overall", "vision_confidence",
        "vision_contradictions",
        "current_high_bid_cad", "last_bid_observed_at", "bid_count_visible",
        "reserve_met", "lot_status", "closed_at", "final_bid_cad",
        "comp_count", "value_low_cad", "value_mid_cad", "value_high_cad",
        "expected_value_cad", "landed_cost_premium_cad",
        "all_in_at_current_bid_cad", "recommended_max_bid_cad",
        "price_deal_score", "flag_score", "confidence_bucket",
        "suspicious_underprice_flag", "scoring_version", "weights_hash",
        "enrichment_status", "valuation_status", "vision_status",
        "notification_status", "enrichment_version",
        "early_warning_notified_at", "cheap_notified_at", "closing_notified_at",
        "trajectory_notified_at", "extended_notified_at", "last_notified_channel",
        "user_action", "max_bid_cad", "bid_placed_at", "won_at", "notes",
        "created_at", "updated_at",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_lot_action_history_table_present():
    assert "lot_action_history" in Base.metadata.tables
    cols = {c.name for c in Base.metadata.tables["lot_action_history"].columns}
    assert cols == {
        "id", "lot_id", "user_action", "max_bid_cad", "changed_at", "source",
    }
