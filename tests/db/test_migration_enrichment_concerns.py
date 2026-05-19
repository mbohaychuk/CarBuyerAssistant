"""End-to-end test for the enrichment-concerns / mileage-provenance migration.

Spins up a separate test database, runs alembic upgrade/downgrade against
it explicitly (the regular conftest builds schema via Base.metadata
.create_all and skips alembic), seeds a fixture row, asserts the result.
"""
from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# The immediate predecessor of our migration. Update if down_revision changes.
PREV_HEAD = "a9cef3ed161c"


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return cfg


@pytest.fixture
def migration_db() -> Generator[sa.Engine, None, None]:
    """Fresh sync engine on a throwaway carbuyer_migration_test database.

    Sync, not async: alembic's command.* API runs synchronously.
    """
    base = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://carbuyer:local@localhost:5433/carbuyer",
    )
    if base.endswith("/carbuyer"):
        url = base[: -len("/carbuyer")] + "/carbuyer_migration_test"
    else:
        pytest.skip("DATABASE_URL doesn't look like the dev URL")

    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))

    cfg = _alembic_config(url)
    command.upgrade(cfg, PREV_HEAD)
    yield eng
    eng.dispose()


def _seed_pre_migration_lot(eng: sa.Engine) -> int:
    """Insert one auction + lot at PREV_HEAD. Returns the lot id."""
    with eng.begin() as conn:
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
        result = conn.execute(sa.text("""
            INSERT INTO auction_lots (
                auction_id, source_lot_id, url, user_action,
                created_at, updated_at
            ) VALUES (
                1, 'L1', 'https://x.com/L1', 'interested',
                now(), now()
            ) RETURNING id
        """))
        return result.scalar_one()


def test_upgrade_adds_columns_with_defaults(migration_db: sa.Engine) -> None:
    lot_id = _seed_pre_migration_lot(migration_db)
    cfg = _alembic_config(
        migration_db.url.render_as_string(hide_password=False)
    )
    command.upgrade(cfg, "head")

    inspector = sa.inspect(migration_db)
    cols = {c["name"] for c in inspector.get_columns("auction_lots")}
    assert "llm_concerns" in cols
    assert "mileage_is_verified" in cols

    with migration_db.begin() as conn:
        row = conn.execute(sa.text(
            "SELECT llm_concerns, mileage_is_verified "
            "FROM auction_lots WHERE id = :id"
        ), {"id": lot_id}).one()

    assert row.llm_concerns == []
    assert row.mileage_is_verified is None


def test_downgrade_drops_columns(migration_db: sa.Engine) -> None:
    _seed_pre_migration_lot(migration_db)
    cfg = _alembic_config(
        migration_db.url.render_as_string(hide_password=False)
    )

    command.upgrade(cfg, "head")
    command.downgrade(cfg, PREV_HEAD)

    inspector = sa.inspect(migration_db)
    cols = {c["name"] for c in inspector.get_columns("auction_lots")}
    assert "llm_concerns" not in cols
    assert "mileage_is_verified" not in cols


def test_roundtrip_clean(migration_db: sa.Engine) -> None:
    lot_id = _seed_pre_migration_lot(migration_db)
    cfg = _alembic_config(
        migration_db.url.render_as_string(hide_password=False)
    )

    command.upgrade(cfg, "head")
    command.downgrade(cfg, PREV_HEAD)
    command.upgrade(cfg, "head")

    with migration_db.begin() as conn:
        row = conn.execute(sa.text(
            "SELECT llm_concerns, mileage_is_verified "
            "FROM auction_lots WHERE id = :id"
        ), {"id": lot_id}).one()

    assert row.llm_concerns == []
    assert row.mileage_is_verified is None
