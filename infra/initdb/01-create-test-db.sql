-- Runs once on first Postgres init (when carbuyer-pg-data volume is empty).
-- Creates the test databases used by pytest fixtures.
CREATE DATABASE carbuyer_test OWNER carbuyer;
-- Separate DB for alembic upgrade/downgrade tests (needs its own schema
-- so alembic commands don't interfere with the main test DB).
CREATE DATABASE carbuyer_migration_test OWNER carbuyer;
