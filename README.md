# CogniSupply P2P

> **Cognizant NPN_SCM Hackathon — Combination 2 (E2 + PR2)**
>
> An integrated, AI-augmented Supply Chain platform combining real-time yard logistics with autonomous end-to-end Procure-to-Pay.

---

## Table of Contents

1. [What This Project Does](#what-this-project-does)
2. [System Architecture](#system-architecture)
3. [Tech Stack](#tech-stack)
4. [Prerequisites](#prerequisites)
5. [Quick Start](#quick-start)
6. [Demo Accounts](#demo-accounts)
7. [How to Run a Demo](#how-to-run-a-demo)
8. [Data Flow Explained](#data-flow-explained)
9. [Project Structure](#project-structure)
10. [API Reference](#api-reference)
11. [Key Design Decisions](#key-design-decisions)

---

## What This Project Does

CogniSupply P2P solves **two critical supply chain problems** in one integrated platform:

### E2 — "Where's My Truck?" Yard, Dock & Delivery Tracker

Warehouse teams and customers lack a single real-time view of inbound/outbound truck movement, yard location, and dock-door assignments. CogniSupply provides:

- **Real-time truck map** with live GPS position, ETA, and delivery progress.
- **Automated dock-door scheduling** using Google OR-Tools (CP-SAT constraint solver) to optimally assign trucks to doors based on priority, load type, and ETA.
- **Instant re-planning** when a truck is delayed — the entire yard schedule re-optimizes in milliseconds.
- **Customer-facing tracker** — any customer can look up their delivery by tracking number, trailer ID, or order reference with no login required. It renders a real Mapbox route (not a straight line) and holds its own public WebSocket, so an ETA slip reaches the customer's browser without a poll cycle.

### PR2 — Autonomous Procure-to-Pay

The end-to-end procurement cycle (Requisition → PO → Shipment → Goods Receipt → Invoice → 3-Way Match → Payment) runs without human involvement on the happy path:

- **Conversational NLP** — employees describe what they need in plain English; AI extracts structured intent.
- **AI-driven supplier selection** — a 5-factor scoring model (Price, Quality, Reliability, Lead Time, Risk) selects the best supplier automatically. Every factor is a column in `suppliers`; the weights live in `shared/procurement_scoring.py`.
- **Intelligent OCR invoice capture** — LLM vision reads supplier invoice images and extracts data with confidence scores.
- **Automated 3-way matching** — deterministic policy checks PO quantity/price vs. Goods Receipt vs. Invoice within defined tolerances.
- **AI audit note** — once the deterministic policy has decided, an LLM writes the verdict up as prose a clerk can paste into an audit log. It narrates the decision; it can never change one.
- **Auto-payment** — an approved match is settled without a human: match-worker approves it, and the simulator releases it a short delay later, which also closes the PO. Exceptions go to a human review queue.
- **Predictive invoice risk** — open POs are ranked by the money a forecast mismatch puts in doubt, from each supplier's own exception history.
- **Measured, not claimed** — `backend/eval/run_eval.py` scores the match engine as a 5-class classifier and the NLP parser against a hand-labelled fixture. `GET /kpi/model-performance` 404s until it has actually been run.

**The two use cases connect at one critical point:** A trailer's `GOODS_RECEIVED` event (written by E2/Yard) unlocks the 3-way match in PR2. This is the integration that makes this one platform, not two apps sharing a dashboard.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        FRONTEND  (React + Vite, :5173)                      │
│   YardDock  │  ControlTower  │  MatchPay  │  Track (public)  │  Chat (NLP) │
└──────────────────────┬────────────────────────────┬────────────────────────-┘
                       │ REST                        │ WebSocket (real-time)
                       ▼                             ▼
┌──────────────────────────────────────────────────────────────────────────--─┐
│                    DASHBOARD GATEWAY  (:8003)                                │
│   Read-only BFF — aggregates cross-domain queries, pushes WS events to UI   │
└────────────┬─────────────────────────────────────┬──────────────────────────┘
             │ REST                                 │ REST
             ▼                                     ▼
┌───────────────────────┐             ┌─────────────────────────┐
│     YARD API (:8001)  │             │  PROCUREMENT API (:8002) │
│  trailers             │             │  purchase_orders         │
│  shipments            │             │  invoices                │
│  docks                │             │  match_results           │
│  outbound_orders      │             │  payments / exceptions   │
│  goods_receipts (own) │             │                          │
└───────────┬───────────┘             └────────────┬─────────────┘
            │                                      │
            │     Both write to PostgreSQL         │
            │     and publish events to Redis      │
            ▼                                      ▼
┌──────────────────────────────────────────────────────────────────────────--─┐
│           PostgreSQL 16 (:5435)           +          Redis 7 (:6379)         │
│           Single source of truth                 Event Bus (Streams)         │
└───────────────────────────────────────┬──────────────────────────────────--─┘
                                        │ Event subscription
                                        ▼
┌──────────────────────────────────────────────────────────────────────────--─┐
│                         BACKGROUND WORKERS                                   │
│                                                                              │
│  dock_worker    — OR-Tools CP-SAT dock scheduling (re-plans on every ETA)    │
│  match_worker   — deterministic 3-way match: PO × GR × Invoice               │
│  supplier_agent — autonomous supplier scoring, PO creation, confirmation     │
└────────────────────────────────┬─────────────────────────────────────────--─┘
                                 │ HTTP calls to Yard + Procurement APIs
                                 ▲
┌──────────────────────────────────────────────────────────────────────────--─┐
│                          SIMULATOR  (:8004)                                  │
│  Drives the demo: moves trucks, submits invoices, generates GPS ticks        │
│  Acts as the physical world (trucks, WMS feeds, suppliers)                   │
│  ONLY makes HTTP calls — never writes to the DB directly                     │
└──────────────────────────────────────────────────────────────────────────--─┘
```

### Core Architectural Patterns

| Pattern | How It's Used Here |
|---|---|
| **Event-Driven Microservices** | Services communicate via Redis Streams. No synchronous service-to-service calls for state changes. |
| **CQRS** | `yard_api` and `procurement_api` own all writes. `dashboard_gateway` only reads — it never writes to domain tables. |
| **Transactional Outbox** | Every write records the event in Postgres *in the same transaction*. A background thread publishes to Redis. Guarantees zero lost events, even on crash. |
| **AI Outside the Decision Boundary** | LLMs are used for extraction (OCR, NLP) and narration only. Business decisions (3-way match, dock scheduling, supplier scoring) are 100% deterministic, auditable code. |

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Database** | PostgreSQL 16 |
| **Event Bus** | Redis 7 Streams |
| **API Framework** | Python 3.12 + FastAPI |
| **Dock Scheduling Engine** | Google OR-Tools CP-SAT (constraint programming solver) |
| **AI / LLM** | Anthropic Claude OR OpenAI GPT (auto-selected from environment) |
| **Invoice OCR** | LLM Vision API with structured output + confidence scores |
| **Frontend** | React 18 + Vite + Tailwind CSS |
| **Mapping** | Mapbox GL JS (`react-map-gl`) — WebGL vector tiles + Mapbox Directions API for real road routes |
| **Real-time Updates** | Native WebSocket — two rails: `/ws/dashboard` (authenticated, all events) and `/ws/track/{ref}` (public, one consignment) |
| **Authentication** | Stateless HS256 JWTs (PyJWT) + bcrypt password hashes |
| **Infrastructure** | Docker Compose (Postgres + Redis containers) |

---

## Prerequisites

Before starting, ensure you have:

- **Docker Desktop** — [Install here](https://www.docker.com/products/docker-desktop) (for PostgreSQL and Redis)
- **Python 3.12+** — [Install here](https://www.python.org/downloads/)
- **Node.js 20+ and npm** — [Install here](https://nodejs.org/)

---

## Quick Start

### 1. Clone the repository
```bash
git clone <repo-url>
cd cognizant
```

### 2. Set up environment variables
```bash
cp .env.example .env
```

Open `.env` and fill in at least one LLM provider key:
```env
OPENAI_API_KEY=sk-proj-...       # OR
ANTHROPIC_API_KEY=sk-ant-...     # Set one of these for AI features
```

> **Note:** The system degrades gracefully if no API key is set. All non-AI features (dock scheduling, 3-way matching, etc.) still work. The system reports `"ai_available": false` in relevant endpoints.

### 3. Install Python dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
```

### 4. Install frontend dependencies
```bash
cd frontend/app
npm install
cd ../..
```

### 5. Start the entire stack
```bash
./run.sh start
```

This single command:
1. Starts PostgreSQL 16 on port `5435` and Redis 7 on port `6379` via Docker
2. Applies the full database schema (`backend/schema.sql`)
3. Seeds all demo data (accounts, suppliers, materials, locations, trucks)
4. Starts all 4 API services and 3 background workers
5. Starts the Vite frontend dev server

**Open the app:** [http://localhost:5173](http://localhost:5173)

> **If you see a `401 Unauthorized` error:** The database exists but demo accounts weren't seeded.
> Run `./run.sh reseed` to rebuild and seed from scratch.

### Useful Commands

```bash
./run.sh stop      # Stop all app processes (keeps Postgres + Redis running)
./run.sh status    # Check which services are up
./run.sh logs      # Tail all service logs simultaneously
./run.sh reseed    # Wipe database and re-seed from scratch
./run.sh migrate   # Apply pending migrations without wiping data

./demo.sh          # Demo remote control — one word per demo moment, see below
```

---

## Demo Accounts

All accounts use the password: **`inbound2026`**

| Email | Role | Access |
|---|---|---|
| `baiju@cognisupply.in` | `admin` | Full access to everything |
| `shubham@cognisupply.in` | `operator` | Yard operations: move trucks, trigger docking, run IoT scanner |
| `sachin@cognisupply.in` | `procurement` | Submit NLP requisitions, review supplier bids |
| `serohn@cognisupply.in` | `finance` | Review invoice exceptions, release payments |

> One account per login role, on purpose — a second account in the same role
> demonstrates nothing the first one does not. `USR-000` also exists but is not
> an identity anyone signs in as: it is the service account that touchless
> approvals are attributed to, so an automated payment is never signed with a
> real person's name.

**Public tracker (no login needed):** Visit `/track/<any-trailer-or-tracking-id>` directly in the browser.

**Global Search:** Use the Cmd+K (or Ctrl+K) search in the app header to look up any trailer ID, PO number, tracking number, invoice, or exception by ID.

---

## How to Run a Demo

### Step 1: Start the simulation

The simulator boots **paused** on purpose, so a demo can begin on a still
dashboard rather than mid-flight. Two ways to start it:

1. Log in as `baiju@cognisupply.in` (admin) and press **Start feed** in the
   sidebar — the button also shows tick count and last-tick time, so you can
   see at a glance whether the feed is alive.
2. Or from a terminal: `./demo.sh feed on`

Either way the system then runs itself:
   - Trucks move toward the warehouse (GPS ticks update every few seconds)
   - Dock doors are assigned and re-planned by the OR-Tools solver
   - Trucks arrive, dock, get scanned (IoT goods receipt), and depart
   - Supplier invoices arrive (via simulated OCR)
   - 3-way matching runs and auto-approves or escalates to exceptions

### Step 2: Trigger specific demo moments

`./demo.sh <word>`. Scenarios are authenticated POSTs and the simulator
registers no OpenAPI security scheme, so `:8004/docs` has no **Authorize**
button and cannot fire them — that is precisely why this script exists.

| Command | What It Demonstrates |
|---|---|
| `./demo.sh delay` | ETA slip triggers a full yard re-plan and a DELAY alert |
| `./demo.sh surge` | Scheduler queues a burst of trucks by priority, not first-come |
| `./demo.sh block` | A door goes out of service; solver routes around it |
| `./demo.sh unblock` | Restored doors are picked up on the next re-plan cycle |
| `./demo.sh rush` | Critical outbound orders compete with inbound trucks for doors |
| `./demo.sh price` | Invoice over PO price → exception, not payment |
| `./demo.sh qty` | Quantity variance outside tolerance → exception queue |
| `./demo.sh missing-po` | Unreadable PO reference → MISSING_PO exception for human |

Each maps to `POST /sim/scenario/{name}` and every one is a real state change
through a real endpoint, not a display trick: `delay` genuinely posts a late
ETA and the dock worker genuinely re-solves around it.

Two more worth knowing mid-demo: `./demo.sh feed off` freezes the world exactly
where it is while you talk, and `./demo.sh step` advances it one tick at a time
so you can narrate a single truck instead of watching nineteen.

### Step 3: Measure it, don't claim it

```bash
./.venv/bin/python backend/eval/run_eval.py          # free: database only
./.venv/bin/python backend/eval/run_eval.py --nlp    # + 30 live parse calls
./.venv/bin/python backend/eval/run_eval.py --all
```

Writes `backend/eval/eval_results.json`, which `GET /kpi/model-performance`
then serves. Before the first run that endpoint returns **404** on purpose — an
honest "not measured yet" beats a fabricated number.

| Suite | What it scores | Cost |
|---|---|---|
| `match_classifier` | The 3-way match as a 5-class classifier: precision, recall, F1, confusion matrix | free |
| `operational` | First-pass rate, touchless rate, turnaround, P2P cycle, exception mix, supplier acceptance | free |
| `nlp_parse` | 30 hand-labelled requisition phrasings, scored per field | ~30 API calls (`--nlp`) |
| `ocr` | Field accuracy + confidence calibration | reports `not_measured` — nothing renders invoice images yet |

**Two things to say out loud when showing this**, because both are the point:

1. **The answer key is `scenario`, never `expected_match_status`.** `seed.py`
   writes the latter from the same `evaluate()` call the suite grades, so
   scoring against it would return F1 = 1.00 by construction — a number that
   looks like a measurement and is an identity. `scenario` is the fault the
   seeder *injected*, chosen before the policy runs.
2. **Quote the baseline with the score.** The mix is ~74% clean, so an
   always-APPROVED classifier already scores 0.74. The harness prints that
   floor next to the accuracy, and calls out the near-miss case by name — a
   1.5% quantity variance that must still be APPROVED. That single case is what
   separates a real tolerance policy from a rule that flags everything.

---

## Data Flow Explained

### Inbound — Procure to Pay (Fully Autonomous)

```
  Employee types: "We need 500 meters of Industrial Aluminium Tubing at Bhiwandi"
       │
       ▼  [POST /requisitions/chat]
  NLP: LLM extracts → {material_id: "MAT-001", qty: 500, uom: "meter", delivery_location_id: "LOC-001"}
       │
       ▼  [POST /requisitions/{id}/select-supplier]
  5-factor scoring model evaluates all suppliers → picks winner → creates PO
       │
       ▼  [supplier_agent reacts to PO_CREATED event]
  Supplier confirms PO → creates shipment + trailer → truck is en route
       │
       ▼  [dock_worker reacts to SHIPMENT_CREATED / TRAILER_DEPARTED]
  OR-Tools CP-SAT assigns dock door + time window, optimized across all trucks
       │
       ▼  [simulator drives GPS ticks → YARD API]
  Truck moves → live ETA updates → map re-renders in real time
       │
       ▼  [trailer arrives → docks → IoT scanner triggered]
  Goods Receipt created with scanned quantity (IoT camera simulation)
       │
       ▼  [simulator submits invoice → POST /invoices/ocr]
  LLM Vision reads invoice image → extracts PO#, qty, price with confidence scores
       │
       ▼  [match_worker reacts to INVOICE_RECEIVED + GOODS_RECEIVED]
  3-way match: PO qty/price × Goods Receipt qty × Invoice qty/price
       │
       ├─ Within tolerance (2% qty, 3% price) → APPROVED
       │      │
       │      ▼  [shared/llm.write_match_reasoning — AFTER the verdict is fixed]
       │  AI writes the audit note; match_results.ai_narration stored alongside status/reason
       │      │
       │      ▼  [simulator: POST /payments/{id}/pay, ~2 min after approval]
       │  Payment settled → PO reaches CLOSED → chain terminates with no human ✅
       │
       └─ Outside tolerance → EXCEPTION → Human review queue 🚨
              (narrated too — the note says what a person now has to check)
```

### Outbound — Order to Delivery

```
  Customer order placed → picking list created
       │
       ▼
  Warehouse stages goods → truck dispatched
       │
       ▼
  Same CP-SAT dock solver assigns door (inbound + outbound compete for same pool)
       │
       ▼
  Truck arrives → docks → goods loaded → departs → DELIVERED
```

---

## Project Structure

```
cognizant/
├── backend/
│   ├── services/
│   │   ├── yard_api/          # E2: trailer, dock, shipment, outbound APIs
│   │   ├── procurement_api/   # PR2: PO, invoice, payment, exception APIs
│   │   ├── dashboard_gateway/ # BFF: cross-domain reads, WebSocket, KPIs
│   │   ├── simulator/         # Demo driver: trucks, GPS, invoices
│   │   ├── dock_worker/       # Async: OR-Tools CP-SAT dock scheduling
│   │   ├── match_worker/      # Async: deterministic 3-way match
│   │   └── supplier_agent/    # Async: supplier scoring + PO automation
│   ├── shared/
│   │   ├── llm.py             # AI layer — Claude/OpenAI, graceful fallback
│   │   ├── match_policy.py    # 3-way match rules (no LLM — pure math)
│   │   ├── dock_engine.py     # OR-Tools CP-SAT optimization engine
│   │   ├── procurement_scoring.py  # 5-factor supplier scoring (arithmetic)
│   │   └── auth.py            # JWT verification + role/permission matrix
│   ├── event_bus.py           # Transactional Outbox: Postgres → Redis
│   ├── schema.sql             # Full database schema (24 tables)
│   ├── migrations/            # Incremental, idempotent schema deltas
│   └── seed/seed.py           # Demo data generator
├── frontend/app/
│   └── src/
│       ├── screens/
│       │   ├── YardDock.tsx        # E2: yard board, dock timeline, IoT scanner
│       │   ├── Track.tsx           # E2: public customer delivery tracker (map)
│       │   ├── ControlTower.tsx    # Dashboard: KPIs, funnels, predictive risk
│       │   ├── MatchPay.tsx        # PR2: 3-way match viewer, exception resolution
│       │   └── Traceability.tsx    # Full P2P audit trail
│       ├── hooks/useEventStream.ts   # Authenticated dashboard event stream
│       ├── hooks/useTrackStream.ts   # Public per-consignment stream (Track)
│       └── api.ts                  # Typed API client
├── docs/                      # Detailed technical documentation
├── docker-compose.yml         # PostgreSQL 16 + Redis 7
├── run.sh                     # One-command start/stop/seed/migrate
├── demo.sh                    # Demo remote control: one word per demo moment
└── .env.example               # Required environment variables template
```

---

## API Reference

Interactive Swagger/OpenAPI docs (try every endpoint live in the browser):

| Service | Swagger URL |
|---|---|
| Yard API | [http://127.0.0.1:8001/docs](http://127.0.0.1:8001/docs) |
| Procurement API | [http://127.0.0.1:8002/docs](http://127.0.0.1:8002/docs) |
| Dashboard Gateway | [http://127.0.0.1:8003/docs](http://127.0.0.1:8003/docs) |
| Simulator | [http://127.0.0.1:8004/docs](http://127.0.0.1:8004/docs) |

### Key Endpoints

```
# Authentication (Gateway :8003)
POST /auth/login                        Sign in, receive JWT
POST /auth/signup                       Create account
GET  /auth/me                           Current user + permissions

# Yard (Yard API :8001)
GET  /yard-status                       All trailers, docks, assignments live
POST /trailers/{id}/tracking            Update GPS + ETA (WMS/IoT feed)
POST /trailers/{id}/arrive              Gate truck in
POST /trailers/{id}/dock                Assign to door (CP-SAT pre-planned)
POST /trailers/{id}/unload              IoT scan → goods receipt
POST /trailers/{id}/depart              Gate truck out

# Customer Tracker (Gateway :8003 — no auth required)
GET  /track/{ref}                       Look up by trailer/tracking/PO/order ID
WS   /ws/track/{ref}                    Live deltas for ONE consignment (public)
GET  /search?q={query}                  Global search across all entity types

# Procurement (Procurement API :8002)
POST /requisitions/chat                 Multi-turn NLP chatbot for procurement
POST /requisitions/{id}/select-supplier AI supplier selection → auto-creates PO
POST /invoices/ocr                      Upload invoice image → LLM extraction
GET  /exceptions/queue                  Human review queue for match failures
POST /exceptions/{id}/resolve           Resolve a flagged exception
POST /payments/{id}/pay                 Release an approved payment

# KPIs & Analytics (Gateway :8003)
GET  /dashboard/overview                Live operational KPIs + sample sizes
GET  /kpi/model-performance             Eval harness: match rates, turnaround
GET  /dashboard/supplier-risk           Predictive invoice risk: open POs ranked by money at risk
WS   /ws/dashboard?token=...            Authenticated live event stream (all domains)

# Simulator (Simulator :8004)
POST /sim/start                         Begin autonomous simulation loop
POST /sim/stop                          Pause (safe to resume anytime)
POST /sim/tick                          Advance exactly one tick manually
POST /sim/scenario/{name}              Force a specific demo moment
GET  /sim/scenarios                     List all available scenarios
```

---

## Key Design Decisions

### 1. Why OR-Tools CP-SAT for Dock Scheduling?
A simple "assign the next available door" rule ignores truck priorities, load types, and appointment windows. OR-Tools CP-SAT solves a proper optimization problem: minimize total weighted wait time across all trucks simultaneously, subject to hard constraints (door type compatibility, no double-booking, priority ordering). The result is a mathematically optimal schedule, not just a greedy heuristic.

### 2. Why is the 3-Way Match 100% deterministic code?
Using an LLM to decide whether to approve a ₹1,00,000 payment introduces hallucination risk. Our `match_policy.py` is a pure function — `(PO, GoodsReceipt, Invoice) → APPROVED | EXCEPTION`. It is directly unit-testable, produces a detailed audit trail explaining every check, and its tolerance thresholds (`QTY_TOLERANCE = 2%`, `PRICE_TOLERANCE = 3%`) are constants in a locked file. AI extracts the numbers from the invoice; math makes the decision.

### 3. Why the Transactional Outbox Pattern?
If a service writes a DB record and then crashes before publishing the Redis event, the event is permanently lost. With the Outbox pattern, the event is recorded in the same Postgres transaction as the domain write. Atomicity is guaranteed: either both commit, or neither does. A reconciliation thread then publishes to Redis at its own pace.

### 4. Why does the Simulator call HTTP APIs instead of writing to the DB directly?
Because this **proves the APIs work under real conditions**. If the simulator bypassed the APIs and injected rows directly into Postgres, the demo would show correct data, but the APIs themselves would be untested. Every simulator tick exercises authentication, request validation, event publishing, and downstream workers — exactly as a real WMS integration would.

### 5. Why separate Yard API and Procurement API?
Domain ownership prevents accidental coupling. The rule is: `goods_receipts` is written **only** by the Yard service. The Procurement service reads it to complete the 3-way match. This strict write-separation means neither service can corrupt the other's data, and both can be tested in complete isolation.

### 6. Why do timelines collapse GPS pings instead of listing them?
`event_log` holds two different kinds of record. **Facts** are discrete things a human audits — departed, docked, received, matched, paid. **Telemetry** is `TRAILER_LOCATION_UPDATED`, a sensor sampling a continuous quantity every few seconds. Measured on the seeded database, **78% of all events are position pings**, and one PO's audit trail is ~692 events of which ~660 say nothing — a payload that grows for as long as the truck drives while the information it carries stays constant.

Position belongs on the map, where 660 points are a smooth line; on a timeline, where every row costs a line of screen, only a change of state earns one. So `shared/telemetry.py` folds each *run* of pings into a single row carrying `count` and its span — runs, not event types, so two stretches of driving either side of a dock delay stay two rows and the order of events is never misrepresented. **Nothing is deleted:** the breadcrumb trail is still served in full from `tracking_events`, every collapsed row reports how many events it stands for, and `?telemetry=full` returns the raw trail for anyone auditing. The Traceability screen has a one-click switch for exactly that. In the live event rail, the pings become what they actually signal — a GPS pulse showing how many vehicles are reporting.

---

## Further Reading

Detailed technical deep-dives are in the [`docs/`](docs/) folder:

| File | Contents |
|---|---|
| [`docs/README.md`](docs/README.md) | Core concepts, patterns, Transactional Outbox explainer |
| [`docs/backend_services.md`](docs/backend_services.md) | APIs, simulator, domain ownership rules |
| [`docs/backend_workers.md`](docs/backend_workers.md) | OR-Tools CP-SAT dock worker, match worker, supplier agent |
| [`docs/ai_and_logic.md`](docs/ai_and_logic.md) | Where AI is used, where it is forbidden, and why |
| [`docs/frontend.md`](docs/frontend.md) | React architecture, role-based screens, WebSocket reactivity |
| [`docs/interview_cheatsheet.md`](docs/interview_cheatsheet.md) | Rapid-fire Q&A for the judging panel |

**Locked contracts** — these went through review-and-test cycles; change them only
by explicit decision (see `CLAUDE.md`):

| File | Governs |
|---|---|
| [`docs/api-contract.md`](docs/api-contract.md) | Every endpoint: request/response shape, tables touched, events emitted |
| [`docs/redis-contract.md`](docs/redis-contract.md) | Event stream, field contract, event-type vocabulary, consumer groups, idempotency |
| [`docs/DOCK_DECISION_ENGINE.md`](docs/DOCK_DECISION_ENGINE.md) | Dock scheduling: constraints, cost model, CP-SAT formulation, re-plan triggers |
| [`docs/3WAY_MATCH_POLICY.md`](docs/3WAY_MATCH_POLICY.md) | 3-way match tolerance rules |
| [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md) | Seed spec, simulator spec, eval-harness spec, milestone checklist |

**Screen-by-screen walkthroughs** — what each screen shows, why, and the exact
API calls behind it:

| File | Screen |
|---|---|
| [`docs/screen_controltower.md`](docs/screen_controltower.md) | Control Tower — KPIs, ROI, funnel, live map, predictive risk |
| [`docs/screen_yarddock.md`](docs/screen_yarddock.md) | Yard & Dock board — trailer table, IoT scanner, dock timeline |
| [`docs/screen_matchpay.md`](docs/screen_matchpay.md) | Match & Pay — 3-way reconciliation, OCR panel, AI audit note |
| [`docs/screen_procurement.md`](docs/screen_procurement.md) | Sourcing AI — NLP intake, supplier scoring, award rationale |
| [`docs/screen_track.md`](docs/screen_track.md) | Public tracker — Mapbox route, milestones, live socket |
| [`docs/screen_traceability_exceptions_outbound_login.md`](docs/screen_traceability_exceptions_outbound_login.md) | Traceability, Exceptions, Outbound, Login |
