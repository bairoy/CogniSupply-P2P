# API Contract — Inbound-to-Pay Platform

Built strictly on `schema.sql`, `redis-contract.md`, and `event_bus.py` —
none of those are modified by this document. Every event named here uses
the exact `event_type`/`entity_type` values already locked in
`redis-contract.md` §3/§4. No new event types are introduced.

Transaction boundary notation used throughout:
**TX:** what's inside one `BEGIN...COMMIT` (domain write + `record_event()`
together), followed by **Publish:** the `publish_to_redis()` call made
after that commit — matching the pattern in `event_bus.py`'s docstring
exactly.

---

## Service boundaries (5 components)

| Component | Owns | HTTP surface |
|---|---|---|
| Yard API | `shipments`, `trailers`, `tracking_events`, `dock_assignments`, `goods_receipts` (write) | REST |
| Procurement API | `requisitions`, `supplier_recommendations`, `purchase_orders`, `invoices`, `match_results`, `exceptions`, `payments` | REST |
| Dock Worker | Consumes `dock-worker` group, writes `dock_assignments`/`alerts` | None — background `consume()` loop only |
| Match Worker | Consumes `match-worker` group, writes `match_results`/`payments`/`exceptions` | None — background `consume()` loop only |
| Dashboard Gateway | Nothing (read-only across both domains) | REST + the one WebSocket endpoint |

The Gateway is new as a named component — it wasn't listed before, but
`redis-contract.md` already names `dashboard-ws` as "owned by Dashboard
WebSocket layer," so this just gives that existing name a concrete home
instead of leaving it ambiguous which process runs it.

---

## YARD API (E2)

### `POST /shipments`
Supplier has begun fulfilling a PO — the bridge point where PR2 hands
off to E2.

Request:
```json
{ "po_id": "PO-10245", "tracking_number": "TRK998", "carrier": "Acme Freight",
  "origin_location_id": "LOC-001", "destination_location_id": "LOC-002",
  "expected_arrival": "2026-08-13T14:20:00Z" }
```
Response `201`:
```json
{ "id": "SHP-2031", "status": "CREATED" }
```
- **Reads:** `purchase_orders` (validate po_id exists)
- **Writes:** `shipments`
- **TX:** INSERT `shipments` + `record_event(SHIPMENT_CREATED)`, commit together
- **Publish:** `SHIPMENT_CREATED`, entity_type=`shipment`
- **Consumed by:** `dock-worker` (no-op — no trailer exists yet, see Dock Worker section), `dashboard-ws`

### `POST /shipments/{shipment_id}/trailers`
Trailer departs.

Request:
```json
{ "load_type": "dry_van", "priority": "high" }
```
Response `201`:
```json
{ "id": "TRL-3391", "status": "EN_ROUTE" }
```
- **Reads:** `shipments`
- **Writes:** `trailers`
- **TX:** INSERT `trailers` + `record_event(TRAILER_DEPARTED)`, commit together
- **Publish:** `TRAILER_DEPARTED`, entity_type=`trailer`
- **Consumed by:** `dock-worker` (**this is the real initial-scoring trigger** — a trailer now exists to assign a dock to), `dashboard-ws`

### `POST /trailers/{trailer_id}/tracking`
Simulator posts a GPS tick.

Request:
```json
{ "latitude": 13.00, "longitude": 79.00, "speed": 48, "eta_estimate": "2026-08-13T14:35:00Z" }
```
Response `201`: `{ "recorded": true, "eta_changed_materially": true }`
- **Reads:** `trailers` (current `eta` for delta comparison)
- **Writes:** `tracking_events` (append), `trailers.eta`
- **TX, in this exact order** (the order matters — computing the delta after overwriting `eta` would always yield zero):
  1. `SELECT eta FROM trailers WHERE id = ...` — capture the *old* value first
  2. INSERT `tracking_events`
  3. UPDATE `trailers.eta` to the new value
  4. `record_event(TRAILER_LOCATION_UPDATED)` always
  5. If `|new_eta - old_eta| >= 10 min`: **also** `record_event(ETA_UPDATED)` in this same transaction
  6. Commit once
- **Publish:** `TRAILER_LOCATION_UPDATED` always; `ETA_UPDATED` conditionally, both after the single commit
- **Consumed by:** `dock-worker` (`TRAILER_LOCATION_UPDATED` → tracking only, no rescore, per redis-contract.md §9; `ETA_UPDATED` → rescore, subject to the same §9 threshold — see note below on why the threshold check appears twice), `dashboard-ws`

> **Why the 10-minute check happens in two places:** the API only emits `ETA_UPDATED` when the change is material (≥10 min), so `dock-worker` isn't flooded with events for a 30-second GPS jitter. The worker's own threshold check (§9) exists for a different reason — a defensive re-check using its own last-scored ETA, so it never re-optimizes off two small emitted changes that individually cleared 10 min from the *previous tick* but the worker hasn't re-scored between them. Belt and suspenders, cheap to keep both, no contradiction with the locked contract.

### `POST /trailers/{trailer_id}/arrive`
Trailer physically reaches the gate.

Response `200`: `{ "id": "TRL-3391", "status": "ARRIVED" }`
- **Writes:** `trailers.status = 'ARRIVED'`
- **TX:** UPDATE `trailers` + `record_event(TRAILER_ARRIVED)`, commit together
- **Publish:** `TRAILER_ARRIVED`, entity_type=`trailer`
- **Consumed by:** `dock-worker` (v6 — the trailer is ready for a door **now** rather than at its ETA, which changes the plan), `dashboard-ws`

### `POST /trailers/{trailer_id}/unload`
Unloading completes — the E2→PR2 bridge point.

Request:
```json
{ "qty_received": 500 }
```
Response `201`:
```json
{ "goods_receipt_id": "GR-9876" }
```
- **Reads:** `trailers`, `shipments` (to resolve `po_id`)
- **Writes:** `trailers.status = 'UNLOADED'`, `goods_receipts` — **this table is written ONLY here, by Yard API. Locked rule, unchanged.**
- **TX:** UPDATE `trailers` + INSERT `goods_receipts` + `record_event(GOODS_RECEIVED)`, commit together
- **Publish:** `GOODS_RECEIVED`, entity_type=`goods_receipt`
- **Consumed by:** `match-worker` (checks for an existing invoice on this `po_id`; runs the 3-way match now if one exists), `dashboard-ws`

### `POST /dock-assignments/{id}/reassign`
Manual operator override (not the automatic path — that's `dock-worker`).

Request: `{ "new_dock_id": "DOCK-02", "reason": "operator override" }`
Response `200`: `{ "old_assignment_id": "DA-5521", "new_assignment_id": "DA-5522", "status": "ASSIGNED", "planned_start": "...", "planned_end": "...", "wait_minutes": 0 }`
- **Writes (history-preserving — locked pattern, see note below):** UPDATE the existing `dock_assignments` row `SET status = 'REASSIGNED'`; INSERT a new `dock_assignments` row for the same `trailer_id` with the new `dock_id`, `status = 'ASSIGNED'`
- **TX:** both writes + `record_event(DOCK_REASSIGNED)`, commit together — `entity_id` is the **new** assignment's id; `payload` includes `old_assignment_id` for traceability
- **Publish:** `DOCK_REASSIGNED`, with `source: "operator"`
- **Consumed by:** `dock-worker` (re-plans every other trailer around the override), `dashboard-ws`

> **v6 — the override is pinned, and its window is computed.** The new row is written with `score_breakdown.source = "manual_override"`, which the scheduler treats as immovable: it will never reverse an operator's door choice, it plans around it. The *door* is honoured verbatim; the *time* is placed at the earliest slot that door is genuinely free at or after the trailer is ready, because two trailers cannot occupy one door at once. `planned_start`/`planned_end` come back in the response so the operator sees what they just committed to.

> **Locked reassignment pattern, applies everywhere a dock gets reassigned** (this manual endpoint *and* `dock-worker`'s automatic `ETA_UPDATED` path below): never `UPDATE` a `dock_assignments` row's `dock_id` in place. Always mark the old row `REASSIGNED` and `INSERT` a new one `ASSIGNED`. This makes `dock_assignments` a genuine history table — `GET /trailers/{id}` can show the full "D4 → D2 → D4" story, which is a real demo moment (see README §9's judge-question example), and it would be silently lost by an in-place update.

### `POST /trailers/{trailer_id}/depart`  *(v6)*
Trailer clears the gate — the outbound leg.

Response `200`: `{ "id": "TRL-3391", "status": "DEPARTED" }`
- **Reads:** `trailers` (must be `UNLOADED`, else `409`)
- **Writes:** `trailers.status = 'DEPARTED'`
- **TX:** UPDATE `trailers` + `record_event(TRAILER_EXITED)`, commit together
- **Publish:** `TRAILER_EXITED`, entity_type=`trailer`
- **Consumed by:** `dashboard-ws` only — the door was already released at unload, so there is no dock work here

> `UNLOADED` and `DEPARTED` are deliberately distinct: the door frees at unload, the yard slot frees at the gate. Without the second state an unloaded trailer vanished from the board, which made "how many trailers are in my yard" unanswerable and hid the outbound half of the movement.

### `GET /yard-status`
Dashboard's E2 initial-load read (REST — see README §5 on why this exists separately from the WebSocket). Each trailer's `dock_assignment` shows the **current** one only — `WHERE trailer_id = ... AND status IN ('ASSIGNED','CONFIRMED')` (there is exactly one such row per trailer at any time, by construction of the locked reassignment pattern above; `REASSIGNED` rows are history, not current state).

Response:
```json
{ "trailers": [{ "id": "TRL-3391", "status": "EN_ROUTE", "eta": "...",
                 "waiting_minutes": null, "arrived_at": null,
                 "dock_assignment": { "dock_id": "DOCK-04", "status": "ASSIGNED",
                                      "planned_start": "...", "planned_end": "...",
                                      "planned_wait_minutes": 12 } }],
  "docks": [{ "id": "DOCK-04", "yard_position": 4, "occupied": false,
              "state": "EMPTY", "service_minutes": 50,
              "window_start": null, "window_end": null,
              "next_trailer_id": "TRL-3402", "next_start": "..." }],
  "summary": { "inbound": 14, "in_yard_waiting": 8, "at_door": 5,
               "awaiting_exit": 3, "unassigned": 0,
               "docks_active": 13, "docks_busy": 7, "dock_occupancy_pct": 54,
               "avg_wait_minutes": 21, "longest_wait_minutes": 58 } }
```
- **Reads:** `trailers`, `dock_assignments`, `docks`, `shipments`, `tracking_events`, `event_log` (arrival time). No writes, no event.
- Trailers are on the board until they are `DEPARTED`, so the unloaded-but-still-here state is visible. `waiting_minutes` (derived from the `TRAILER_ARRIVED` event, accruing only while `ARRIVED`) and `unload_progress_pct` are both **derived at read time** — storing either would need a writer ticking it every few seconds.

### `GET /dock-schedule?hours=12`  *(v6)*
The door timeline: for each dock, the windows committed on it over the horizon, plus the utilisation that follows from them. `/yard-status` answers "what is happening now"; this answers "what is each door doing for the rest of the shift", which is what dock-door availability actually means.

Response:
```json
{ "generated_at": "...", "horizon_hours": 12,
  "docks": [{ "id": "DOCK-11", "yard_position": 11, "is_active": true,
              "compatible_load_types": ["dry_van","flatbed","tanker"],
              "service_minutes": 50, "committed_minutes": 250, "utilisation_pct": 35,
              "bookings": [{ "assignment_id": "DA-5521", "trailer_id": "TRL-3391",
                             "start": "...", "end": "...", "in_progress": true,
                             "priority": "high", "wait_minutes": 0,
                             "source": "dock-worker", "reason": "..." }] }],
  "summary": { "docks_total": 14, "docks_active": 13, "booked_windows": 22,
               "utilisation_pct": 31 } }
```
- **Reads:** `docks`, `dock_assignments`, `trailers`, `shipments`. No writes, no event.
- Booked minutes come from the planned windows the scheduler wrote, so utilisation is measured against the real plan rather than estimated from occupancy at one instant.

### `GET /trailers/{trailer_id}`
Full timeline for one trailer — the "show me what happened to TRL-3391" panel-Q&A endpoint. `dock_assignments` here is the **full history** (all rows for this `trailer_id`, ordered by `assigned_at`) — this is what actually answers "why did the dock change," not just the current one.

Response includes tracking history, full dock assignment history, and the matching `event_log` rows for `entity_type='trailer' AND entity_id='TRL-3391'`.
- **Reads:** `trailers`, `tracking_events`, `dock_assignments`, `event_log`. No writes.

---

## PROCUREMENT API (PR2)

### `POST /requisitions`
NLP intake.

Request: `{ "raw_text": "I need 500 units of Material X", "requested_by": "USR-001" }`
Response `201`: `{ "id": "REQ-1001", "parsed": { "item": "MAT-001", "qty": 500 }, "status": "PARSED" }`
- **Writes:** `requisitions` (LLM call happens inside the request handler; `parsed` JSONB is the structured output)
- **TX:** INSERT `requisitions` + `record_event(REQUISITION_CREATED)`, commit together
- **Publish:** `REQUISITION_CREATED`, entity_type=`requisition`
- **Consumed by:** `dashboard-ws` only

### `POST /requisitions/{id}/select-supplier`
AI scores suppliers and auto-creates the PO — two events, one transaction.

Response `201`:
```json
{ "purchase_order_id": "PO-10245",
  "recommendations": [{ "supplier_id": "SUP-001", "overall_score": 0.91, "rank": 1, "recommended": true },
                       { "supplier_id": "SUP-002", "overall_score": 0.83, "rank": 2, "recommended": false }] }
```
- **Reads:** `requisitions`, `suppliers`, `materials`
- **Writes:** `supplier_recommendations` (one row per candidate, not just the winner — this is what lets the demo answer "why Supplier B"), `purchase_orders`, `requisitions.status = 'CONVERTED'`
- **TX:** all of the above INSERTs/UPDATEs + `record_event(SUPPLIER_RECOMMENDED)` + `record_event(PO_CREATED)`, one commit
- **Publish:** `SUPPLIER_RECOMMENDED` (entity_type=`requisition` — there's no `supplier_recommendation` entity_type in the locked vocabulary, and there doesn't need to be; the recommendations belong conceptually to the requisition they were computed for), then `PO_CREATED` (entity_type=`purchase_order`)
- **Consumed by:** `dashboard-ws` only for both (neither worker subscribes to procurement-intake events — correct per the locked contract, dock-worker and match-worker only react once physical/document events start)

### `POST /invoices`
OCR intake (Tier 1: accepts pre-structured mock JSON; real OCR call is a drop-in swap inside this handler, no contract change).

Request:
```json
{ "po_id": "PO-10245", "qty_invoiced": 550, "unit_price_invoiced": 100, "tax": 0,
  "ocr_raw": { "vendor": "Acme Parts", "confidence": 0.97 } }
```
Response `201`: `{ "id": "INV-7788" }`
- **Writes:** `invoices`
- **TX:** INSERT `invoices` + `record_event(INVOICE_RECEIVED)` + `record_event(OCR_COMPLETED)`, one commit (both fire together since Tier 1 OCR is synchronous within the request — if real async OCR is added later, split into two endpoints without touching this contract's event shapes)
- **Publish:** `INVOICE_RECEIVED` (entity_type=`invoice`), then `OCR_COMPLETED` (entity_type=`invoice`)
- **Consumed by:** `match-worker` reacts to `INVOICE_RECEIVED` (checks for an existing `goods_receipt` on this `po_id`, runs the match now if one exists — `OCR_COMPLETED` is not in match-worker's allowed set, it's informational only since `INVOICE_RECEIVED` is already the trigger), `dashboard-ws` gets both

### `GET /purchase-orders/{id}`
The "key reference query" from `README.md` §9, as an endpoint — PO promise vs. shipment ETA vs. receipt vs. invoice vs. match, in one call.

- **Reads:** `purchase_orders`, `locations`, `shipments`, `goods_receipts`, `invoices`, `match_results`, `exceptions`. No writes.

### `GET /purchase-orders?status=`
List/filter POs. Read-only.

### `GET /exceptions?status=OPEN`
Exception queue for human review. Read-only.

### `POST /exceptions/{id}/resolve`
Human review closes the loop.

Request: `{ "resolution": "APPROVE", "resolved_by": "USR-002", "notes": "confirmed with supplier, extra units accepted" }`
Response `200`: `{ "id": "EXC-201", "status": "APPROVED" }`
- **Writes:** `exceptions.status`, `exceptions.resolution_notes`, `exceptions.resolved_at`; if `APPROVE`: also `payments` (new row, `status='APPROVED'`), `purchase_orders.status = 'MATCHED'`
- **TX:** all writes + `record_event(EXCEPTION_RESOLVED)` + (if approved) `record_event(PAYMENT_APPROVED)`, one commit
- **Publish:** `EXCEPTION_RESOLVED` (entity_type=`exception`), then conditionally `PAYMENT_APPROVED` (entity_type=`payment`)
- **Consumed by:** `dashboard-ws` only for both

---

## DOCK WORKER (background — `consume()`, no HTTP surface)

`allowed_event_types` = `{"SHIPMENT_CREATED", "TRAILER_DEPARTED", "ETA_UPDATED", "TRAILER_LOCATION_UPDATED", "TRAILER_ARRIVED", "TRAILER_DOCKED", "GOODS_RECEIVED", "DOCK_REASSIGNED"}` (exactly `redis-contract.md` §5)

**v6: every trigger runs one function.** Read the whole yard from Postgres,
plan it with `shared/dock_engine.plan_docks()`, write back the difference. The
worker no longer scores the single trailer an event names — it re-derives the
schedule for every pending trailer from committed state, which is why the
answer never depends on event arrival order and why a worker that has been
down recovers by simply planning on startup. Full model in
`docs/DOCK_DECISION_ENGINE.md`.

| Event | Handler action |
|---|---|
| `SHIPMENT_CREATED` | No-op. No trailer row exists yet — real action waits for `TRAILER_DEPARTED`. Still claimed via `processed_events` (cheap), just does nothing. |
| `TRAILER_DEPARTED` | Re-plan. The new trailer gets a door, a `planned_start`/`planned_end` window and a `reason`; `record_event(DOCK_ASSIGNED)` in the same TX, commit together. |
| `TRAILER_LOCATION_UPDATED` | Tracking only, per §9 — no domain write, no event emitted. Claimed and acked, nothing else. |
| `ETA_UPDATED` | Re-plan **only if** the new ETA differs from the ETA last used for planning by ≥10 min (§9). A large slip *also* raises a `DELAY` alert on the trailer, independent of which door it ends up at. |
| `TRAILER_ARRIVED` | Re-plan: the trailer is ready **now** rather than at its ETA, so it competes differently. |
| `TRAILER_DOCKED` | Re-plan: that window is now immovable, and its door is fixed for everything else. |
| `GOODS_RECEIVED` | Re-plan: a door was released, so trailers queued behind it move up. |
| `DOCK_REASSIGNED` | Re-plan **unless** `payload.source == "dock-worker"` (the worker's own move, already reflected in the state it would read). An operator override is pinned and everything else is planned around it. |
| **Writing the difference** (any re-plan) | Same door → refresh `planned_start`/`planned_end`/`score_breakdown`, **no event**. Different door → **locked reassignment pattern**: old row `REASSIGNED`, new row `ASSIGNED`, `record_event(DOCK_REASSIGNED)` on the new id. No feasible door → `alerts` row (`DOCK_CONFLICT`) + `ALERT_CREATED`, once per open conflict. Planned wait ≥45 min → `alerts` row (`DELAY`) + `record_event(DOCK_DELAYED)`, once, on crossing the threshold. One transaction, one commit, owned by `consume()`. |

## MATCH WORKER (background — `consume()`, no HTTP surface)

`allowed_event_types` = `{"GOODS_RECEIVED", "INVOICE_RECEIVED"}` (exactly `redis-contract.md` §5)

| Event | Handler action |
|---|---|
| `GOODS_RECEIVED` | Look up `po_id` from the payload. Check if an `invoices` row already exists for that `po_id`. If yes → run 3-way match now. If no → do nothing yet (the eventual `INVOICE_RECEIVED` will trigger it). |
| `INVOICE_RECEIVED` | Same check in reverse: if a `goods_receipts` row already exists for this `po_id`, run the match now; otherwise wait. |
| **Match execution** (either trigger) | Compare PO/receipt/invoice (tolerance rules — separate lock item, not defined here). Write `match_results` (status=`APPROVED`\|`EXCEPTION`). If approved: also write `payments` (status=`APPROVED`), update `purchase_orders.status='MATCHED'`. If exception: also write `exceptions` (status=`OPEN`). `record_event(MATCH_COMPLETED)` always, then `record_event(PAYMENT_APPROVED)` **or** `record_event(EXCEPTION_CREATED)` depending on outcome — all in one transaction, one commit. |

---

## DASHBOARD GATEWAY (cross-domain reads + the one WebSocket)

### `GET /dashboard/overview`
Cross-domain summary — the only endpoint that reads across both Yard and Procurement tables in one call, which is exactly why it doesn't live inside either domain service.

```json
{ "active_trailers": 6, "open_exceptions": 2, "pending_invoices": 3,
  "docks_occupied": 3, "docks_total": 8, "kpis": { "avg_touchless_rate": 0.78 } }
```
- **Reads:** across both domains. No writes, no event.

### `GET /alerts?acknowledged=false`
Cross-cutting for the same reason — `alerts.entity_type` spans both `trailer`/`dock_assignment` (E2) and `match_result` (PR2).

### `WS /ws/dashboard`
Runs the `dashboard-ws` consumer group internally (`consume(pg_conn, "dashboard-ws", ..., allowed_event_types=None)` — the one group that gets everything, per the locked contract). Forwards every event envelope verbatim to all connected clients:
```json
{ "event_id": "10291", "event_type": "DOCK_ASSIGNED", "entity_type": "dock_assignment",
  "entity_id": "DA-5521", "timestamp": "2026-08-13T14:20:05Z", "payload": {"dock_id": "DOCK-04"} }
```
Client contract, per README §5: call `GET /yard-status` + `GET /purchase-orders` + `GET /dashboard/overview` **once on connect** for current state, then apply WS messages as deltas. The WebSocket is for *changes*, never for reconstructing state from scratch.

---

---

## v4 ADDITIONS (approved in BUILD_PLAN.md §2.3, implemented)

Additive only — no existing endpoint changed method, URL, or removed a field.
Response additions are new keys on existing objects.

### Yard API
| Endpoint | Purpose |
|---|---|
| `POST /trailers/{id}/dock` | `trailers.status='DOCKED'`, current assignment → `CONFIRMED`, sets `docked_at`. Emits `TRAILER_DOCKED`. Guarded: 409 unless the trailer is `ARRIVED` and has a current assignment. |
| `GET /yard-status` | **Response extended**: trailers gain `carrier`, `load_type`, `priority`, `po_id`, `latitude`, `longitude`, `tracking_number`; docks gain `state` (EMPTY/RESERVED/UNLOADING/BLOCKED), `current_trailer_id`, `assignment_reason`, `unload_progress_pct` (derived from `docked_at` + `docks.metadata.expected_unload_minutes`, never stored). |
| `GET /trailers/{id}` | **Response extended**: each dock assignment gains `score_breakdown` and `docked_at`. |

Shipment lifecycle (`CREATED → EN_ROUTE → ARRIVED → UNLOADED`) is now actually
written by the existing trailer endpoints; it was specified in `schema.sql` and
never implemented.

### Procurement API
| Endpoint | Purpose |
|---|---|
| `POST /requisitions/chat` | Conversational NLP intake. Returns `{status:"clarifying", questions[]}` or `{status:"parsed", id}`. **Writes nothing while ambiguous** — a half-understood request never becomes a requisition row. |
| `GET /requisitions/{id}` | Requisition + all scored candidates + resulting PO. |
| `POST /invoices/ocr` | Real OCR: multipart image → Claude vision → extracted fields. The PO reference comes from the DOCUMENT, so an invoice showing none yields `po_id=NULL` and a genuine `MISSING_PO`. |
| `GET /invoices/{id}` | Invoice + PO + receipt + match + exception + payment, with variance. |
| `GET /invoices/{id}/document` | Streams the stored scan ("View Original Scan"). |
| `POST /exceptions/{id}/assign` | Writes `exceptions.assigned_to`. Emits `EXCEPTION_ASSIGNED`. |
| `GET /payments` | Payment list/filter. |
| `POST /payments/{id}/pay` | `APPROVED → PAID`, PO → `CLOSED`. Emits `PAYMENT_PAID` + `PO_STATUS_CHANGED`. |

### Dashboard Gateway
| Endpoint | Purpose |
|---|---|
| `GET /dashboard/pipeline` | Funnel counts per stage. |
| `GET /dashboard/at-risk` | Open exceptions + unacknowledged alerts + stalled requisitions, ranked. |
| `GET /exceptions/queue` | **`exceptions` UNION `alerts`**, read-only. The design shows "Dock Delay" beside "Price Mismatch"; they live in different tables and neither table changes. |
| `GET /traceability/{po_id}` | Cross-entity timeline: gathers every related entity id, then their `event_log` rows in one pass. |
| `GET /search?q=` | Global Cmd+K resolution across trailer/shipment/tracking-number/PO/invoice/exception. |
| `GET /track/{ref}` | Customer-facing tracker (brief E2 #1): tracking number, trailer ID or shipment reference → location, ETA, progress. |
| `GET /map/trailers` | Live positions + origin/destination for the map. |
| `POST /alerts/{id}/acknowledge` | Writes `alerts.acknowledged`. Emits `ALERT_ACKNOWLEDGED`. |
| `GET /kpi/model-performance` | Latest eval-harness run. 404 until the harness has run — an honest "not measured yet" beats a fabricated number. |

### Match Worker — allowed set gains `SHIPMENT_CREATED`
`purchase_orders` is PR2-owned and Yard API must never write it, so match-worker
is the PR2-side status reconciler: `SHIPMENT_CREATED → SHIPPED`,
`GOODS_RECEIVED → RECEIVED | PARTIALLY_RECEIVED`, approved match → `MATCHED`,
payment paid → `CLOSED`. Each emits `PO_STATUS_CHANGED`.

---

## Final FastAPI route tree

```
yard-api/
  POST   /shipments
  POST   /shipments/{shipment_id}/trailers
  POST   /trailers/{trailer_id}/tracking
  POST   /trailers/{trailer_id}/arrive
  POST   /trailers/{trailer_id}/unload
  POST   /dock-assignments/{id}/reassign
  GET    /yard-status
  GET    /trailers/{trailer_id}

procurement-api/
  POST   /requisitions
  POST   /requisitions/{id}/select-supplier
  POST   /invoices
  GET    /purchase-orders
  GET    /purchase-orders/{id}
  GET    /exceptions
  POST   /exceptions/{id}/resolve

dashboard-gateway/
  GET    /dashboard/overview
  GET    /alerts
  WS     /ws/dashboard

dock-worker/       (no HTTP — background consume() loop, group="dock-worker")
match-worker/      (no HTTP — background consume() loop, group="match-worker")
```

16 REST endpoints + 1 WebSocket, across 3 HTTP-facing services + 2 headless
workers. Every write endpoint's transaction boundary, tables touched, and
emitted event(s) are specified above — nothing here required a change to
`schema.sql`, `redis-contract.md`, or `event_bus.py`.

---

## v5 ADDITIONS — AUTHENTICATION & ROLES (implemented)

Approved before implementation, per README §10. This section is the contract;
`backend/shared/auth.py` is the implementation of it.

Schema delta (additive only, applied by `backend/migrations/v5_auth.sql`):
four columns on `users` (`email`, `password_hash`, `is_active`,
`last_login_at`), one unique index `uq_users_email_lower`, one sequence
`user_id_seq`. **No new event types.** Auth is not a supply-chain domain
event, so nothing is added to `redis-contract.md` §3/§4 and nothing is
published to Redis; logins, signups and role changes are recorded in
`audit_logs` instead.

### §0. Enforcement model — applies to EVERY endpoint above

| Class | Requirement |
|---|---|
| Public | `GET /health`, `POST /auth/login`, `POST /auth/signup`, `GET /auth/roles`, `GET /track/{ref}`, `/docs`, `/redoc`, `/openapi.json` |
| Read | Any valid bearer token |
| Write | Valid bearer token **and** the endpoint's capability |

`Authorization: Bearer <jwt>`. HS256, signed with `JWT_SECRET`, which must be
identical across all three services — each verifies locally, so a token issued
by the gateway is accepted by Yard API and Procurement API without any
service-to-service call. `401` = no/expired/invalid token. `403` = valid token,
insufficient role.

Enforced by one middleware in `shared/api.py` (protected-by-default: a new
route is authenticated unless its path is added to the public allowlist), plus
an explicit `Depends(require(<capability>))` on each write endpoint.

### §1. Capability matrix

| Capability | operator | procurement | finance | admin |
|---|:--:|:--:|:--:|:--:|
| `yard:write` — shipments, trailers, tracking, arrive, dock, unload, reassign | ✅ | — | — | ✅ |
| `procurement:write` — requisitions, chat, select-supplier | — | ✅ | — | ✅ |
| `invoice:write` — invoice intake (JSON + OCR) | — | ✅ | ✅ | ✅ |
| `exception:assign` | — | ✅ | ✅ | ✅ |
| `exception:resolve` — override the match engine, creates a payment | — | — | ✅ | ✅ |
| `payment:write` — settle a payment | — | — | ✅ | ✅ |
| `alert:ack` | ✅ | ✅ | ✅ | ✅ |
| `admin:users` — change another user's role | — | — | — | ✅ |
| *(all reads)* | ✅ | ✅ | ✅ | ✅ |

`exception:resolve` and `payment:write` sit with finance, not procurement, on
purpose: resolving an exception creates a payment row, and separating who
orders goods from who pays for them is the standard segregation of duties.

`system` (USR-000) holds no capabilities and cannot sign in. It exists solely
as the recorded actor for touchless approvals (`payments.approved_by`).

### §2. Actor fields are taken from the token, not the request body

`POST /requisitions`, `POST /requisitions/chat` (`requested_by`) and
`POST /exceptions/{id}/resolve` (`resolved_by`) previously accepted the acting
user as a body field. Since v5 the server uses the authenticated caller and
**ignores** those fields, which remain accepted so pre-auth callers do not
break. A client-supplied actor id is spoofable, which would make the audit
trail on the single most consequential manual action in PR2 worthless.

### §3. New endpoints — all on Dashboard Gateway (the only token issuer)

#### `POST /auth/signup` — public
Request: `{ "name": "...", "email": "...", "password": "...", "role": "operator|procurement|finance" }`
Response `201`: `{ "token", "token_type", "expires_in", "user": { "id", "name", "email", "role", "permissions" } }`
- `admin` and `system` are not self-assignable → `422`. Duplicate email → `409`
  (also guaranteed by `uq_users_email_lower` under concurrency). Password < 8
  chars or > 72 bytes → `422`.
- **Writes:** `users`, `audit_logs` (`user_signup`). **Events:** none.

#### `POST /auth/login` — public
Request: `{ "email", "password" }` → same response shape as signup.
- Unknown email and wrong password return an identical `401` and both pay the
  same bcrypt cost, so neither response body nor timing enumerates accounts.
  Deactivated account → `403`. `system` role → `403`.
- **Writes:** `users.last_login_at`, `audit_logs` (`user_login`). **Events:** none.

#### `GET /auth/me`
Re-reads `users` rather than trusting token claims, so a deactivated or
re-roled account is caught at page load. Returns `role_changed: true` when the
database role differs from the token's — the token still carries the old role
and is what the services enforce on, so the UI must force a re-login.

#### `GET /auth/roles` — public
The self-signup role list, so the signup dropdown cannot drift from the server.

#### `GET /auth/users?role=`
The assignee directory behind "assign this exception to…", hence readable by
any authenticated user. `email`/`last_login_at` are returned only to `admin`.

#### `POST /auth/users/{user_id}/role` — requires `admin:users`
Request: `{ "role": "finance" }`. The only path to `admin`. Takes effect at the
target's next sign-in (their current token still carries the old role).
- **Writes:** `users.role`, `audit_logs` (`user_role_changed`, with old/new). **Events:** none.

#### `WS /ws/dashboard` — now authenticated
Token passed as `?token=<jwt>`, because the browser WebSocket API cannot set
headers and the HTTP middleware does not see WebSocket scopes. Validated
before `accept()`; rejected connections close with code `1008` and never join
the broadcast hub.

**Running total: 22 REST endpoints + 1 WebSocket.**

---

## v7 ADDITIONS — OUTBOUND OPERATIONS & THE AUTONOMOUS BRIDGE

Approved before implementation. Schema delta is additive-only and applied by
`backend/migrations/v7_outbound.sql` (three tables, three columns, seven
indexes, three sequences); event vocabulary delta is in `redis-contract.md` §3
(six new types, two new entity types, one new consumer group).

Two things ship here:

1. **Outbound**, the half of E2 that did not exist. A customer order becomes a
   pick plan, a truck comes to collect it, contends for the same doors as every
   inbound truck, loads, and leaves.
2. **The autonomous bridge**, which is the step the end-to-end workflow always
   described and the code never had: a PO being *confirmed by the supplier*,
   and a shipment coming into existence because of it rather than because a
   human called `POST /shipments`.

### The one rule that shapes all of it

Outbound reuses `trailers`, `tracking_events` and `dock_assignments`. There is
**no outbound dock endpoint, no outbound scheduler, and no outbound tracking
endpoint** — an outbound truck posts GPS to `POST /trailers/{id}/tracking`,
arrives via `POST /trailers/{id}/arrive`, and takes a door via
`POST /trailers/{id}/dock`, exactly as an inbound one does. Only the two ends
of the journey differ, and only those get new endpoints.

This is a direct consequence of CLAUDE.md's locked rule that dock assignment is
scheduling, not scoring: one optimiser plans one set of doors over one set of
trailers. A door is contended for by both directions *simultaneously*, so
planning them separately would let two trucks be promised the same door for the
same fifteen minutes.

### Yard API (E2) — outbound

| Endpoint | Purpose |
|---|---|
| `POST /outbound-orders` | **New.** Customer order + its pick lines. Body `{customer_name, destination_location_id, requested_ship_date, priority, lines:[{material_id, qty}]}`. Writes `outbound_orders` (`CREATED`→`PLANNED`) **and** its `load_plans` rows in one transaction. Emits `OUTBOUND_ORDER_CREATED` then `LOAD_PLAN_CREATED`, both `entity_type=outbound_order`. |
| `POST /outbound-orders/{id}/stage` | **New.** Warehouse picks to the staging lane. Body `{lines:[{load_plan_id, qty_staged}]}` — or empty body to stage every line in full, which is the simulator's path. Sets `load_plans.qty_staged`/`status` (`STAGED`, or `SHORT` when short-picked); when every line is resolved the order moves to `STAGED`. Emits `LOAD_STAGED`. |
| `POST /outbound-orders/{id}/dispatch` | **New.** A truck is assigned to collect the order. Writes an **outbound** `shipments` row (`direction='OUTBOUND'`, `po_id` NULL, `outbound_order_id` set) and its `trailers` row (`direction='OUTBOUND'`, `EN_ROUTE`) in one transaction. Emits `SHIPMENT_CREATED` then `TRAILER_DEPARTED` — the same pair an inbound dispatch emits, so `dock-worker` plans a door for it with no new subscription. `409` unless the order is `STAGED`: a door must never be committed to a load that is not picked. |
| `POST /trailers/{id}/load` | **New.** Loading complete — the outbound mirror of `/unload`, and the **only** writer of `goods_issues`. Body `{lines:[{load_plan_id, qty_loaded}]}` or empty for "load everything staged". Writes `goods_issues`, `trailers.status='LOADED'`, `load_plans.qty_loaded`/`LOADED`, releases the door (`dock_assignments → COMPLETED`), `outbound_orders.status='SHIPPED'`, `shipments.status='LOADED'`. Emits `GOODS_ISSUED`, `entity_type=goods_issue`. |
| `POST /trailers/{id}/deliver` | **New.** Confirmed at the customer. `trailers.status='DELIVERED'`, `shipments.status='DELIVERED'`, `outbound_orders.status='DELIVERED'`. Emits `OUTBOUND_DELIVERED`, `entity_type=outbound_order`. `409` unless the trailer is `DEPARTED`. |
| `GET /outbound-orders?status=` | **New.** Queue view: order, lines, staging progress, trailer, current dock assignment. |
| `GET /outbound-orders/{id}` | **New.** One order end to end — lines, shipment, trailer, full dock-assignment history, goods issue, and its `event_log` timeline. |
| `POST /trailers/{id}/depart` | **Amended guard, additive.** Was `UNLOADED → DEPARTED`. Now also `LOADED → DEPARTED` for outbound. The event is unchanged (`TRAILER_EXITED`); a gate is a gate. |
| `GET /yard-status` | **Amended response, additive.** Optional `?direction=INBOUND\|OUTBOUND` filter. Every trailer gains `direction`, and outbound ones gain `outbound_order_id`/`customer_name` where an inbound one carries `po_id`. `summary` gains `outbound_*` counters. Absent the query param the board shows **both**, which is the honest default — the yard is one yard. |

### Procurement API (PR2) — the confirmation step

| Endpoint | Purpose |
|---|---|
| `POST /purchase-orders/{id}/confirm` | **New.** The supplier accepts the PO. Body `{confirmed_delivery_date?, notes?}`. Writes `purchase_orders.status='CONFIRMED'` and stamps the acceptance into `terms.supplier_confirmation`. Emits `PO_CONFIRMED`. Requires `procurement:write`. `409` unless the PO is `CREATED`. |

`purchase_orders.status` gains `CONFIRMED` between `CREATED` and `SHIPPED`
(append-only, added to `schema.sql`'s comment list). `match-worker`'s
`SHIPMENT_CREATED → SHIPPED` reconciliation is unchanged and still fires,
because it advances from *whatever* pre-shipment state the PO is in.

### Supplier Agent (background — `consume()`, no HTTP surface)

`allowed_event_types` = `{"PO_CREATED"}`, group `supplier-agent`.

| Event | Handler action |
|---|---|
| `PO_CREATED` | Decide acceptance from the supplier's seeded `reliability_score` (deterministic, seeded by PO id — a demo must replay identically). Accept → call `POST /purchase-orders/{id}/confirm`, then `POST /shipments`, then `POST /shipments/{id}/trailers`, each over HTTP with a service token. Decline → raise an `alerts` row and leave the PO `CREATED` for a human. |

It writes **no domain table directly**. Every write goes through the owning
service's public endpoint, so the ownership boundaries in this document hold
under automation exactly as they hold under an operator — and the agent is
provably not a back door into E2's tables.

### Simulator (control surface, `POST /sim/*`)

Not a domain service: it drives the real HTTP APIs above and owns no tables.
`POST /sim/start`, `POST /sim/stop`, `GET /sim/status`, and
`POST /sim/scenario/{name}` for `delay-trailer`, `surge-arrivals`,
`block-dock`, `inject-price-mismatch`, `inject-missing-po`, `outbound-rush`.

### Capability matrix delta

| Capability | operator | procurement | finance | admin |
|---|:--:|:--:|:--:|:--:|
| `outbound:write` — outbound orders, staging, dispatch, load, deliver | ✅ | — | — | ✅ |

Outbound sits with `operator` because it is yard work: the people who move
trucks move them in both directions. It is a **separate** capability from
`yard:write` rather than folded into it, so a deployment that outsources
outbound to a 3PL can grant one without the other — which is the reason to have
a capability matrix at all.

`POST /purchase-orders/{id}/confirm` requires the existing `procurement:write`.

**Running total: 32 REST endpoints + 1 WebSocket, 3 HTTP services + 3 workers
+ 1 simulator.**

---

---

## v8 ADDITIONS

Additive only, per the v4 rule above — no endpoint changed method, URL, or
removed a field. Response additions are new keys on existing objects.

### Yard API

| Endpoint | Purpose |
|---|---|
| `GET /yard-status` | **Amended response, additive.** Each trailer gains `po_qty` — the ordered quantity from `purchase_orders.qty`, joined through `shipments.po_id`. `null` for outbound trailers (no PO) and for any inbound trailer whose shipment has no PO link. Read-only: it is the baseline a received count is a *variance against*, so the dock scanner can derive what it counted from the order instead of asserting a hardcoded number. **Reads** gains `purchase_orders`. No writes, no event. |

### Procurement API

| Endpoint | Purpose |
|---|---|
| `GET /invoices/{invoice_id}` | **Amended response, additive.** `match_result` gains `ai_narration` — the completed 3-way match written up as prose for the audit log, from `shared/llm.write_match_reasoning()`. `null` for every row matched before v8 (the migration deliberately does not backfill) and whenever no provider key is configured. **Reads** gains no table: it is a new column on `match_results`. No writes, no event. |

**`ai_narration` is a second rendering of the decision, never a second
decision.** `match_result.status` and `match_result.reason` remain
authoritative and are what every consumer keys on; the narration is generated
*after* `evaluate()` has returned and is handed the finished verdict. A client
must render `reason` unconditionally and treat the narration as an addition to
it — if the two ever disagree, `reason` wins (docs/3WAY_MATCH_POLICY.md).
`shared/match_policy.py` imports no model and must never start.

Written by match-worker on the same INSERT as the match result, inside the same
transaction. The call is synchronous and capped at
`MATCH_NARRATION_TIMEOUT_SECONDS` (12s), falling back to a deterministic
sentence built from `decision.reason` on timeout, refusal, empty completion, or
absent API key — so match-worker's behaviour, and the shape of this response,
are identical with and without a provider configured.

### Dashboard Gateway

| Endpoint | Purpose |
|---|---|
| `GET /dashboard/supplier-risk?limit=10` | **New.** Predictive invoice risk: open POs ranked by the money a forecast mismatch puts in doubt, plus the per-supplier scores behind the ranking. Authenticated (protected-by-default); read-only, no capability beyond a valid token. **Reads** `suppliers`, `purchase_orders`, `match_results`, `exceptions`, `invoices`, `materials`. No writes, no event. Recomputed per request — nothing is stored. |
| `WS /ws/track/{ref}` | **New.** Live deltas for ONE consignment, for the public customer tracker. **Public — no token**, exactly as `GET /track/{ref}` is (`auth.PUBLIC_PREFIXES`); the customer it exists for has no account. `{ref}` resolves by the same rules as the REST tracker (shared `_trailer_for_reference()`), and an unresolvable reference is refused at the handshake with close code `1008` rather than left open. **Reads** `trailers`, `shipments`, `purchase_orders`, `outbound_orders` (resolution only, once, at connect). No writes, no event, no new consumer group — it is fed by the existing `dashboard-ws` group. |

**The risk model, stated.** `/dashboard/supplier-risk` returns a smoothed base
rate, not a trained model, and the response says so by returning every input
beside every output. Per supplier:

```
prior = (house exception rate + suppliers.risk_score) / 2
score = (exceptions + k*prior) / (matched + k)          k = 5 (RISK_PRIOR_STRENGTH)
confidence = matched / (matched + k)
```

The observed rate pulled toward the prior in proportion to how little history
backs it. `k = 5` is the smallest value that stops a supplier whose only two
invoices both failed being published as "100% risk"; every supplier in the seed
has between 0 and 8 matched invoices, so unsmoothed rates would be noise. A
supplier with no matched invoice scores exactly the prior — not a flattering
zero — and reports `observed_rate: null`, `confidence: 0`.

The house rate is taken over **all** of `match_results`, not by summing the
per-supplier counts: an invoice arriving with no PO reference (`MISSING_PO`)
produces a `match_result` with `po_id NULL` that joins to no supplier, and
excluding it would flatter the average every prior is built from.
`baseline.attributed_to_a_supplier` reports the difference so the two sets of
numbers reconcile.

**Ranking is by money, not probability.** Risk is a property of the supplier —
nothing knowable before the invoice arrives separates two POs to the same
supplier — so what orders the list is exposure:

```
expected_impact = score x typical_exception_severity x qty*unit_price
```

`typical_exception_severity` is the **measured** median of
`exceptions.impact_amount / PO value` over every priced exception, returned with
its sample count. It is a median, not a mean: a `DUPLICATE_INVOICE` bills an
order twice and exceeds 100% while a price slip sits near 6%, and one duplicate
would treble a mean. Without this term the only rupee figure available would be
`score x whole PO value`, which asserts the entire order is at stake when the
typical mismatch disputes a fraction of it. It is `null` — and every rupee
figure with it — until one exception has been priced, on the same principle
that makes `GET /kpi/model-performance` 404 before an eval run.

`invoice_received` is a flag, never a multiplier: the invoice is already in and
awaiting match, so the risk is *imminent*, not larger.

**Frames.** `{"type":"hello","trailer_id":…,"note":…}` once at connect, then
`{"type":"update","event_type":…,"timestamp":…}` per delta. Deliberately no
`entity_id` and no `payload`: the client re-reads `GET /track/{ref}`, which
stays the single authority on what a customer may be shown, so this socket
cannot disclose anything the public REST endpoint does not already.

**Filtered twice.** To one trailer — matched on `entity_id` for
`entity_type = trailer` events and on `payload.trailer_id` for the rest
(`GOODS_RECEIVED` is a `goods_receipt`, `DOCK_ASSIGNED` a `dock_assignment` —
redis-contract.md §4) — and to the consignment's own vocabulary:
`TRAILER_DEPARTED`, `TRAILER_LOCATION_UPDATED`, `ETA_UPDATED`,
`TRAILER_ARRIVED`, `DOCK_ASSIGNED`, `DOCK_REASSIGNED`, `DOCK_DELAYED`,
`TRAILER_DOCKED`, `GOODS_RECEIVED`, `GOODS_ISSUED`, `TRAILER_EXITED`. Nothing
from PR2 crosses it.

**Why not `/ws/dashboard`.** That rail requires a token the customer does not
have, and carries every event in both domains — purchase orders, invoices,
supplier scores, payments. Sending that to a browser opened by someone outside
the company is a disclosure whether or not the screen renders it.
