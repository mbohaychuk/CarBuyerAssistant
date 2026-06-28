"""Acceptance gate for the vehicle_offer split migration (1d6201a6e2d0).

The test suite builds its schema with ``create_all`` and never runs migrations,
so the migration's data-preservation is otherwise unverified. This script spins
up a throwaway ``carbuyer_migcheck`` database, brings it to the pre-split
revision, seeds representative rows (auction + lots + bid history + want match +
purchase), upgrades through the split, asserts every id / FK / value survived,
then round-trips the downgrade and asserts the monolith is restored intact.

Run:  uv run python scripts/verify_offer_split_migration.py
Exit code 0 = PASS. It is non-destructive to the dev/test databases.
"""
# ruff: noqa: PLR2004 -- expected counts/seeded values are inherent to a migration gate
from __future__ import annotations

import sys

import psycopg
from alembic.config import Config

from alembic import command
from carbuyer.db.notify import to_psycopg_url
from carbuyer.shared import config as _config

SCRATCH_DB = "carbuyer_migcheck"
PRE_SPLIT = "403d74523f36"
HEAD = "1d6201a6e2d0"


def _swap_db(url: str, db: str) -> str:
    head, _, _old = url.rpartition("/")
    return f"{head}/{db}"


_ORIGINAL_URL = _config.settings.database_url
_SCRATCH_URL = _swap_db(_ORIGINAL_URL, SCRATCH_DB)


def _admin_conn() -> psycopg.Connection:
    return psycopg.connect(to_psycopg_url(_swap_db(_ORIGINAL_URL, "postgres")), autocommit=True)


def _scratch_conn() -> psycopg.Connection:
    return psycopg.connect(to_psycopg_url(_SCRATCH_URL), autocommit=True)


def _recreate_scratch() -> None:
    with _admin_conn() as c, c.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {SCRATCH_DB}")


def _drop_scratch() -> None:
    with _admin_conn() as c, c.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")


def _upgrade(rev: str) -> None:
    # env.py reads settings.database_url at run time — point it at the scratch DB.
    _config.settings.database_url = _SCRATCH_URL
    command.upgrade(Config("alembic.ini"), rev)


def _downgrade(rev: str) -> None:
    _config.settings.database_url = _SCRATCH_URL
    command.downgrade(Config("alembic.ini"), rev)


def _scalar(cur: psycopg.Cursor, sql: str) -> object:
    cur.execute(sql)
    row = cur.fetchone()
    return row[0] if row else None


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _seed() -> tuple[int, int]:
    """Seed the pre-split monolith; return (lot_id_a, lot_id_b)."""
    with _scratch_conn() as c, c.cursor() as cur:
        auction_id = _scalar(cur, (
            "INSERT INTO auctions (source, source_auction_id, url, canonical_url, "
            "first_seen_at, last_seen_at) VALUES "
            "('test','A1','http://x/a','http://x/a', now(), now()) RETURNING id"
        ))
        lot_a = _scalar(cur, (
            "INSERT INTO auction_lots (auction_id, source_lot_id, url, make, model, "
            "year, current_high_bid_cad, valuation_status) VALUES "
            f"({auction_id}, 'L1', 'http://x/l1', 'Nissan', 'Xterra', 2010, 8000.00, 'done') "
            "RETURNING id"
        ))
        lot_b = _scalar(cur, (
            "INSERT INTO auction_lots (auction_id, source_lot_id, url, make, model, "
            "year, current_high_bid_cad) VALUES "
            f"({auction_id}, 'L2', 'http://x/l2', 'Lexus', 'GX 470', 2005, 15000.00) "
            "RETURNING id"
        ))
        search_id = _scalar(cur, (
            "INSERT INTO searches (name, config) VALUES ('w', '{}'::jsonb) RETURNING id"
        ))
        cur.execute(
            "INSERT INTO want_matches (search_id, lot_id) VALUES (%s, %s)",
            (search_id, lot_a),
        )
        cur.execute(
            "INSERT INTO auction_bid_history (lot_id, observed_at, current_high_bid_cad) "
            "VALUES (%s, now(), 7500.00)",
            (lot_a,),
        )
        cur.execute(
            "INSERT INTO purchases (purchase_date, make, model, year, purchase_price_cad, "
            "linked_lot_id) VALUES (current_date, 'Nissan', 'Xterra', 2010, 8200.00, %s)",
            (lot_b,),
        )
        return int(lot_a), int(lot_b)  # type: ignore[arg-type]


def _assert_post_split(lot_a: int, lot_b: int) -> None:
    with _scratch_conn() as c, c.cursor() as cur:
        _check(_scalar(cur, "SELECT count(*) FROM vehicle_offer") == 2,
               "vehicle_offer has 2 rows")
        _check(_scalar(cur, "SELECT count(*) FROM auction_lot") == 2,
               "auction_lot has 2 rows")
        _check(_scalar(cur, "SELECT count(*) FROM private_listing") == 0,
               "private_listing is empty")
        _check(_scalar(cur,
               "SELECT count(*) FROM auction_lot al JOIN vehicle_offer vo ON vo.id = al.id")
               == 2, "every auction_lot id has a vehicle_offer parent")
        _check(_scalar(cur, "SELECT count(*) FROM vehicle_offer WHERE offer_kind <> 'auction'")
               == 0, "all offers backfilled to offer_kind='auction'")
        # Column placement preserved values.
        _check(_scalar(cur, f"SELECT make FROM vehicle_offer WHERE id = {lot_a}") == "Nissan",
               "parent column (make) preserved")
        _check(_scalar(cur,
               f"SELECT current_high_bid_cad FROM auction_lot WHERE id = {lot_a}") == 8000,
               "child column (current_high_bid_cad) preserved")
        _check(_scalar(cur, f"SELECT valuation_status FROM vehicle_offer WHERE id = {lot_a}")
               == "done", "parent status column preserved (not reset to pending)")
        # FK values resolve to the right tables.
        _check(_scalar(cur,
               "SELECT count(*) FROM want_matches wm JOIN vehicle_offer vo ON vo.id = wm.lot_id")
               == 1, "want_matches.lot_id resolves to vehicle_offer")
        _check(_scalar(cur,
               "SELECT count(*) FROM auction_bid_history b JOIN auction_lot al ON al.id = b.lot_id")
               == 1, "auction_bid_history.lot_id resolves to auction_lot")
        _check(_scalar(cur,
               "SELECT count(*) FROM purchases p JOIN vehicle_offer vo ON vo.id = p.linked_lot_id")
               == 1, "purchases.linked_lot_id resolves to vehicle_offer")


def _seed_private_post_split() -> None:
    """Insert a private offer (parent + child) AFTER the split so the downgrade
    has to handle a non-auction parent it can't represent (it must drop it)."""
    with _scratch_conn() as c, c.cursor() as cur:
        oid = _scalar(cur, (
            "INSERT INTO vehicle_offer (offer_kind, url, make, model, year, valuation_status) "
            "VALUES ('private', 'http://k/1', 'Lexus', 'GX 470', 2005, 'done') RETURNING id"
        ))
        cur.execute(
            "INSERT INTO private_listing (id, asking_price_cad, listing_status) "
            "VALUES (%s, 15000, 'active')",
            (oid,),
        )


def _assert_private_present() -> None:
    with _scratch_conn() as c, c.cursor() as cur:
        _check(_scalar(cur, "SELECT count(*) FROM private_listing") == 1,
               "private listing present post-split")
        _check(_scalar(cur, "SELECT count(*) FROM vehicle_offer WHERE offer_kind='private'")
               == 1, "private parent has offer_kind='private'")


def _assert_post_downgrade(lot_a: int) -> None:
    with _scratch_conn() as c, c.cursor() as cur:
        _check(_scalar(cur, "SELECT to_regclass('public.auction_lots') IS NOT NULL"),
               "auction_lots monolith restored")
        _check(_scalar(cur, "SELECT to_regclass('public.auction_lot') IS NULL"),
               "auction_lot child dropped")
        _check(_scalar(cur, "SELECT to_regclass('public.private_listing') IS NULL"),
               "private_listing dropped")
        # The private offer can't be represented by the monolith → downgrade drops it.
        _check(_scalar(cur, "SELECT count(*) FROM auction_lots") == 2,
               "only the 2 auction rows remain (private offer dropped)")
        _check(_scalar(cur, f"SELECT make FROM auction_lots WHERE id = {lot_a}") == "Nissan",
               "make intact after round-trip")
        _check(_scalar(cur,
               f"SELECT current_high_bid_cad FROM auction_lots WHERE id = {lot_a}") == 8000,
               "current_high_bid_cad intact after round-trip")
        _check(_scalar(cur, f"SELECT valuation_status FROM auction_lots WHERE id = {lot_a}")
               == "done", "valuation_status survived round-trip")
        _check(_scalar(cur,
               "SELECT count(*) FROM auction_bid_history b JOIN auction_lots l ON l.id = b.lot_id")
               == 1, "bid_history FK intact after round-trip")
        _check(_scalar(cur,
               "SELECT count(*) FROM want_matches wm JOIN auction_lots l ON l.id = wm.lot_id")
               == 1, "want_matches.lot_id resolves after round-trip")
        _check(_scalar(cur,
               "SELECT count(*) FROM purchases p JOIN auction_lots l ON l.id = p.linked_lot_id")
               == 1, "purchases.linked_lot_id resolves after round-trip")


def main() -> int:
    try:
        print("recreating scratch DB ...")
        _recreate_scratch()
        print(f"upgrading to pre-split revision {PRE_SPLIT} ...")
        _upgrade(PRE_SPLIT)
        print("seeding pre-split rows ...")
        lot_a, lot_b = _seed()
        print(f"upgrading through the split to {HEAD} ...")
        _upgrade(HEAD)
        print("asserting post-split invariants ...")
        _assert_post_split(lot_a, lot_b)
        print("seeding a private offer post-split ...")
        _seed_private_post_split()
        _assert_private_present()
        print(f"downgrading back to {PRE_SPLIT} ...")
        _downgrade(PRE_SPLIT)
        print("asserting post-downgrade round-trip ...")
        _assert_post_downgrade(lot_a)
        print("\nPASS — migration preserves ids, FKs, and column values both ways.")
        return 0
    except Exception as exc:
        print(f"\nFAIL — {type(exc).__name__}: {exc}")
        return 1
    finally:
        _config.settings.database_url = _ORIGINAL_URL
        _drop_scratch()


if __name__ == "__main__":
    sys.exit(main())
