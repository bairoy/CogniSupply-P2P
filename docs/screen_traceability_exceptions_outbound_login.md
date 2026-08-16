# Screen Documentation: Traceability — Full P2P Audit Trail

## What This Screen Is For
This screen answers the question: **"What happened to PO-1042 from start to finish?"**

It gives a complete chronological audit trail of every event that happened to a Purchase Order — from when the requisition was raised, through supplier selection, shipment, goods receipt, invoice, 3-way match, to payment. Every document linked to the PO is listed.

> **Route:** `/traceability` (list) and `/traceability/:poId` (detail)  
> **Who sees it:** All logged-in users (read-only)  
> **Use case:** Both E2 and PR2 — audit, traceability, compliance

---

## Why This Screen Exists

In a real supply chain audit, an accountant or compliance officer needs to answer: *"Show me every step that happened for this purchase order, and who was responsible at each step."* This screen provides that. Every domain action in the system is recorded as a timestamped event in the `event_log` table, linked to the entity it happened to. The Traceability screen assembles all those events into a timeline view.

---

## Mode 1: PO History List

**What it shows:** A table of all Purchase Orders with:
- PO ID, Supplier name, Material, Qty, Value, Status, Expected delivery date, GRN ID, Invoice ID

This is the "browse all POs" view. Clicking any row navigates to `/traceability/{po-id}` for the full trail.

**How it works:** Calls `GET /purchase-orders` which returns the full list with related document IDs pre-joined.

---

## Mode 2: Full Trail Detail

When a PO ID is in the URL, the screen fetches `GET /traceability/{poId}` from the gateway. This single endpoint returns:

1. **The Purchase Order** (root document)
2. **Related Entities** (every document that was created as part of this PO's lifecycle)
3. **Timeline** (every event that touched any of those documents, in chronological order)

### Sub-component A: Related Documents Panel

Groups all related documents by type and shows them as clickable chips:

- `Requisition` → links to the requisition that triggered the PO
- `Purchase Order` → the PO itself
- `Shipment` → the shipment created by the supplier agent
- `Vehicle` → the trailer that carried the goods
- `Dock Assignment` → which door was assigned
- `Goods Receipt Note (GRN)` → the physical receipt from the IoT scanner
- `Supplier Invoice` → links to `/match-pay/{invoice-id}`
- `3-Way Match Result` → the decision record
- `Exception` → if one was raised
- `Payment` → the payment record

**Interview tip:** *"The related entities are linked by FK in the database. The traceability endpoint does a graph traversal — starting from the PO, it finds the shipment, the trailer, the goods receipt, the invoice, the match result. It's essentially building a document graph and returning all nodes."*

### Sub-component B: Event Timeline

A chronological list of every event that happened, with:
- Timestamp
- Event type (e.g., `PO_CREATED`, `TRAILER_ARRIVED`, `MATCH_COMPLETED`, `PAYMENT_APPROVED`)
- The entity it happened to (e.g., "trailer TRL-00042")
- A summary (if available)

**The key design principle:** The timeline uses the `event_log` table — not reconstructed from status fields. This means the history is immutable. You cannot go back and change when a PO was created; the event record is permanent. This is what makes it a true audit trail.

---

## Data Flow

```
User opens /traceability/PO-1042
      │
      ▼
GET /traceability/PO-1042 (gateway endpoint)
  → looks up PO
  → follows FKs to: shipment, trailer, dock_assignment,
                    goods_receipt, invoice, match_result,
                    exception, payment
  → fetches all events for all those entity IDs
  → returns: { purchase_order, related_entities: [...], timeline: [...] }
      │
      ▼
Frontend groups related_entities by type → renders linked chips
Frontend renders timeline in chronological order
```

---

# Screen Documentation: Exceptions — Exceptions Command Center

## What This Screen Is For
This is where human reviewers go to resolve problems the system couldn't handle automatically. Think of it as the "inbox" for exceptions — 3-way match failures, duplicate invoices, and dock delays all appear here.

> **Route:** `/exceptions`  
> **Who sees it:** Finance managers, Procurement roles, Admins  
> **Use case:** PR2 — exception management

---

## What an Exception Is

When the `match_worker` runs the 3-way match and the invoice falls **outside tolerance**, instead of approving the payment, it creates an `exception` record. The exception captures:
- Which invoice and PO are involved
- What type of problem occurred (`QTY_MISMATCH`, `PRICE_MISMATCH`, `MISSING_PO`, `DUPLICATE_INVOICE`)
- The severity (`medium`, `high`, `critical`)
- The financial impact amount
- Current status (`OPEN`, `RESOLVED`)

**The Exceptions Command Center unifies exceptions and alerts** — dock delays and conflicts (from the yard) appear alongside invoice mismatches (from procurement) in one prioritized queue. This is the "single pane of glass" for anything in the system that needs human attention.

---

## Components of the Exceptions Screen

### 1. Summary Tiles (Top Row)
Shows the count of exceptions by status and severity:
- Open / Resolved / Total
- Critical / High / Medium counts

### 2. The Unified Queue Table

Each row has:
- **Reference** — the PO or Trailer ID (clickable, links to the relevant detail screen)
- **Type** — `QTY_MISMATCH`, `PRICE_MISMATCH`, `DOCK_DELAYED`, etc. (displayed as a badge)
- **Severity** — `critical` (red), `high` (orange), `medium` (yellow)
- **Impact** — the financial amount at stake (e.g., ₹73,200 overbilled)
- **Age** — how long the exception has been open (e.g., "3 hours ago")
- **Owner** — who is assigned (or "Unassigned")
- **Resolve button** — calls `POST /exceptions/{id}/resolve`

**The dual-source design:** The queue is built by a `UNION` SQL query in the gateway:
```sql
SELECT ... FROM exceptions          -- match failures (PR2 domain)
UNION ALL
SELECT ... FROM alerts WHERE ...    -- dock delays, conflicts (E2 domain)
```
Neither table knows about the other. The gateway is the only place that combines them, and it does so without writing to either table. This is CQRS read-side aggregation in practice.

### 3. Resolve Action

When a finance manager clicks "Resolve" on an exception:
- They can optionally enter a resolution note.
- `POST /exceptions/{id}/resolve` is called.
- The exception status changes to `RESOLVED`.
- A `EXCEPTION_RESOLVED` event fires.
- The match_worker can then allow the payment to proceed (depending on the exception type and resolution).

---

# Screen Documentation: Outbound — Customer Order Management

## What This Screen Is For
This manages the **outbound flow** — goods leaving the warehouse to customers. It's the mirror of the inbound E2 use case.

> **Route:** `/outbound`  
> **Who sees it:** Operators and Admins  
> **Use case:** E2 v7 — Outbound Order to Delivery

---

## Key Components

### 1. Outbound Orders Table
Shows all customer orders with: Order ID, Customer name, Priority, Status, Requested ship date, and actions.

**Order statuses flow:** `PLANNED` → `STAGED` → `LOADING` → `DELIVERED`
- `PLANNED` — order placed, not yet picked from warehouse
- `STAGED` — goods picked and staged in the loading lane
- `LOADING` — truck arrived, goods being loaded
- `DELIVERED` — truck has delivered to customer

### 2. Create Outbound Order Form
A form to create a new customer order:
- Customer name
- Destination location (selected from warehouse locations excluding the warehouse itself)
- Priority (`low`, `normal`, `high`, `critical`)
- Requested ship date
- Line items (material + quantity)

Calls `POST /outbound-orders`.

### 3. Action Buttons
- **Stage** (on PLANNED orders) — calls `POST /outbound-orders/{id}/stage` — marks goods as picked
- **Dispatch** (on STAGED orders) — calls `POST /outbound-orders/{id}/dispatch` — assigns a carrier and creates an outbound shipment/trailer

**The same CP-SAT scheduler handles outbound:** When an outbound truck is created, the dock worker treats it identically to an inbound truck — it competes for the same pool of dock doors. The scheduler plans it in the same solve, ensuring inbound receiving and outbound loading don't conflict.

### 4. Live Shipment Map
Same `TrailerMap` component as the ControlTower — shows outbound trucks in transit to customers.

---

# Screen Documentation: Login

## What This Screen Is For
The login page. Simple, but a few details worth knowing for interviews.

> **Route:** `/login`  
> **Who sees it:** Everyone (before authentication)

---

## Key Details

### How Authentication Works
1. User enters email + password.
2. `POST /auth/login` is called.
3. The backend looks up the user in `users` table, verifies the bcrypt password hash.
4. Returns a **JWT (JSON Web Token)** signed with `JWT_SECRET`.
5. The frontend stores the JWT in `localStorage`.
6. Every subsequent API call includes the JWT in the `Authorization: Bearer {token}` header.
7. Each backend service verifies the JWT locally using the shared `JWT_SECRET` — no session database, no auth service call.

**Interview tip:** *"We use stateless JWT authentication. There is no session table in the database. Each service verifies the token using a shared secret. This means any service can verify a user's identity and role without a network hop to an auth server."*

### The Demo Account Buttons
The login screen deliberately shows **no** seeded-account panel. The four demo
accounts (`baiju@` admin, `shubham@` operator, `sachin@` procurement, `serohn@`
finance, all on `inbound2026`) still exist and whoever is driving the demo knows
them — they are simply not printed on the screen.

**Why:** this screen is the product's front door, and a visible list of logins
next to their shared password is the one element on it that would read as a test
fixture rather than a product. Role-based access control is demonstrated by
signing in as each role, not by advertising the roster.

### Role-Based Routing After Login
After login, the app checks the user's role and routes them to the appropriate home screen:
- `operator` → `/yard-dock`
- `procurement` → `/procurement`
- `finance` → `/match-pay`
- `admin` → `/` (ControlTower)
