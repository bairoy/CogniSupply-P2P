-- ============================================================
-- v8 — AI NARRATION ON 3-WAY MATCH RESULTS (additive only)
-- ============================================================
-- docker-compose mounts schema.sql as an initdb script, which Postgres runs
-- ONLY on an empty data directory. A database that is already up and seeded
-- therefore never sees a schema.sql edit. This file applies the v8 delta to
-- such a database.
--
--   ./run.sh migrate          (or)
--   docker compose exec -T postgres psql -U postgres -d inbound_test \
--       < backend/migrations/v8_ai_narration.sql
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. Safe to run repeatedly. Nothing is
-- dropped, renamed, or retyped, and no existing row is rewritten.
--
-- What it enables: match_results already records WHAT the deterministic policy
-- concluded (`status`) and the one-line arithmetic behind it (`reason`, e.g.
-- "invoiced qty 480 vs received 500, -4.0% outside 2% tolerance"). That is
-- correct and auditable but reads as machine output. `ai_narration` holds the
-- same verdict written up as prose an AP clerk can paste into an audit log.
--
-- IT IS A SECOND RENDERING OF THE DECISION, NEVER A SECOND DECISION.
-- `status` and `reason` remain the record of what happened and are what every
-- downstream consumer keys on. Nothing reads `ai_narration` to determine an
-- outcome, and nothing may start: shared/match_policy.py imports no model, and
-- the narration is written after the verdict is already fixed. If this column
-- is empty, or disagrees with `reason`, `reason` wins — see
-- docs/3WAY_MATCH_POLICY.md.
--
-- NULLable with no default and no backfill. Rows matched before v8 have no
-- narration and get none: inventing prose for a historical decision the model
-- never saw would put words in the auditor's mouth. The UI renders the
-- deterministic `reason` alone for those, exactly as it did before v8.
-- ============================================================

ALTER TABLE match_results ADD COLUMN IF NOT EXISTS ai_narration TEXT;

COMMENT ON COLUMN match_results.ai_narration IS
  'Prose rendering of an ALREADY-DECIDED match, from shared/llm.write_match_reasoning(). '
  'Never an input to any decision; status/reason are authoritative. '
  'NULL for rows matched before v8 and whenever the LLM call was not attempted.';
