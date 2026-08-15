-- ============================================================
-- v6 — DOCK SCHEDULING TIME WINDOWS (additive only)
-- ============================================================
-- docker-compose mounts schema.sql as an initdb script, which Postgres runs
-- ONLY on an empty data directory. A database that is already up and seeded
-- therefore never sees a schema.sql edit. This file applies the v6 delta to
-- such a database, and is written so that running it produces a schema
-- identical to a fresh build of schema.sql.
--
--   ./run.sh migrate          (or)
--   docker compose exec -T postgres psql -U postgres -d inbound_test \
--       < backend/migrations/v6_dock_scheduling.sql
--
-- Idempotent: every statement is IF NOT EXISTS. Safe to run repeatedly, and
-- safe against a fresh database that already has v6 from schema.sql.
-- Nothing is dropped, renamed, or retyped.
--
-- What it enables: before v6 a dock_assignments row recorded WHICH door but
-- never WHEN, so the decision engine could only ask "is this door occupied at
-- this instant". With a planned window it can ask the question the yard
-- actually cares about — "is this door free during that truck's service
-- window" — which is what makes ETA, waiting time and future conflicts real
-- inputs to the assignment rather than commentary on it.
-- ============================================================

BEGIN;

ALTER TABLE dock_assignments ADD COLUMN IF NOT EXISTS planned_start TIMESTAMPTZ;
ALTER TABLE dock_assignments ADD COLUMN IF NOT EXISTS planned_end   TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_dock_assignments_window
    ON dock_assignments(dock_id, planned_start, planned_end)
    WHERE status IN ('ASSIGNED', 'CONFIRMED');

-- Backfill the live assignments only. History (REASSIGNED/COMPLETED) is left
-- with NULL windows on purpose: inventing a plausible-looking planned window
-- for a row that was decided by the pre-v6 engine would be fabricating a
-- decision that never happened, and those rows are never conflict candidates
-- anyway. Live rows must be backfilled, because a NULL window would make an
-- occupied door invisible to the scheduler's overlap check.
--
--   planned_start: when the door is/was expected to be taken — docked_at if
--                  the trailer is already at the door, otherwise the trailer's
--                  ETA, otherwise when the assignment was made.
--   planned_end:   planned_start + that dock's expected_unload_minutes
--                  (docks.metadata, v4), defaulting to 45 minutes.
WITH planned AS (
    SELECT da.id,
           COALESCE(da.docked_at, t.eta, da.assigned_at) AS start_at,
           COALESCE((d.metadata->>'expected_unload_minutes')::numeric, 45)::int AS service_minutes
    FROM dock_assignments da
    JOIN docks d          ON d.id = da.dock_id
    LEFT JOIN trailers t  ON t.id = da.trailer_id
    WHERE da.status IN ('ASSIGNED', 'CONFIRMED')
      AND da.planned_start IS NULL
)
UPDATE dock_assignments da
SET planned_start = planned.start_at,
    planned_end   = planned.start_at + make_interval(mins => planned.service_minutes)
FROM planned
WHERE planned.id = da.id;

COMMIT;
