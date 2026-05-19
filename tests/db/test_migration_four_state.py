"""End-to-end test for the four-state migration.

Spins up a separate test database, runs alembic upgrade/downgrade against
it explicitly (the regular conftest builds schema via Base.metadata
.create_all and skips alembic), seeds fixture rows, asserts the result.
"""
from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from alembic import command

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# The immediate predecessor of our migration. Update if down_revision changes.
PREV_HEAD = "a7d3a0c1e927"


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return cfg


@pytest.fixture
def migration_db() -> Generator[sa.Engine, None, None]:
    """Fresh sync engine pointed at carbuyer_migration_test schema.

    Drops + creates the schema before each test so each scenario starts
    from a known state. Sync because alembic command.* is sync.
    """
    # Find the dev URL. Look at tests/conftest.py:21-31 for the pattern.
    base = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://carbuyer:local@localhost:5433/carbuyer",
    )
    if base.endswith("/carbuyer"):
        url = base[: -len("/carbuyer")] + "/carbuyer_migration_test"
    else:
        pytest.skip("DATABASE_URL doesn't look like the dev URL")

    sync_url = url.replace("+psycopg_async", "+psycopg")
    eng = sa.create_engine(sync_url)
    with eng.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))

    cfg = _alembic_config(sync_url)
    command.upgrade(cfg, PREV_HEAD)
    yield eng
    eng.dispose()


def _seed_pre_migration_lots(eng: sa.Engine) -> dict[str, int]:
    """Insert one row per pre-migration scenario. Returns label → row id.

    Pre-migration auction_lots has: user_action ∈ {interested, maybe,
    not_interested, NULL} and was_purchased_by_us boolean. No max_bid_cad
    / bid_placed_at / won_at columns yet.
    """
    ids: dict[str, int] = {}
    with eng.begin() as conn:
        # An auction row that all lots can FK to.
        # Column names match the real schema at PREV_HEAD (a7d3a0c1e927):
        # - url (not source_url), no schema_version, no sources_seen
        # - first_seen_at / last_seen_at / canonical_url are NOT NULL
        conn.execute(sa.text("""
            INSERT INTO auctions (
                id, source, source_auction_id, url, canonical_url,
                title, status, first_seen_at, last_seen_at,
                created_at, updated_at
            ) VALUES (
                1, 'hibid', 'A1', 'https://x.com/a', 'https://x.com/a',
                'A', 'live', now(), now(),
                now(), now()
            )
        """))
        scenarios = {
            "interested": ("interested", False),
            "maybe": ("maybe", False),
            "not_interested": ("not_interested", False),
            "purchased_flag_only": (None, True),
            "purchased_with_interested": ("interested", True),
            "purchased_with_not_interested": ("not_interested", True),
        }
        for label, (ua, wpbu) in scenarios.items():
            # auction_lots at PREV_HEAD: no source column, no schema_version
            result = conn.execute(
                sa.text("""
                    INSERT INTO auction_lots (
                        auction_id, source_lot_id, url,
                        user_action, was_purchased_by_us,
                        created_at, updated_at
                    ) VALUES (
                        1, :slid, :url,
                        :ua, :wpbu,
                        now(), now()
                    ) RETURNING id
                """),
                {
                    "slid": label,
                    "url": f"https://x.com/{label}",
                    "ua": ua,
                    "wpbu": wpbu,
                },
            )
            ids[label] = result.scalar_one()
    return ids


def test_upgrade_backfill_maps_correctly(migration_db: sa.Engine) -> None:
    ids = _seed_pre_migration_lots(migration_db)
    cfg = _alembic_config(migration_db.url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")

    with migration_db.begin() as conn:
        rows = {
            row.id: row.user_action
            for row in conn.execute(sa.text(
                "SELECT id, user_action FROM auction_lots"
            ))
        }

    assert rows[ids["interested"]] == "interested"
    assert rows[ids["maybe"]] == "interested"
    assert rows[ids["not_interested"]] == "passed"
    assert rows[ids["purchased_flag_only"]] == "purchased"
    assert rows[ids["purchased_with_interested"]] == "purchased"
    # The ORDERING INVARIANT scenario: purchased wins over passed.
    assert rows[ids["purchased_with_not_interested"]] == "purchased"


def test_was_purchased_by_us_column_dropped(migration_db: sa.Engine) -> None:
    _seed_pre_migration_lots(migration_db)
    cfg = _alembic_config(migration_db.url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")

    inspector = sa.inspect(migration_db)
    cols = {c["name"] for c in inspector.get_columns("auction_lots")}
    assert "was_purchased_by_us" not in cols
    assert "max_bid_cad" in cols
    assert "bid_placed_at" in cols
    assert "won_at" in cols


def test_history_seeded_for_labeled_lots(migration_db: sa.Engine) -> None:
    ids = _seed_pre_migration_lots(migration_db)
    cfg = _alembic_config(migration_db.url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")

    with migration_db.begin() as conn:
        history_rows = conn.execute(sa.text(
            "SELECT lot_id, user_action, source FROM lot_action_history "
            "ORDER BY lot_id"
        )).all()

    # Every lot that has a non-NULL user_action POST-migration gets a row.
    # purchased_flag_only had NULL user_action pre-migration but was promoted
    # to 'purchased' by step 4, so it gets seeded too.
    expected_lot_ids = sorted(ids.values())
    actual_lot_ids = sorted(r.lot_id for r in history_rows)
    assert actual_lot_ids == expected_lot_ids
    for row in history_rows:
        assert row.source == "migration"


def test_check_rejects_bid_placed_without_max_bid(
    migration_db: sa.Engine,
) -> None:
    cfg = _alembic_config(migration_db.url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")

    with migration_db.begin() as conn:
        conn.execute(sa.text("""
            INSERT INTO auctions (
                id, source, source_auction_id, url, canonical_url,
                title, status, first_seen_at, last_seen_at,
                created_at, updated_at
            ) VALUES (
                1, 'hibid', 'A1', 'https://x.com/a', 'https://x.com/a',
                'A', 'live', now(), now(),
                now(), now()
            )
        """))

    with pytest.raises(IntegrityError):
        with migration_db.begin() as conn:
            conn.execute(sa.text("""
                INSERT INTO auction_lots (
                    auction_id, source_lot_id, url, user_action,
                    max_bid_cad, bid_placed_at, won_at,
                    created_at, updated_at
                ) VALUES (
                    1, 'L1', 'https://x.com/L1', 'bid_placed',
                    NULL, NULL, NULL,
                    now(), now()
                )
            """))


def test_check_rejects_purchased_without_won_at(
    migration_db: sa.Engine,
) -> None:
    cfg = _alembic_config(migration_db.url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")

    with migration_db.begin() as conn:
        conn.execute(sa.text("""
            INSERT INTO auctions (
                id, source, source_auction_id, url, canonical_url,
                title, status, first_seen_at, last_seen_at,
                created_at, updated_at
            ) VALUES (
                1, 'hibid', 'A1', 'https://x.com/a', 'https://x.com/a',
                'A', 'live', now(), now(),
                now(), now()
            )
        """))

    with pytest.raises(IntegrityError):
        with migration_db.begin() as conn:
            conn.execute(sa.text("""
                INSERT INTO auction_lots (
                    auction_id, source_lot_id, url, user_action,
                    won_at, created_at, updated_at
                ) VALUES (
                    1, 'L1', 'https://x.com/L1', 'purchased',
                    NULL, now(), now()
                )
            """))


def test_check_rejects_passed_with_bid_amount(
    migration_db: sa.Engine,
) -> None:
    """NULL-safe CHECK gap closer: max_bid_cad must be NULL when not bid_placed."""
    cfg = _alembic_config(migration_db.url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")

    with migration_db.begin() as conn:
        conn.execute(sa.text("""
            INSERT INTO auctions (
                id, source, source_auction_id, url, canonical_url,
                title, status, first_seen_at, last_seen_at,
                created_at, updated_at
            ) VALUES (
                1, 'hibid', 'A1', 'https://x.com/a', 'https://x.com/a',
                'A', 'live', now(), now(),
                now(), now()
            )
        """))

    with pytest.raises(IntegrityError):
        with migration_db.begin() as conn:
            conn.execute(sa.text("""
                INSERT INTO auction_lots (
                    auction_id, source_lot_id, url, user_action,
                    max_bid_cad, bid_placed_at, won_at,
                    created_at, updated_at
                ) VALUES (
                    1, 'L1', 'https://x.com/L1', 'passed',
                    500, NULL, NULL,
                    now(), now()
                )
            """))


def test_check_rejects_null_user_action_with_bid_amount(
    migration_db: sa.Engine,
) -> None:
    """NULL-safe CHECK gap closer: NULL user_action requires NULL bid fields."""
    cfg = _alembic_config(migration_db.url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")

    with migration_db.begin() as conn:
        conn.execute(sa.text("""
            INSERT INTO auctions (
                id, source, source_auction_id, url, canonical_url,
                title, status, first_seen_at, last_seen_at,
                created_at, updated_at
            ) VALUES (
                1, 'hibid', 'A1', 'https://x.com/a', 'https://x.com/a',
                'A', 'live', now(), now(),
                now(), now()
            )
        """))

    with pytest.raises(IntegrityError):
        with migration_db.begin() as conn:
            conn.execute(sa.text("""
                INSERT INTO auction_lots (
                    auction_id, source_lot_id, url, user_action,
                    max_bid_cad, bid_placed_at, won_at,
                    created_at, updated_at
                ) VALUES (
                    1, 'L1', 'https://x.com/L1', NULL,
                    500, NULL, NULL,
                    now(), now()
                )
            """))


def test_downgrade_roundtrip(migration_db: sa.Engine) -> None:
    ids = _seed_pre_migration_lots(migration_db)
    cfg = _alembic_config(migration_db.url.render_as_string(hide_password=False))

    command.upgrade(cfg, "head")
    command.downgrade(cfg, PREV_HEAD)

    inspector = sa.inspect(migration_db)
    cols = {c["name"] for c in inspector.get_columns("auction_lots")}
    assert "was_purchased_by_us" in cols
    assert "max_bid_cad" not in cols
    assert "lot_action_history" not in inspector.get_table_names()

    with migration_db.begin() as conn:
        rows = {
            row.id: (row.user_action, row.was_purchased_by_us)
            for row in conn.execute(sa.text(
                "SELECT id, user_action, was_purchased_by_us FROM auction_lots"
            ))
        }

    # passed → not_interested; purchased → interested + flag TRUE
    assert rows[ids["not_interested"]] == ("not_interested", False)
    assert rows[ids["purchased_flag_only"]] == ("interested", True)
    # Lossy: maybe stays interested.
    assert rows[ids["maybe"]] == ("interested", False)
