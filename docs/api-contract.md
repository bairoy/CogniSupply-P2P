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
- **Consumed by:** `dashboard-ws` only (no worker subscribes to this per the locked contract)

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
Response `200`: `{ "old_assignment_id": "DA-5521", "new_assignment_id": "DA-5522", "status": "ASSIGNED" }`
- **Writes (history-preserving — locked pattern, see note below):** UPDATE the existing `dock_assignments` row `SET status = 'REASSIGNED'`; INSERT a new `dock_assignments` row for the same `trailer_id` with the new `dock_id`, `status = 'ASSIGNED'`
- **TX:** both writes + `record_event(DOCK_REASSIGNED)`, commit together — `entity_id` is the **new** assignment's id; `payload` includes `old_assignment_id` for traceability
- **Publish:** `DOCK_REASSIGNED`
- **Consumed by:** `dashboard-ws` only

> **Locked reassignment pattern, applies everywhere a dock gets reassigned** (this manual endpoint *and* `dock-worker`'s automatic `ETA_UPDATED` path below): never `UPDATE` a `dock_assignments` row's `dock_id` in place. Always mark the old row `REASSIGNED` and `INSERT` a new one `ASSIGNED`. This makes `dock_assignments` a genuine history table — `GET /trailers/{id}` can show the full "D4 → D2 → D4" story, which is a real demo moment (see README §9's judge-question example), and it would be silently lost by an in-place update.

### `GET /yard-status`
Dashboard's E2 initial-load read (REST — see README §5 on why this exists separately from the WebSocket). Each trailer's `dock_assignment` shows the **current** one only — `WHERE trailer_id = ... AND status = 'ASSIGNED'` (there is exactly one such row per trailer at any time, by construction of the locked reassignment pattern above; `REASSIGNED` rows are history, not current state).

Response:
```json
{ "trailers": [{ "id": "TRL-3391", "status": "EN_ROUTE", "eta": "...",
                 "dock_assignment": { "dock_id": "DOCK-04", "status": "ASSIGNED" } }],
  "docks": [{ "id": "DOCK-04", "yard_position": 4, "occupied": false }] }
```
- **Reads:** `trailers`, `dock_assignments`, `docks`, `shipments`. No writes, no event.

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

`allowed_event_types` = `{"SHIPMENT_CREATED", "TRAILER_DEPARTED", "ETA_UPDATED", "TRAILER_LOCATION_UPDATED"}` (exactly `redis-contract.md` §5)

| Event | Handler action |
|---|---|
| `SHIPMENT_CREATED` | No-op. No trailer row exists yet to score a dock for — real action waits for `TRAILER_DEPARTED`. Still claimed via `processed_events` (cheap), just does nothing. |
| `TRAILER_DEPARTED` | **Initial dock scoring.** Reads `docks` (availability, `compatible_load_types`), `trailers` (priority, load_type, eta). Writes `dock_assignments` (status=`ASSIGNED`). `record_event(DOCK_ASSIGNED)` in the same TX as the write, commit together, then `publish_to_redis()`. |
| `TRAILER_LOCATION_UPDATED` | Tracking only, per §9 — no domain write, no event emitted. Claimed and acked, nothing else. |
| `ETA_UPDATED` | Re-score **only if** the new ETA differs from the ETA last used for scoring by ≥10 min (§9). If triggered: re-run scoring. If the result picks a different dock: apply the **locked reassignment pattern** — mark the current `dock_assignments` row `REASSIGNED`, insert a new one `ASSIGNED`, `record_event(DOCK_REASSIGNED)` referencing the new row's id. If the result keeps the same dock but the trailer will now miss its window: write an `alerts` row instead, `record_event(DOCK_DELAYED)` + `record_event(ALERT_CREATED)`. Whichever branch: one transaction, commit once. |

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
