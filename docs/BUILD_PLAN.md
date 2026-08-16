# Build Plan — CogniSupply P2P Working Prototype

Cognizant NPN_SCM Hackathon, Combination 2 (E2 + PR2).

This document is the bridge between the locked contracts (`schema.sql`,
`redis-contract.md`, `api-contract.md`, `DOCK_DECISION_ENGINE.md`,
`3WAY_MATCH_POLICY.md`) and a running system. It records the decisions taken
on 2026-08-14, every contract change those decisions require, and the phased
build order.

**Nothing in `§2` is applied yet.** Review the diff in `§2`, then Phase 0
applies it to the locked files. Everything after Phase 0 codes against the
amended contracts.

---

## 1. Decisions taken

| # | Decision | Chosen |
|---|---|---|
| 1 | Locked-file policy | **`schema.sql` is open.** Additive columns only — no renames, no drops, no type changes. Every addition is listed in §2.1 and applied in one pass. |
| 2 | AI depth | **Real Claude API for NLP and OCR.** Conversational requisition intake, vision-based OCR over generated invoice images, LLM-written supplier reasoning. The 3-way match decision stays 100% deterministic per `3WAY_MATCH_POLICY.md` — AI never decides pay/don't-pay. |
| 3 | Judging extras | **All three**: eval harness with ground truth, Dockerized stack + CI, architecture docs + roadmap. |
| 4 | Demo shape | **Working prototype**: continuous background simulator keeps the yard live, plus one-click scenario triggers so any demo moment can be forced during Q&A. |

### 1.1 Non-negotiables that survive unchanged

These are *not* reopened by decision 1:

- `goods_receipts` is written **only** by Yard API `/trailers/{id}/unload`.
- Dock reassignment never updates a row in place (old → `REASSIGNED`, new → `ASSIGNED`).
- Every domain write commits with its `event_log` row in one transaction, via `record_event()` → commit → `publish_to_redis()`.
- Status fields are `TEXT`, append-only.
- New event type → `redis-contract.md` §3 first, then emit.
- Consumer idempotency is Postgres-owned (`processed_events`).
- `consume()` filters by `allowed_event_types` before touching Postgres.
- No Redis state cache, no broker beyond Redis, no object storage, no PostGIS, no native enums.
- The pay/don't-pay decision is deterministic and auditable.

---

## 2. Contract change set (Phase 0)

### 2.1 `backend/schema.sql` — additive only

Nine additions. Every one is a new column or a new table; no existing line changes.

```sql
-- Dock decision explainability. The Yard & Dock screen renders the weighted
-- score breakdown; `reason` (TEXT) can hold the sentence but not the numbers.
ALTER TABLE dock_assignments ADD COLUMN score_breakdown JSONB DEFAULT '{}';
--   {"hard_constraints":{"active":true,"load_type_ok":true,"unoccupied":true},
--    "priority_score":0.50,"specialization_score":0.30,
--    "position_penalty":-0.08,"final_score":0.72,
--    "candidates":[{"dock_id":"DOCK-02","final_score":0.61}, ...]}

-- Dock occupancy timing. Drives the "Unloading 45%" progress bar without a
-- stored percentage: progress = elapsed since docked_at / expected_unload_minutes.
ALTER TABLE dock_assignments ADD COLUMN docked_at TIMESTAMPTZ;
ALTER TABLE docks ADD COLUMN metadata JSONB DEFAULT '{}';
--   docks.metadata carries {"expected_unload_minutes": 45}

-- Supplier recommendation card: price, lead time, and the AI's written
-- rationale. The five score columns already exist; these are what the card
-- shows above them.
ALTER TABLE supplier_recommendations ADD COLUMN quoted_unit_price NUMERIC;
ALTER TABLE supplier_recommendations ADD COLUMN quoted_lead_time_days NUMERIC;
ALTER TABLE supplier_recommendations ADD COLUMN reasoning TEXT;

-- Exception queue columns. Sorted and filtered on every render, so they earn
-- real columns rather than a JSONB blob.
ALTER TABLE exceptions ADD COLUMN severity TEXT DEFAULT 'medium';  -- low | medium | high | critical
ALTER TABLE exceptions ADD COLUMN impact_amount NUMERIC;           -- |invoice total - PO total|

-- "View Original Scan" on the Match & Pay screen. A path to a file on the
-- local invoice volume, NOT object storage — the no-S3 decision stands.
ALTER TABLE invoices ADD COLUMN document_path TEXT;

CREATE INDEX idx_exceptions_severity ON exceptions(severity, created_at DESC);
CREATE INDEX idx_dock_assignments_dock_status ON dock_assignments(dock_id, status);
```

**Deliberately NOT added**, with reasons:

| Rejected | Why |
|---|---|
| `trailers.unload_progress_pct` | Derivable from `docked_at` + `docks.metadata.expected_unload_minutes`. A stored percentage needs a writer ticking it. |
| `purchase_orders.line_items` | The schema is header-level by design (one material, one qty, one price). The Match & Pay table renders as a single line. Multi-line POs are a Tier-2 change, not a hackathon one. |
| `kpi_snapshots` table | KPIs are computed live from the tables that already exist. A snapshot table is a caching decision we don't need at this data volume. |
| `alerts.severity` | Already exists. |
| Any `ENUM` type | Locked design principle. |

### 2.2 `docs/redis-contract.md`

**§3 — five new `event_type` values (appended, nothing renamed):**

```
TRAILER_DOCKED        -- trailer moves ARRIVED -> DOCKED; dock_assignment -> CONFIRMED
PO_STATUS_CHANGED     -- purchase_orders.status advanced (SHIPPED/RECEIVED/MATCHED/CLOSED)
ALERT_ACKNOWLEDGED    -- operator dismissed an alert
EXCEPTION_ASSIGNED    -- exception routed to a user
PAYMENT_PAID          -- payment moved APPROVED -> PAID
```

**§4 — `entity_type` vocabulary unchanged.** All five map to existing entity types (`trailer`, `purchase_order`, `alert`, `exception`, `payment`).

**§5 — one consumer-group change:**

```
dock-worker allowed_event_types gains "GOODS_RECEIVED"
```

This closes the limitation `DOCK_DECISION_ENGINE.md` flags honestly at the
bottom of the file: nothing currently re-scores a *waiting* trailer when a
*different* trailer's dock frees up. `GOODS_RECEIVED` is exactly the
dock-release signal. The doc says this "would need a deliberate addition, not
a silent one" — this is that deliberate addition, recorded before any code.
Handler: on `GOODS_RECEIVED`, re-run Stage 1 + Stage 2 for every trailer with
no current `ASSIGNED`/`CONFIRMED` row, oldest ETA first.

**New §2a — payload enrichment convention:**

> Every event payload carries, in addition to its event-specific fields:
> `summary` (a one-line human-readable string, e.g. `"TRL-3391 assigned to DOCK-04"`)
> and the display fields the dashboard needs to apply the event as a delta
> without refetching. The envelope in §2 is unchanged; §6's "no locked payload
> schema per event type" still holds — this is a floor, not a schema.

This is what makes the live event rail render real text and the WebSocket a
genuine delta channel. Today most events publish `payload = {}`, and even
`SHIPMENT_CREATED` omits `po_id`.

### 2.3 `docs/api-contract.md` — 14 new endpoints, 2 amended

**Yard API (E2)**

| Endpoint | Purpose |
|---|---|
| `POST /trailers/{id}/dock` | **New.** `trailers.status='DOCKED'`, current `dock_assignments` row → `CONFIRMED`, `docked_at=now()`. Emits `TRAILER_DOCKED`. Nothing sets these today, so the yard board's Docked/Unloading states are unreachable. |
| `GET /yard-status` | **Amended response** (additive fields only): each trailer gains `carrier`, `load_type`, `priority`, `po_id`, `latitude`, `longitude`, `unload_progress_pct`; each dock gains `compatible_load_types`, `is_active`, `current_trailer_id`, `assignment_reason`. |
| `GET /trailers/{id}` | **Amended response:** each `dock_assignment_history` entry gains `score_breakdown`. |

**Procurement API (PR2)**

| Endpoint | Purpose |
|---|---|
| `POST /requisitions/chat` | **New.** Conversational NLP intake. Body `{session_id, message, history[]}`. Returns either `{status:"clarifying", question}` or `{status:"parsed", parsed{...}}`. Only on `parsed` does it write a `requisitions` row + `REQUISITION_CREATED`. This is the brief's "conversational NLP chatbot"; `POST /requisitions` stays as the single-shot path. |
| `GET /requisitions/{id}` | **New.** Read back raw text + parsed JSONB + recommendations. |
| `POST /invoices` | **Amended:** now accepts `multipart/form-data` with an image file (real OCR path) *in addition to* the existing structured-JSON body. Same writes, same events, same transaction. Image is saved to the invoice volume and its path stored in `invoices.document_path`. |
| `GET /invoices/{id}` | **New.** Invoice detail incl. `ocr_raw` per-field confidences. |
| `GET /invoices/{id}/document` | **New.** Streams the stored invoice image for "View Original Scan". |
| `POST /exceptions/{id}/assign` | **New.** `{assigned_to}` → `exceptions.assigned_to`, emits `EXCEPTION_ASSIGNED`. |
| `GET /payments` | **New.** Payment list/filter; the Payment Status timeline needs it. |
| `POST /payments/{id}/pay` | **New.** `APPROVED → PAID`, sets `paid_at`, emits `PAYMENT_PAID`. Closes the loop the schema already models but nothing drives. |

**Dashboard Gateway**

| Endpoint | Purpose |
|---|---|
| `GET /search?q=` | **New.** Global Cmd+K and the brief's E2 requirement #1 — accepts a tracking number, trailer ID, shipment reference, PO, invoice, or exception ID and resolves to `{entity_type, entity_id, url}`. |
| `GET /track/{ref}` | **New.** Customer-facing tracker: resolves any of the above to trailer status, live position, ETA, and delivery progress. No auth. |
| `GET /dashboard/pipeline` | **New.** Funnel counts per stage (Requisition → Sourcing → PO → Transit → Receiving → Match → Payment) plus delayed/exception counts. |
| `GET /dashboard/at-risk` | **New.** Union of open exceptions, unacknowledged delay alerts, and requisitions stalled > N hours — the Control Tower's At-Risk table. |
| `GET /exceptions/queue` | **New.** The Exceptions Command Center feed: `exceptions` **unioned with** `alerts`, each row carrying severity, age, impact, owner. Two sources, one queue — the design shows "Dock Delay" next to "Price Mismatch", and those live in different tables. Read-only union in the gateway; neither table changes. |
| `GET /traceability/{po_id}` | **New.** Full cross-entity timeline for a PO — gathers `event_log` rows for the PO and every shipment, trailer, goods receipt, invoice, match result, exception, and payment attached to it. The "Traceability" nav item and the root-cause chain both need this; no existing endpoint spans entities. |
| `POST /alerts/{id}/acknowledge` | **New.** Flips `alerts.acknowledged`, emits `ALERT_ACKNOWLEDGED`. The column exists and nothing writes it. |
| `GET /map/trailers` | **New.** Live positions + recent breadcrumb trail per active trailer, for the map (Mapbox GL JS as of v8). |
| `GET /kpi/model-performance` | **New.** Serves the latest eval run (precision/recall/F1). See §5. |

**Match Worker — allowed set gains `SHIPMENT_CREATED`**

`purchase_orders.status` never advances past `CREATED` today, so the pipeline
funnel can't be computed. `purchase_orders` is PR2-owned, and Yard API must not
write it — so match-worker becomes the PR2-side status reconciler:

| Event | PO status transition |
|---|---|
| `SHIPMENT_CREATED` | `CREATED → SHIPPED` |
| `GOODS_RECEIVED` | `SHIPPED → RECEIVED` (or `PARTIALLY_RECEIVED` if `qty_received < po.qty`) |
| match approved | `RECEIVED → MATCHED` (already specified) |
| payment paid | `MATCHED → CLOSED` |

Each emits `PO_STATUS_CHANGED`. Ownership rule intact: PR2 writes PR2 tables.

Similarly `shipments.status` (`CREATED → EN_ROUTE → ARRIVED → UNLOADED`) is
Yard-owned and advances inside the existing trailer endpoints — no new
endpoint, just writes that were specified and never implemented.

---

## 3. Defects to fix in already-built code

Found while reading `backend/`:

| # | File | Issue | Fix |
|---|---|---|---|
| 1 | `shared/db.py` | `SimpleConnectionPool` is not thread-safe. FastAPI runs `def` endpoints in a threadpool, so concurrent requests share the pool unsafely. | `ThreadedConnectionPool` |
| 2 | all services | No CORS middleware. A browser frontend on `:5173` is blocked outright. | `CORSMiddleware` on every FastAPI app |
| 3 | `yard_api/main.py` | Event payloads are `{}` — `SHIPMENT_CREATED` doesn't even carry `po_id`. | Apply the §2.2 payload convention |
| 4 | all services | No `/health` endpoint. | Add; compose healthchecks depend on it |
| 5 | `requirements.txt` | Missing `anthropic`, `httpx`, `pytest`, `pytest-asyncio`, `websockets`, `python-dotenv`, `Pillow`, `Faker` | Add |
| 6 | `docker-compose.yml` | Starts Postgres + Redis only; no application services | Add all five services + frontend |
| 7 | `yard_api/main.py` | `arrive()` doesn't validate the current status before transitioning | Guard: only `EN_ROUTE → ARRIVED` |

---

## 4. AI layer

One shared module, `backend/shared/llm.py`, wrapping **either** the Anthropic
or the OpenAI SDK. Every AI call goes through it, and no service outside it
knows which provider ran.

- **Provider selection is environment-only.** `LLM_PROVIDER=anthropic|openai`
  forces one; unset auto-picks Anthropic if `ANTHROPIC_API_KEY` is set, else
  OpenAI if `OPENAI_API_KEY` is set. The client is built once, lazily.
- **Anthropic model: `claude-opus-5`** (override: `ANTHROPIC_MODEL`) for all
  four tasks (v8 added match narration). Thinking is on by default on this model (adaptive) — do not pass
  `budget_tokens`, and do not pass `temperature`/`top_p`/`top_k`; both are
  rejected. `max_tokens` caps thinking *plus* response, so size it with
  headroom (4096 for parse/OCR calls).
- **OpenAI model: `gpt-5.4-mini`** (override: `OPENAI_MODEL`). Measured live on
  all four tasks: ~1.5s vs 10–18s for `gpt-5`, same extracted fields. Nothing
  here decides anything, and intake is a request an operator waits on. Same
  no-sampling-params rule; the budget is `max_completion_tokens` (not
  `max_tokens`) and is set higher — 8192 — because reasoning tokens are spent
  out of it before any JSON is emitted, and exhausting it yields an empty parse
  rather than an error.
- **Structured output via `client.messages.parse()` / `client.chat.completions
  .parse()`** with the same Pydantic models — validated objects, no manual JSON
  parsing, no prefill (prefill returns 400 on Opus 5).
- **One schema caveat, contained in `llm.py`.** OpenAI's strict mode forbids
  free-form maps, so `OCRInvoice.field_confidence: dict[str, float]` would be
  rejected. The OpenAI path uses a mirror model with one named float per field
  and converts back to `OCRInvoice`, so the contract callers see is identical.
- **Graceful degradation is mandatory.** If neither key is set or a call fails,
  every AI path falls back to a deterministic stub and marks the result
  `ai_available: false`. A live demo must never die on a network blip.

### 4.1 Requisition NLP contract (closes an open item from CLAUDE.md)

```python
class ParsedRequisition(BaseModel):
    material_id: str | None       # resolved against the seeded materials catalog
    material_name: str
    qty: float
    uom: str
    required_date: date | None
    delivery_location_id: str | None
    confidence: float             # 0-1, the model's own confidence
    ambiguities: list[str]        # empty => ready to convert
```

Written to `requisitions.parsed`. The system prompt carries the seeded
materials + locations catalog so the model resolves to real IDs instead of
inventing them. `POST /requisitions/chat` returns a clarifying question
whenever `ambiguities` is non-empty; the row is only written once it's clear.

### 4.2 OCR contract

The invoice simulator renders a realistic invoice **image** (Pillow), which
`POST /invoices` sends to Claude as a base64 image block alongside the
extraction prompt:

```python
class OCRInvoice(BaseModel):
    supplier_name: str
    po_reference: str | None      # None is a legitimate MISSING_PO scenario
    qty_invoiced: float
    unit_price_invoiced: float
    tax: float
    total: float
    field_confidence: dict[str, float]   # per-field, drives the OCR panel
```

`invoices.ocr_confidence` = mean of `field_confidence`; the full object goes to
`invoices.ocr_raw`; the rendered PNG path to `invoices.document_path`. Swapping
in Tesseract later is a change inside this one function.

### 4.3 Supplier reasoning

Scoring is deterministic (weighted price/quality/lead-time/reliability/risk over
the seeded supplier data). One LLM call then *writes the narrative* from the
computed scores into `supplier_recommendations.reasoning`. AI explains the
decision; it does not make it — the same principle `3WAY_MATCH_POLICY.md`
applies to matching.

### 4.4 Anomaly detection

Deterministic: z-score of the invoiced unit price against that supplier's
historical prices for that material. Above threshold → `alerts` row. The LLM
writes the alert message; the threshold decides.

---

## 5. Seed data + simulator + eval (closes the second open item)

### 5.1 Seed spec

| Table | Volume | Notes |
|---|---|---|
| `users` | 6 | 2 operator, 2 procurement, 1 finance, 1 admin — the Owner column needs real names |
| `locations` | 12 | 1 warehouse, 8 supplier sites, 3 waypoints, real lat/long in one metro so the map reads well |
| `suppliers` | 8 | Varied reliability/quality/risk; `metadata.price_multiplier` |
| `materials` | 15 | `metadata.base_price`, `metadata.category` |
| `docks` | 14 | Matches the design's D-01…D-14 board; mixed `compatible_load_types`; one `is_active=false` (the "Blocked" tile) |
| PO chains | 40 | Full requisition → PO → shipment → trailer → dock → receipt → invoice → match |

**Mismatch mix — 28% (inside README §7's 20–30% target):**

| Scenario | Count | Expected outcome |
|---|---|---|
| Clean | 29 | `APPROVED` |
| Qty variance > 2% | 4 | `EXCEPTION / QTY_MISMATCH` |
| Price variance > 3% | 3 | `EXCEPTION / PRICE_MISMATCH` |
| Missing PO reference | 2 | `EXCEPTION / MISSING_PO` |
| Duplicate invoice | 1 | `EXCEPTION / DUPLICATE_INVOICE` |
| Qty variance *within* tolerance (1.5%) | 1 | `APPROVED` — the near-miss that proves tolerance works |

Every generated pair is written to `seeds/ground_truth.json` with its intended
outcome. That file is the eval harness's answer key.

### 5.2 Simulator

Two processes, both driving the **real HTTP APIs** — never the database
directly. If the simulator can drive it, the system genuinely works.

- **Yard simulator**: moves trailers along waypoint routes, posts GPS ticks with
  drifting ETAs, calls `/arrive`, `/dock`, `/unload` on schedule. Tick 3s.
- **Supplier simulator**: renders and posts invoices on a delay after shipment,
  following the mismatch mix above.

**Scenario triggers** (`POST /sim/scenario/{name}` on a small control service):
`delay-trailer`, `inject-price-mismatch`, `inject-missing-po`, `block-dock`,
`surge-arrivals`. These are what let you force any demo moment on cue.

### 5.3 Eval harness

`backend/eval/run_eval.py` compares the seeded ground truth against what the
system actually decided:

- **3-way match as a classifier**: precision, recall, F1, and a confusion matrix
  over `{APPROVED, QTY_MISMATCH, PRICE_MISMATCH, MISSING_PO, DUPLICATE_INVOICE}`.
- **NLP parse**: field-level exact-match accuracy over a fixed set of 30 labelled
  requisition phrasings.
- **OCR**: field-level accuracy plus confidence calibration (is a 0.9-confidence
  field right ~90% of the time?).

Writes `eval_results.json`; `GET /kpi/model-performance` serves it. Every number
on the dashboard comes from our own measured run — never presented as a
Cognizant-given figure (README §8).

**BUILT in v8** — with three deviations from the spec above, all deliberate:

- **The answer key is `scenario`, NOT `expected_match_status`.** `seed.py`
  writes `expected_match_status` from the same `evaluate()` call the suite
  grades, so scoring against it returns F1 = 1.00 by construction — an identity
  wearing the costume of a measurement. `scenario` is the fault the seeder
  *injected*, chosen before the policy runs, and §5.1's table above maps each
  fault to its expected outcome. That mapping is the key.
- **The NLP fixture is hand-written** (`backend/eval/requisitions.json`), not
  drawn from seeded requisitions: every seeded requisition uses the single
  template `"We need {qty} {uom} of {material} delivered to the Bhiwandi
  plant"`, so scoring on it would measure one sentence sixty times. The 30
  cases vary abbreviations, typos, digits-vs-words, `nos`, unit variants,
  distractor materials, stated deadlines, and four under-specified requests
  where the correct behaviour is to raise an ambiguity rather than guess.
- **OCR reports `not_measured`.** `invoices.document_path` is written only by
  the image-upload path, and nothing in the seed or simulator renders an
  invoice image — so there is no scan to read. `ocr_raw` on seeded rows was
  written by `seed.py`, and grading it would grade `random.uniform()`.

The harness also prints the **majority-class baseline** (always-APPROVED scores
0.74 on this mix) beside the accuracy, names the **near-miss case** explicitly
(1.5% qty variance that must still be APPROVED — the one case separating a
tolerance policy from "flag everything"), and records whether each NLP case
reached the live model or the fallback stub. Suites are opt-in where they cost
money: `--nlp`, `--ocr`, `--all`.

---

## 6. Frontend

`frontend/app/` — Vite + React + TypeScript + Tailwind. Design tokens ported
from `design-reference/inbound_pay_design_system/DESIGN.md` into
`tailwind.config.js`. Inter + JetBrains Mono + Material Symbols.

**Mapping — superseded in v8.** This plan originally specified Leaflet 1.9.4 via
`react-leaflet`, following the design reference. The shipped system uses
**Mapbox GL JS** (`react-map-gl` v8 + `mapbox-gl` v3) instead: WebGL vector
tiles, and the **Mapbox Directions API** so a shipment's route follows actual
roads rather than a straight line between two pins. The token is read from
`VITE_MAPBOX_TOKEN`; if it is absent the map panel hides itself and every other
panel still renders.

| Route | Screen | Reads |
|---|---|---|
| `/` | Control Tower | `GET /dashboard/overview`, `/dashboard/pipeline`, `/dashboard/at-risk`, `/alerts` |
| `/yard` | Yard & Dock board | `GET /yard-status`; score panel from `dock_assignment_history[].score_breakdown` |
| `/procurement` | Sourcing AI | `POST /requisitions/chat`, `POST /requisitions/{id}/select-supplier` |
| `/match-pay/:invoiceId` | Match & Pay | `GET /invoices/{id}`, `GET /purchase-orders/{id}`, `GET /invoices/{id}/document` |
| `/exceptions` | Exceptions Command Center | `GET /exceptions/queue`, `GET /traceability/{po}`, resolve/assign |
| `/traceability/:poId` | Timeline | `GET /traceability/{po_id}` |
| `/track/:ref` | Public tracker (brief E2 #1) | `GET /track/{ref}` |

**Data-flow rule, per README §5:** every screen does a REST read on mount, then
applies WebSocket messages as deltas. One `useEventStream()` hook owns the
single `/ws/dashboard` connection and fans events out through a reducer. The
event stream is never used to reconstruct state from scratch — a client
connecting five minutes into the demo sees correct current state.

**Flagged design-vs-data gaps** (per the design-reference README's instruction
to flag rather than invent): "Environment: PRODUCTION" toggle is cosmetic and
will be hardcoded; there is no auth, so the user avatar is a fixed demo-user
picker; the 3-Way Match Detail table renders one line item because the schema is
header-level.

---

## 7. Deployment + CI

- One `backend/Dockerfile` (shared image, per-service `CMD`), one
  `frontend/Dockerfile` (build → nginx).
- `docker-compose.yml` grows to: postgres, redis, yard-api, procurement-api,
  dock-worker, match-worker, dashboard-gateway, simulator, frontend — with
  healthchecks and `depends_on` so `docker compose up` brings up the whole
  system on a clean machine.
- `.env.example` with `DATABASE_URL`, `REDIS_URL`, `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY` / `LLM_PROVIDER`.
- GitHub Actions: `ruff` + `pytest` on the backend, `tsc --noEmit` + `vite build`
  on the frontend, `docker compose build` on every push.

---

## 8. Build order

Each phase ends with a verification gate. **No phase is "done" on inspection —
it is done when the gate passes against live Postgres and Redis**, per
CLAUDE.md's testing discipline.

| Phase | Work | Gate |
|---|---|---|
| **0** | Apply §2 contract changes + §3 defect fixes | `schema.sql` applies clean to a fresh DB; existing Yard API tests still pass |
| **1** | Seed script + ground-truth file | 40 PO chains seeded; `ground_truth.json` matches row counts; README §9 reference query returns correct rows |
| **2** | Dock worker + Yard API additions (`/dock`, extended `/yard-status`, `GET /track`) | Trailer departs → dock assigned with score breakdown; ETA shift ≥10min → reassignment with old row `REASSIGNED`; unload frees the dock and a waiting trailer gets scored |
| **3** | Procurement API + AI layer (chat, supplier scoring + reasoning, OCR intake) | A typed sentence becomes a requisition with resolved `material_id`; supplier selection writes all candidates with reasoning; an invoice *image* posts and extracts correctly |
| **4** | Match worker + PO status reconciliation | All 40 seeded pairs match; 11 exceptions raised with the exact expected types; the 1.5% near-miss approves |
| **5** | Eval harness | Precision/recall/F1 printed from a real run; confusion matrix matches ground truth |
| **6** | Dashboard Gateway + WebSocket | `/dashboard/*`, `/exceptions/queue`, `/traceability`, `/search` return correct data; WS forwards every event; a client connecting mid-run sees correct state |
| **7** | Frontend, screen by screen | Each screen renders live data and updates over WS without a refresh |
| **8** | Simulator + scenario triggers | System runs unattended for 10 minutes with trailers moving, invoices arriving, exceptions appearing; each trigger fires its scenario on demand |
| **9** | Docker + CI + docs/PPT/roadmap | `docker compose up` on a clean machine brings up everything; CI green |

### Rough effort

| Phase | Estimate |
|---|---|
| 0–1 | 3h |
| 2 | 4h |
| 3 | 6h |
| 4–5 | 5h |
| 6 | 4h |
| 7 | 10h |
| 8 | 3h |
| 9 | 4h |
| **Total** | **~39h** — parallelizable across the team along the service boundaries |

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Live LLM call fails during the demo | Deterministic fallback on every AI path; `ai_available` flag surfaced in the UI |
| Map tiles need internet | Mapbox GL JS (v8). If the venue network is down or `VITE_MAPBOX_TOKEN` is unset, the map panel hides itself and the yard board, tracker status, milestones and every other screen still work |
| Simulator races the demo narrative | Scenario triggers give manual control; simulator can be paused |
| Scope: 14 new endpoints | Phases 2–6 are independently demoable; if time runs out, phases 8–9 are the drop candidates, not the core flow |

---

## 10. Still open

Nothing blocks Phase 0. Two items to settle before Phase 7:

- **Demo user set** — whose names appear in the Owner column. Trivial, but it's
  seed data someone should choose.
- **Presentation narrative** — which PO we walk the judges through end to end.
  Recommend seeding one deliberately memorable chain (a critical-priority reefer
  trailer that gets reassigned mid-route and then throws a price mismatch),
  since it exercises both halves of the system in one story.
