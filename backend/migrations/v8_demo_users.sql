-- ============================================================
-- v8 — DEMO USER ROSTER (data only, no schema change)
-- ============================================================
-- docker-compose mounts schema.sql as an initdb script, which Postgres runs
-- ONLY on an empty data directory, and `seed.py --reset` is the only thing that
-- rebuilds the users table from scratch. An already-seeded database therefore
-- keeps whatever roster it was seeded with, however many times seed.py's USERS
-- list is edited afterwards. This file carries the v8 roster to such a database
-- WITHOUT discarding its traffic.
--
--   ./run.sh migrate          (or)
--   docker compose exec -T postgres psql -U postgres -d inbound_test \
--       < backend/migrations/v8_demo_users.sql
--
-- What changed: the roster is now one account per login role -- Baiju (admin),
-- Shubham (operator), Sachin (procurement), Serohn (finance) -- replacing the
-- six-name set that carried two operators and two procurement users. A second
-- account in the same role demonstrated nothing the first one did not, and the
-- login screen no longer advertises the roster at all, so the roster's only job
-- now is to be four real identities behind the capability matrix.
--
-- Idempotent: every statement is keyed by id and states the final value, so
-- re-running changes nothing. Safe on a fresh database (the surplus ids simply
-- do not exist) and safe before seeding (the table is empty, and `./run.sh
-- migrate` runs `seed.py --master-only` immediately after this file, which
-- inserts the roster with its password hashes).
--
-- USR-000, the service account that payments.approved_by points at, is not
-- touched. It is not an identity anyone signs in as -- see shared/auth.py.
-- ============================================================

BEGIN;

-- ── Rename the four surviving accounts ──────────────────────
-- Ids are reused rather than reissued: requisitions.requested_by,
-- exceptions.assigned_to, payments.approved_by and audit_logs.user_id all point
-- here, and rewriting history to a new set of ids would be a bigger lie than
-- the rename. USR-003 stays procurement and USR-002 stays operator, so the two
-- roles that own the most seeded rows keep their exact meaning.
UPDATE users SET name = 'Baiju',   email = 'baiju@cognisupply.in',   role = 'admin'
 WHERE id = 'USR-001';
UPDATE users SET name = 'Shubham', email = 'shubham@cognisupply.in', role = 'operator'
 WHERE id = 'USR-002';
UPDATE users SET name = 'Sachin',  email = 'sachin@cognisupply.in',  role = 'procurement'
 WHERE id = 'USR-003';
UPDATE users SET name = 'Serohn',  email = 'serohn@cognisupply.in',  role = 'finance'
 WHERE id = 'USR-004';

-- ── Repoint the two removed accounts, then remove them ──────
-- Every reference is moved to the account that now holds the role the old row
-- was acting in, so no audit trail loses its actor and no FK is orphaned.
--   USR-005 (was finance)   -> USR-004, the finance account
--   USR-006 (was admin)     -> USR-001, the admin account
-- USR-004 changed role from procurement to finance in the block above, so any
-- requisition it raised as a buyer moves to USR-003 first: a finance user
-- standing in the requested_by column would misreport who can raise demand.
UPDATE requisitions SET requested_by = 'USR-003'
 WHERE requested_by IN ('USR-004', 'USR-005', 'USR-006');

UPDATE exceptions SET assigned_to = 'USR-004' WHERE assigned_to = 'USR-005';
UPDATE exceptions SET assigned_to = 'USR-001' WHERE assigned_to = 'USR-006';

UPDATE payments  SET approved_by = 'USR-004' WHERE approved_by = 'USR-005';
UPDATE payments  SET approved_by = 'USR-001' WHERE approved_by = 'USR-006';

UPDATE audit_logs SET user_id = 'USR-004' WHERE user_id = 'USR-005';
UPDATE audit_logs SET user_id = 'USR-001' WHERE user_id = 'USR-006';

DELETE FROM users WHERE id IN ('USR-005', 'USR-006');

COMMIT;
