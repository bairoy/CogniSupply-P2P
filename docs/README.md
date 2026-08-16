# CogniSupply P2P: Overall Architecture & Hackathon Pitch

## Overview
This project tackles two primary use cases:
1. **E2: Where's My Truck?** - A yard, dock door, and delivery tracker.
2. **PR2: Autonomous P2P** - An end-to-end Procure-to-Pay pipeline featuring conversational AI, OCR, and automated 3-way matching.

## The Core Philosophy (Interview Talking Points)
When a judge asks "How is this structured?", you should hit these three points:

1. **Event-Driven Microservices:** The system is broken down into distinct domains (Yard, Procurement, Dashboard). They do not call each other synchronously to change state. Instead, they publish events to a Redis Event Bus. This ensures loose coupling.
2. **CQRS (Command Query Responsibility Segregation):** We strictly separate "writes" from "reads". The `yard_api` and `procurement_api` handle all writes (Commands). The `dashboard_gateway` handles complex cross-domain reads (Queries) and pushes real-time WebSocket updates to the frontend.
3. **AI is Kept Outside the Decision Boundary:** This is a crucial defense against AI hallucinations. We use LLMs for extraction (reading invoices via OCR, understanding NLP requisitions) and narration (explaining why a supplier was chosen, and writing up a 3-way match verdict that has already been decided). However, the actual **decisions** (3-way match approval, supplier scoring) are pure, deterministic math. This makes our system enterprise-ready and auditable.

## System Components
- **Postgres:** The single source of truth for all structured data.
- **Redis:** Used as a message broker (Event Bus) for async workers.
- **Backend Services (Python/FastAPI):**
  - `yard_api`: Manages physical logistics (trucks, docks).
  - `procurement_api`: Manages financial/inventory documents (POs, Invoices).
  - `dashboard_gateway`: Serves the frontend, aggregates data, handles WebSockets.
  - `simulator`: Drives the demo by acting as the "real world" (moving trucks, submitting invoices).
- **Backend Workers (Python):**
  - `dock_worker`: Uses Google OR-Tools to solve dock door scheduling.
  - `match_worker`: Executes the 3-way match policy.
  - `supplier_agent`: Handles autonomous PO generation and supplier selection.
- **Frontend (React/Vite):** A role-based dashboard simulating different user personas.

## The Transactional Outbox Pattern
**If a judge asks:** *"How do you guarantee an event is sent to Redis if the database commits, but the network crashes before Redis receives it?"*

**Answer:** We use the Transactional Outbox pattern. When an API writes to the database (e.g., saving an invoice), it also writes the event to the `event_log` table in the *same Postgres transaction*. A background thread (`reconcile_unpublished()`) continuously reads from this table and pushes to Redis. If a crash happens, the database transaction rolls back entirely, OR the event remains in the DB waiting to be published when the server restarts. Atomicity is guaranteed.
