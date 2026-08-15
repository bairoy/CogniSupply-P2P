# Inbound-to-Pay Platform — Project Context

Cognizant NPN_SCM Hackathon, Combination 2 (E2 + PR2). One integrated
system: yard/dock tracking (E2) connected to autonomous procure-to-pay
(PR2) via the goods-receipt → 3-way-match bridge.

**Read `README.md` first for the full picture.** This file is a quick
reference for rules that must never be silently violated.

## Project structure

```
cognizant/
├── CLAUDE.md            <- this file, read automatically every session
├── README.md
├── docker-compose.yml   <- local Postgres 16 + Redis 7
├── docs/                <- architecture contracts. BOTH frontend and
│                            backend must follow these, not just backend.
│   ├── redis-contract.md
│   ├── api-contract.md
│   ├── DOCK_DECISION_ENGINE.md
│   └── 3WAY_MATCH_POLICY.md
|.  └── actual_usecase.docx
├── backend/
│   ├── schema.sql       <- applied automatically by docker-compose on first boot
│   ├── migrations/      <- deltas for an ALREADY-RUNNING db (./run.sh migrate).
│   │                       schema.sql only runs on a fresh volume.
│   ├── event_bus.py     <- the only sanctioned way any service touches events
│   ├── requirements.txt
│   ├── shared/db.py     <- connection pool, imported by every service
│   ├── shared/auth.py   <- roles, capability matrix, JWT verify. All 3 services.
│   └── services/
│       └── yard_api/main.py   <- built and tested, don't rewrite
└── frontend/
    └── design-reference/     <- exported HTML/design mockups. Visual
                                  and layout SPEC ONLY -- not wired to
                                  real data or APIs. See its own README
                                  before building the real app here.
```

## Locked files — do not modify without explicit discussion

| File | Governs |
|---|---|
| `backend/schema.sql` | Every table/column/relationship. Verified against live Postgres 16, including under real concurrency. |
| `backend/shared/auth.py` | Role vocabulary, the capability matrix, and how tokens are issued and verified. Change the matrix here, never with an inline role check in a handler. |
| `docs/redis-contract.md` | Event stream, field contract, fixed event_type/entity_type vocabulary, consumer groups, idempotency rules. |
| `backend/event_bus.py` | The only sanctioned way any service writes or reads events. |
| `docs/api-contract.md` | Every endpoint, request/response shape, tables touched, events emitted, transaction boundary. |
| `docs/DOCK_DECISION_ENGINE.md` | Dock scheduling: feasibility constraints, the cost model and its weights, the CP-SAT formulation, and the re-plan triggers — tested against real scenarios. |
| `docs/3WAY_MATCH_POLICY.md` | 3-way match tolerance rules — tested against real scenarios. |

`frontend/design-reference/` is **not** a locked contract — it's a
visual/layout spec to rebuild against, not data to trust. See its own
README before touching it.

If a task seems to require changing any of these, **stop and ask** rather
than modifying and continuing. These went through multiple review-and-test
cycles; changing them silently reopens problems that were already solved.

## Non-negotiable rules

- **`goods_receipts` is written ONLY by Yard API's `/trailers/{id}/unload`.** PR2 reads it, never writes it.
- **Dock reassignment never updates a row in place.** Mark the old `dock_assignments` row `REASSIGNED`, insert a new one `ASSIGNED`. This applies to both the manual reassign endpoint and dock-worker's automatic re-plan.
- **Dock assignment is scheduling, not scoring.** `shared/dock_engine.plan_docks()` plans every pending trailer jointly over time (CP-SAT, deterministic). Never add a second, simpler "just pick a free dock" path next to it, and never re-plan a trailer that is `DOCKED` or carries `score_breakdown.source = 'manual_override'` — those are immovable by design.
- **Every domain write commits together with its `event_log` row**, in one transaction. Use `record_event()` (no commit) inside your transaction, commit both together, then `publish_to_redis()` after. See `event_bus.py`'s module docstring for the exact pattern — don't improvise a variant.
- **Status fields are `TEXT`, append-only.** Add new values to the comment list in `schema.sql`; never rename or repurpose an existing one.
- **New event type = add to `redis-contract.md` §3 first, then emit it.** Never invent one inline in code.
- **Consumer idempotency is Postgres-owned** (`processed_events`, `INSERT ... ON CONFLICT DO NOTHING`), not Redis-based. Don't add a Redis-side dedup set.
- **`consume()` filters by `allowed_event_types` before touching Postgres at all.** Don't rely on a handler to internally ignore irrelevant events.
- **No Redis state cache, no message broker beyond Redis itself, no object storage, no PostGIS, no native Postgres enum types.** Deliberate hackathon-scope decisions — don't reintroduce them because they seem "more correct" in the abstract.
- **New field that doesn't fit an existing column** goes in that table's `metadata`/`payload`/`terms` JSONB column — unless it's something you'll query/index constantly, in which case it earns a real column, added to `schema.sql` first.
- **Auth is protected-by-default.** The middleware in `shared/api.py` authenticates every request unless the path is in `shared/auth.PUBLIC_PATHS`/`PUBLIC_PREFIXES`. A new endpoint is therefore authenticated the moment it exists; a new *write* endpoint still needs its own `Depends(require(<capability>))`.
- **Never trust a request body for who is acting.** The acting user comes from the bearer token (`Depends(current_user)`). `requested_by`/`resolved_by` body fields are deprecated and ignored — see api-contract.md §v5.2.
- **Auth emits no events.** There is no auth entry in the locked `redis-contract.md` vocabulary and none is to be invented; logins, signups and role changes go to `audit_logs`.
- **A schema change needs a migration too.** `schema.sql` is only executed by docker-compose on a *fresh* volume, so an already-seeded database never sees an edit to it. Add an idempotent file to `backend/migrations/` and confirm `./run.sh migrate` applies it cleanly.

## Still open — do not invent values, ask first

Dock scoring and 3-way match tolerance are now locked (see `docs/`).
Remaining:

- **NLP requisition parsing contract** — the exact prompt and expected JSON schema for `requisitions.parsed`.
- **Seed/simulator data spec** — volumes, mismatch rate (target ~20-30% intentional mismatches per README §7), timing model.

## Build order (from README §5)

1. Apply `schema.sql` to a fresh Postgres instance, confirm clean.
2. Seed master data: `locations`, `suppliers`, `materials`, `docks`.
3. Simulator scripts (yard/GPS, supplier/invoice).
4. Yard API + dock scoring worker — **Yard API is already built and tested**, see `app/services/yard_api/main.py`. Dock worker is not yet built.
5. Procurement API, invoice mocked as structured JSON first.
6. 3-way match worker, tested against both clean and mismatched seeded pairs.
7. Wire `event_bus.py` across all of the above (already done for Yard API).
8. Dashboard Gateway + frontend last. Two paths, not one: REST for initial load, WebSocket for live deltas — never reconstruct state from the event stream alone.

## Testing discipline

Every claim in the locked files was verified by actually running it
against live Postgres and Redis — not just read and judged plausible.
Hold code to the same bar: prove a new endpoint/worker behaves correctly
with a real request against a real database, not just "this looks right."
