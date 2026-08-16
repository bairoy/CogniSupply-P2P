# Backend Workers (Async Processing)

Workers listen to the Redis Event Bus and perform heavy background processing. By separating them from the REST APIs, the web requests remain extremely fast.

## 1. Dock Worker (`backend/services/dock_worker/`)
**Purpose:** Solves the E2 requirement of assigning dock doors intelligently.

**How it works:**
- It listens for the events that change the schedule: `SHIPMENT_CREATED`, `TRAILER_DEPARTED`, `ETA_UPDATED`, `TRAILER_LOCATION_UPDATED`, `TRAILER_ARRIVED`, `TRAILER_DOCKED`, `GOODS_RECEIVED`, `GOODS_ISSUED`.
- When a change occurs, it pulls the current state of the yard and passes it to the `dock_engine`.
- **The Engine (`shared/dock_engine.py`):** Uses Google OR-Tools (Constraint Programming solver / CP-SAT) to mathematically find the optimal door assignment for every truck, minimizing wait times while respecting constraints (e.g., refrigerated trucks must go to reefer doors).
- It then writes the resulting `dock_assignments` to the database.

**Interview Tip:** If asked about door scheduling, emphasize that you used a mathematically rigorous Operations Research solver (OR-Tools) rather than simple if/else statements.

## 2. Match Worker (`backend/services/match_worker/`)
**Purpose:** Solves the PR2 requirement of automated 3-way matching.

**How it works:**
- It listens for `INVOICE_RECEIVED`, `GOODS_RECEIVED` and `SHIPMENT_CREATED`. (It is also the PR2-side status reconciler: `SHIPMENT_CREATED` → PO `SHIPPED`, `GOODS_RECEIVED` → `RECEIVED`/`PARTIALLY_RECEIVED`.)
- When it has all three documents (PO, Goods Receipt, Invoice), it passes them to the `match_policy`.
- It executes a strict, deterministic evaluation (checking quantity and price variances against fixed tolerances, e.g., 2% for qty, 3% for price).
- If it passes, it inserts an `APPROVED` payment and moves the PO to `MATCHED`. If it fails, it opens an `EXCEPTION` for the review queue (unassigned — a person picks it up).
- **v8 — AI audit note.** *After* `evaluate()` has returned, it calls
  `shared/llm.write_match_reasoning()` and stores the prose in
  `match_results.ai_narration`, in the same INSERT. The model is handed the
  finished verdict; it cannot reach the decision. The call is capped at 12s and
  falls back to a deterministic sentence, so the worker behaves identically
  with and without an API key.

## 3. Supplier Agent (`backend/services/supplier_agent/`)
**Purpose:** Automates supplier selection and PO generation for PR2.

**How it works:**
- Listens for `PO_CREATED` — one event, exactly as `redis-contract.md` §5 specifies. It is the *supplier* side of the chain: a PO appears, the supplier decides whether to accept it, and if so the shipment and its trailer come into being — at which point dock-worker takes over on `TRAILER_DEPARTED`.
- Supplier choice itself is scored by `shared/procurement_scoring.py` on 5 factors: **Price, Quality, Reliability, Lead Time, Risk**. Every factor is a real column on `suppliers`; there is no carbon term.
- It uses AI to write a natural language justification of *why* the supplier won the bid.
- It then drives the **real HTTP APIs** to confirm the PO and create the shipment + trailer. It holds a database connection and could INSERT directly; it may not. Driving the same endpoints an operator would is what stops the automation drifting from the contract the system is tested against.
