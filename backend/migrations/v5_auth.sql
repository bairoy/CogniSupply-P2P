-- ============================================================
-- v5 — AUTHENTICATION (additive only)
-- ============================================================
-- docker-compose mounts schema.sql as an initdb script, which Postgres runs
-- ONLY on an empty data directory. A database that is already up and seeded
-- therefore never sees a schema.sql edit. This file applies the v5 delta to
-- such a database, and is written so that running it produces a schema
-- identical to a fresh build of schema.sql.
--
--   ./run.sh migrate          (or)
--   docker compose exec -T postgres psql -U postgres -d inbound_test \
--       < backend/migrations/v5_auth.sql
--
-- Idempotent: every statement is IF NOT EXISTS. Safe to run repeatedly, and
-- safe to run against a fresh database that already has v5 from schema.sql.
-- No data is modified, nothing is dropped, no column is retyped.
-- ============================================================

BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS email          TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash  TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active      BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at  TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower
    ON users(lower(email)) WHERE email IS NOT NULL;

CREATE SEQUENCE IF NOT EXISTS user_id_seq START 100;

-- Existing rows keep email/password_hash NULL, which shared/auth.py treats as
-- "no login configured" -- they cannot authenticate until seed.py gives them
-- credentials (./run.sh seed-logins). That is deliberate: a migration must
-- never invent a password, and NULL is not a hash anything can match.

COMMIT;
