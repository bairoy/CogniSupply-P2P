# Inbound-to-Pay Platform — README

Cognizant NPN_SCM Hackathon, Combination 2 (E2 + PR2).
Read this first. Everything below is locked before writing application code.

## What this is

One integrated system, not two apps sharing a dashboard:

**E2** — Where's My Truck? Yard, Dock Door & Delivery Tracker
**PR2** — End-to-End Autonomous Procure-to-Pay

They connect at one point: a trailer's `goods_receipt` event (E2) is what
lets a PO's invoice get 3-way matched and auto-approved (PR2). That link
is the entire reason this is one project instead of two.

## The locked files

| File | What it locks |
|---|---|
| `schema.sql` | Every table, column, and relationship. Verified against live PostgreSQL 16 — see §6. |
| `redis-contract.md` | The event stream, its field contract, and the fixed `event_type`/`entity_type` vocabulary. |
| `event_bus.py` | The one sanctioned way any service writes an event — Postgres first, Redis second, never the reverse. |
| `shared/auth.py` | The role vocabulary and the capability matrix. Roles are append-only like statuses; a new permission is added here, never as an inline role check in a handler. |

Nothing outside these files is "real" yet. If a feature needs a
new status value, event type, role, permission, or field — add it here
first, then code against it. Never the other way around.

## 1. Tech stack

| Layer | Choice |
|---|---|
| Database | PostgreSQL 16 |
| Live event delivery | Redis Streams (one stream: `events:supply-chain`) |
| Backend | Python, FastAPI (Yard API service, Procurement API service) |
| Background workers | Python — dock scheduling worker, 3-way match worker |
| Dock scheduling | OR-Tools CP-SAT over an integer cost model, deterministic (`docs/DOCK_DECISION_ENGINE.md`) |
| Frontend | React + Tailwind, REST + WebSocket |
| Auth | Stateless HS256 JWTs (PyJWT) + bcrypt password hashes; roles enforced per-endpoint |
| NLP | LLM call for requisition intent extraction |
| OCR | Structured JSON mock first; real OCR (Tesseract) if time allows |

Deliberately not used, and why: no message broker beyond Redis, no S3
(invoice files can stay as JSON/base64 in Tier 1), no PostGIS (plain
lat/long floats are enough for simulated GPS), no native Postgres enum
types (TEXT status columns avoid migration risk).

## 1b. Authentication and roles

Every API is authenticated. The `users.role` column that was always in the
schema is now load-bearing: it decides what a signed-in person can *do*, not
just whose name appears in an Owner column.

| Role | Can act on |
|---|---|
| `operator` | The yard: shipments, trailers, tracking, arrive/dock/unload, dock reassignment |
| `procurement` | Requisitions, supplier selection → PO, invoice intake, routing exceptions |
| `finance` | Invoice intake, resolving exceptions, releasing payments |
| `admin` | Everything, plus granting roles |
| `system` | Nothing — USR-000 is the service account touchless approvals are recorded against, and cannot sign in |

Reads are open to any signed-in user on purpose. Hiding the supply chain from
the people upstream of it is the silo this project exists to remove; the
separation that matters is over *writes*.

Mechanics: HS256 bearer tokens issued by the gateway (`POST /auth/login`,
`POST /auth/signup`) and verified locally by all three services with a shared
`JWT_SECRET` — no session table, no Redis session cache, no auth network hop.
The full capability matrix and every endpoint is in `docs/api-contract.md`
§v5; the implementation is `backend/shared/auth.py`.

Demo accounts are seeded (`priya@`, `meiling@`, `aisha@`, `jordan@inbound.dev`,
password `inbound2026`) and listed on the sign-in screen. Signing in as two of
them is the fastest way to see the model actually refuse an action.

**Applying it to a database that already exists:** `schema.sql` is only run by
docker-compose on a fresh volume, so run `./run.sh migrate` — it applies
`backend/migrations/*.sql` (idempotent) and gives the seeded users their
credentials without regenerating any traffic.

## 2. The one non-negotiable ownership rule

**`goods_receipts` is written only by the E2/yard service.** PR2 reads
it, never writes it. This is what makes the write path race-free. Do
not relitigate this mid-build.

## 3. Data flow, in order

```
1. Employee submits requisition (NLP)      → requisitions row
2. AI scores suppliers, picks one          → supplier_recommendations rows, purchase_orders row
3. Shipment simulated, linked to PO        → shipments row
4. Trailer created                         → trailers row
5. Dock worker plans doors over time       → dock_assignments row (+ planned window)
6. Trailer "arrives", unloads, departs     → goods_receipts row  (E2 writes this)
7. Invoice arrives (OCR)                   → invoices row
8. Match worker compares PO+GR+Invoice     → match_results row
   → within tolerance: APPROVED → payments row
   → outside tolerance: EXCEPTION → exceptions row, human review
```

Every domain write above also logs an event, using the pattern in
`event_bus.py`: `record_event()` runs inside the same transaction as the
domain-table write, both commit together, then `publish_to_redis()`
mirrors it live. (`publish_event()` is a convenience wrapper for the
rarer case where the event has no accompanying domain-table write.) See
`redis-contract.md` §7 for the exact ordering contract.

## 4. ETA field ownership

Four fields all relate to timing. They are not redundant — each answers
a different question, and code should only ever write to the one that
matches what it's actually reporting:

| Field | Meaning | Who writes it |
|---|---|---|
| `purchase_orders.expected_delivery` | Supplier's contractual promised date, set once at PO creation | Procurement API, at PO creation |
| `shipments.expected_arrival` | Current operational ETA for the whole shipment | Yard API / dock worker, can update as conditions change |
| `trailers.eta` | Latest known ETA for this specific trailer | Real-time engine, updated on every tracking tick |
| `tracking_events.eta_estimate` | Historical snapshot — what the ETA prediction *was* at that tracking point | Written once per tracking event, never updated |

`tracking_events` is the only append-only one of the four — it's what
lets you later compute "how did our ETA prediction drift over time,"
which is a legitimate KPI (`ETA prediction error`) if you have time
for it.

## 5. Build order

1. Apply `schema.sql`, confirm it builds clean (it already does — §6).
2. Seed `locations`, `suppliers`, `materials`, `docks` — master data first.
3. Write the two simulator scripts (yard/GPS, supplier/invoice) that
   generate realistic Tier-1 traffic against the schema.
4. Yard API service + dock scoring worker. Test with raw API calls
   before touching the frontend.
5. Procurement API service, mocked invoice as structured JSON first.
6. 3-way match worker — build and test the tolerance/exception logic
   against seeded clean *and* mismatched pairs.
7. Wire in `event_bus.py` across all of the above.
8. Dashboard last — every state it needs to render already exists in
   the DB and the event stream by this point. Two distinct paths, not
   one: **initial load** is a plain REST call to Postgres (a client
   connecting five minutes into the demo must see correct current
   state, not an empty screen waiting for the next event); **live
   updates** come from `dashboard-ws` forwarding the Redis stream over
   WebSocket. The event stream is for *changes*, not for *reconstructing
   state from scratch*.

## 6. Verification status

`schema.sql` has been executed against a live PostgreSQL 16 instance:

- All 21 `CREATE TABLE` statements succeed in dependency order (no
  forward references).
- All indexes build successfully, including the unique constraint that blocks double-matching an invoice.
- A full requisition → PO → shipment → trailer → dock assignment →
  goods receipt → invoice → match → exception chain was inserted and
  joined successfully, correctly surfacing a seeded quantity mismatch
  (PO 500 vs. invoice 550) as `EXCEPTION` / `QTY_MISMATCH`.
- A bad foreign key (`shipment_id` pointing to a non-existent shipment)
  was correctly rejected by Postgres — integrity constraints are real,
  not just declared.
- The PO-promise-vs-shipment-ETA variance query (§9 below) runs
  correctly against seeded data.

This is not a schema that "looks right" — it has been run.

## 7. What to seed deliberately

- ~70-80% of trailer/PO pairs should resolve cleanly end-to-end (the
  "it works" demo path).
- ~20-30% should carry an intentional mismatch — quantity, price, or a
  missing PO reference. This is what proves the exception-handling
  story, not just the happy path. A demo that only shows auto-approval
  looks like the outdated rules-only version of this problem, not the
  agentic version.

## 8. KPIs — measured, not claimed

Cognizant's brief gives qualitative outcomes, not numeric targets.
Any percentage on the final dashboard must come from your own seeded
run (baseline vs. measured), never presented as a Cognizant-given
number. Suggested KPIs:

**E2**: average truck turnaround time, dock utilization %, truck
waiting time, on-time dock-ready %.
**PR2**: P2P cycle time, first-pass 3-way match rate, % touchless
transactions, manual interventions per transaction.

## 9. Key reference query

The query every KPI and demo moment ultimately traces back to:

```sql
SELECT po.id AS po,
       loc.name AS delivery_point,
       po.expected_delivery AS po_promised,
       shp.expected_arrival AS shipment_eta,
       gr.qty_received,
       inv.qty_invoiced,
       mr.status AS match_status,
       ex.exception_type
FROM purchase_orders po
JOIN locations loc      ON loc.id = po.delivery_location_id
JOIN shipments shp      ON shp.po_id = po.id
LEFT JOIN goods_receipts gr ON gr.po_id = po.id
LEFT JOIN invoices inv      ON inv.po_id = po.id
LEFT JOIN match_results mr  ON mr.po_id = po.id
LEFT JOIN exceptions ex     ON ex.match_result_id = mr.id
WHERE po.id = 'PO-10245';
```

## 10. Team discipline for staying frozen

- Status values are append-only. New value = add to the comment list
  in `schema.sql`. Never rename or repurpose an existing one.
- New event type = add to `redis-contract.md` §3 first, then emit it.
  Never invent one inline in code.
- New field that doesn't fit an existing column = goes in that table's
  `metadata`/`payload`/`terms` JSONB column, not a new migration,
  unless it's something you'll query/index constantly (like
  `expected_delivery` was) — in which case it earns a real column,
  added here first.
