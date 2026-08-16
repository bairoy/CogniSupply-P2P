# Backend Services (APIs)

This document explains the three core API services and the simulator.

## 1. Yard API (`backend/services/yard_api/`)
**Purpose:** Owns all physical logistics data.
**Domain Models:** `trailers`, `shipments`, `locations`, `outbound_orders`, `docks`, `dock_assignments`, `tracking_events`, **`goods_receipts` and `goods_issues`**.

**Key Concepts:**
- This API only exposes standard CRUD endpoints for yard operations.
- When a trailer's status changes (e.g., `POST /trailers/{id}/arrive`), it updates the database and publishes an `ETA_UPDATED` or `TRAILER_ARRIVED` event.
- It **never** talks to the Procurement API.

## 2. Procurement API (`backend/services/procurement_api/`)
**Purpose:** Owns all financial and inventory documentation.
**Domain Models:** `requisitions`, `purchase_orders`, `invoices`, `match_results`, `exceptions`, `payments`.

> **`goods_receipts` is NOT owned here.** It is written *only* by Yard API's
> `POST /trailers/{id}/unload`; Procurement reads it to complete the 3-way
> match and never writes it. Its outbound twin `goods_issues` is written only
> by `POST /trailers/{id}/load`. This is a locked decision — the physical
> record of what arrived belongs to the service that was standing at the door.

**Key Concepts:**
- Handles the creation of POs from NLP intents.
- Handles the OCR processing of invoices via AI.
- Exposes exception management endpoints for humans to resolve 3-way match failures.

## 3. Dashboard Gateway (`backend/services/dashboard_gateway/`)
**Purpose:** The "Backend For Frontend" (BFF).
**Domain Models:** Read-only access to all tables.

**Key Concepts:**
- **No Domain Ownership:** The gateway is strictly forbidden from writing to tables like `trailers` or `purchase_orders`. It only writes to purely UI-driven tables (like `alerts`).
- **Union Queries:** If the frontend needs a unified "Exceptions Command Center" showing both Yard Delays and Invoice Mismatches, the Gateway writes a SQL `UNION` across the Yard and Procurement tables and returns a standardized list. This prevents the frontend from making 5 different API calls.
- **WebSockets — two rails, one consumer group.** `/ws/dashboard` is
  token-gated and carries every event in both domains, for signed-in staff.
  `/ws/track/{ref}` is **public** and carries deltas for exactly one
  consignment, for the customer tracker — it filters to that trailer and to an
  11-event customer vocabulary, and carries no ids or payloads, so it cannot
  disclose anything `GET /track/{ref}` does not already. Both are fed by the
  single `dashboard-ws` consumer group; a second group would be a second
  `processed_events` claim on every event for no gain.
- **Analytics reads (v8):** `GET /dashboard/supplier-risk` ranks open POs by
  forecast invoice risk, and `GET /kpi/model-performance` serves the eval
  harness's last run (404 until it has been run).

## 4. Simulator (`backend/services/simulator/`)
**Purpose:** Acts as the "physical world" and external actors.

**Key Concepts:**
- **Honest Simulation:** The simulator does not directly update the database using SQL. It makes real HTTP `POST` requests to the Yard and Procurement APIs, exactly as a real 3rd-party logistics provider, WMS, or IoT device would.
- **Deterministic Randomness:** To ensure the demo is reproducible for judges, the "random" errors (like an invoice having a 5% price variance) are seeded based on the PO ID. `PO-1042` will always fail the exact same way every time you run the demo, making it predictable for your pitch.
- **Ticks:** It runs in a loop (3s), advancing time and triggering events — trucks moving, gating in, docking, unloading, gating out, invoicing, **settling approved payments (v8)**, and the whole outbound pick/stage/dispatch cycle.
- **It starts paused.** `POST /sim/start` begins the loop; `GET /sim/status` shows ticks and a per-action tally. Restarting the process resets it to paused.
- **Auto-pay closes the loop (v8):** match-worker approves a clean match, but
  until v8 nothing ever *paid* it, so a fully touchless PO stopped at `MATCHED`.
  `_advance_payments()` POSTs `/payments/{id}/pay` for anything approved 2+
  minutes ago — a POST, not an UPDATE, because paying also closes the PO and
  emits both `PAYMENT_PAID` and `PO_STATUS_CHANGED` in one transaction.
