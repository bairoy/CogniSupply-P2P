# Inbound-to-Pay Platform — Project Context

Cognizant NPN_SCM Hackathon, Combination 2 (E2 + PR2). One integrated
system: yard/dock tracking (E2) connected to autonomous procure-to-pay
(PR2) via the goods-receipt → 3-way-match bridge.

As of v7 the whole chain runs itself: a typed sentence becomes a
requisition, a scored supplier, a PO, a supplier confirmation, a shipment
and a rolling truck with no human in the happy path (`supplier_agent`),
and E2 handles **both** directions — inbound receiving and outbound
fulfilment — over one set of dock doors.

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
│   ├── shared/auth.py   <- roles, capability matrix, JWT verify. All services.
│   └── services/
│       ├── yard_api/main.py       <- inbound yard. Built and tested, don't rewrite
│       ├── yard_api/outbound.py   <- v7 outbound, mounted into the SAME app
│       ├── procurement_api/main.py
│       ├── dock_worker/main.py    <- plans BOTH directions in one CP-SAT solve
│       ├── match_worker/main.py
│       ├── supplier_agent/main.py <- v7: PO_CREATED -> confirm -> shipment+trailer
│       ├── simulator/main.py      <- v7: drives the real APIs; owns no tables
│       └── dashboard_gateway/main.py
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
- **`goods_issues` is written ONLY by Yard API's `/trailers/{id}/load`** (v7) — the exact mirror. It has no downstream matcher and never gets one: nobody invoices us for goods we shipped out.
- **Outbound reuses `trailers`, `tracking_events` and `dock_assignments`** via a `direction` column — it does NOT get parallel tables. A door is one resource contended for by both directions at the same instant, so both must be planned by the one `plan_docks()` call. Never add an outbound scheduler, an outbound tracking endpoint, or an outbound dock endpoint; outbound trucks use the inbound ones unchanged.
- **The optimiser is direction-blind.** `TrailerRequest.direction` is descriptive only and must never enter the cost model. If outbound loads deserve to be served sooner, that goes in `priority`, which already drives the wait weight.
- **A door is never committed to a load that isn't picked.** `/outbound-orders/{id}/dispatch` refuses anything not `STAGED`. This is outbound's one ordering rule and inbound has no equivalent — do not "simplify" it away.
- **`supplier-agent` and `simulator` drive HTTP, never tables.** Both hold a database connection and could INSERT directly; neither may. They call the same public endpoints an operator would, so automation cannot drift from the contract the system is tested against. The one sanctioned exception is the simulator's `block-dock`, which flips `docks.is_active` — infrastructure failing is the world changing, not a user acting.
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

Dock scoring, 3-way match tolerance, the NLP parsing contract
(`BUILD_PLAN.md` §4.1) and the seed/simulator spec (§5) are all locked now.
Remaining:

- **Eval harness** (`BUILD_PLAN.md` §5.3) — `backend/eval/run_eval.py` is specified and not yet built. `GET /kpi/model-performance` returns 404 until it has run, which is deliberate: an honest "not measured yet" beats a fabricated number.
- **Docker packaging of the app services** (`BUILD_PLAN.md` §7) — `docker-compose.yml` still starts Postgres and Redis only; `./run.sh start` runs the six app processes locally.

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
