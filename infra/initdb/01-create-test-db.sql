-- Runs once on first Postgres init (when carbuyer-pg-data volume is empty).
-- Creates the test database used by pytest fixtures.
CREATE DATABASE carbuyer_test OWNER carbuyer;
