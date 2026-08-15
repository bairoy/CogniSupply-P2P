-- ============================================================
-- Inbound-to-Pay Platform — LOCKED BASE SCHEMA (v4)
-- ============================================================
-- v4 (2026-08-14): additive-only changes, approved in docs/BUILD_PLAN.md §2.1.
-- Nine new columns + two indexes. NOTHING was renamed, dropped, or retyped —
-- every v3 column keeps its name, type, and meaning. New columns are marked
-- `-- v4` inline so the delta is greppable.
--
-- v5 (2026-08-15): authentication. Four new columns on `users`, one unique
-- index, one sequence — same additive-only discipline, marked `-- v5` inline.
-- Nothing else in this file changed. The `role` column already existed and
-- keeps its exact meaning; v5 only gives a user a way to prove who they are.
-- Applied to an already-running database by migrations/v5_auth.sql, which is
-- idempotent and produces a database identical to a fresh build of this file.
-- ============================================================
-- Companion file: README.md (start there for the full picture).
-- Companion file: redis-contract.md (event_type / entity_type vocab).
-- Companion file: event_bus.py (the only sanctioned way code writes events).
--
-- Design rules that make this schema stable under feature growth:
--
-- 1. Status fields are free TEXT, not DB-native ENUM types.
--    Postgres ENUMs require ALTER TYPE to add a value — painful
--    mid-project. TEXT + an app-level constant list gives the same
--    safety without migration risk. Statuses are an APPEND-ONLY
--    contract: once a value ships, never rename or remove it,
--    only add new ones.
--
-- 2. JSONB is used only where flexible/AI/event payloads genuinely
--    need it (parsed requisition intent, contract terms, OCR raw
--    output, event payloads, audit before/after values). Core
--    relational fields stay explicit typed columns. Not every table
--    has a JSONB column, and that's intentional — a metadata column
--    nobody ever queries is clutter, not flexibility.
--
-- 3. Shipment and Trailer are separate entities. A PO can produce
--    multiple shipments (partial delivery); a shipment can span
--    multiple trailers (split loads). Tier 1 only ever uses the
--    1:1 case, but the schema doesn't assume it.
--
-- 4. event_log is a generic, append-only ledger from day one —
--    the whole event backbone, no separate message broker needed
--    to have one. It is additionally mirrored live to Redis Streams
--    (see redis-contract.md) — Postgres write happens first and is
--    the source of truth; Redis is best-effort live delivery on top.
--
-- 5. goods_receipts.po_id is intentionally denormalized (also
--    derivable via trailer -> shipment -> po) for a fast, simple
--    3-way match query.
--
-- 6. locations holds GPS-scale points only (suppliers, warehouse,
--    waypoints) — never dock/yard layout, which is a floor-plan
--    concern (docks.yard_position), not a geography one. Keeping
--    these separate is what avoids ever needing PostGIS.
--
-- 7. IDs are human-readable TEXT with prefixes (PO-10245,
--    TRL-3391). Directly presentable in a live demo/panel Q&A —
--    "show me what happened to PO-10245" is answerable at a glance.
--
-- 8. Infrastructure: single PostgreSQL instance + Redis (Streams
--    only, for live event delivery). No message broker beyond
--    Redis itself, no object storage, no PostGIS, no native enum
--    types. Deliberate hackathon-scope decision — see README.md.
--
-- Verified: every statement in this file has been executed against
-- a live PostgreSQL 16 instance, including full-chain inserts and
-- joins across all 20 tables and FK-violation rejection tests.
-- ============================================================


-- ─────────────────────────────────────────────
-- MASTER / REFERENCE DATA
-- ─────────────────────────────────────────────

CREATE TABLE users (
    id           TEXT PRIMARY KEY,                  -- 'USR-001'
    name         TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'operator',   -- operator | procurement | finance | admin | system
      -- 'system' is the service account (USR-000) that touchless approvals are
      -- recorded against. It is NOT a login role -- see shared/auth.py.
    -- v5: login credentials. `email` is the login identifier and is looked up
    -- on every authentication, so it earns a real column rather than a
    -- metadata key (README §10). Both are NULLable: users seeded before v5,
    -- and the system service account, legitimately have no password.
    email          TEXT,                              -- v5
    password_hash  TEXT,                              -- v5, bcrypt; never plaintext
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,     -- v5, deactivate without deleting an FK target
    last_login_at  TIMESTAMPTZ,                       -- v5
    metadata     JSONB DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- v5: one account per email, case-insensitively. A functional unique index
-- rather than a UNIQUE column constraint, so 'Priya@x.com' and 'priya@x.com'
-- cannot both register. NULL emails are exempt (pre-v5 rows, service account).
CREATE UNIQUE INDEX uq_users_email_lower ON users(lower(email)) WHERE email IS NOT NULL;

-- GPS-scale points only (supplier sites, warehouse, route waypoints).
-- Deliberately NOT used for dock/yard layout — that's a floor-plan
-- concern (see docks.yard_position below), not a geography concern,
-- and mixing the two is what drags in PostGIS you don't need.
CREATE TABLE locations (
    id             TEXT PRIMARY KEY,                 -- 'LOC-001'
    name           TEXT NOT NULL,
    location_type  TEXT NOT NULL,                     -- SUPPLIER | WAREHOUSE | WAYPOINT
    latitude       NUMERIC,
    longitude      NUMERIC,
    metadata       JSONB DEFAULT '{}'
);

CREATE TABLE suppliers (
    id                  TEXT PRIMARY KEY,            -- 'SUP-001'
    name                TEXT NOT NULL,
    location_id         TEXT REFERENCES locations(id), -- nullable; set once locations are seeded
    reliability_score   NUMERIC DEFAULT 0.8,          -- 0-1, used by supplier ranking
    avg_lead_time_days  NUMERIC,
    quality_score       NUMERIC DEFAULT 0.8,          -- 0-1
    risk_score          NUMERIC DEFAULT 0.1,          -- 0-1, higher = riskier
    metadata            JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE materials (
    id                    TEXT PRIMARY KEY,          -- 'MAT-001'
    name                  TEXT NOT NULL,
    uom                   TEXT DEFAULT 'unit',
    default_supplier_id   TEXT REFERENCES suppliers(id),
    metadata              JSONB DEFAULT '{}'
);

CREATE TABLE docks (
    id                       TEXT PRIMARY KEY,       -- 'DOCK-04'
    compatible_load_types    TEXT[],                 -- {'dry_van','reefer'}
    yard_position            INTEGER,                -- simple layout order/slot for the yard board UI, not GPS
    is_active                BOOLEAN DEFAULT TRUE,
    metadata                 JSONB DEFAULT '{}'      -- v4: {"expected_unload_minutes": 45}
      -- Unload progress on the yard board is DERIVED, not stored:
      --   pct = (now() - dock_assignments.docked_at) / expected_unload_minutes
      -- A stored percentage would need a writer ticking it every few seconds.
);


-- ─────────────────────────────────────────────
-- PROCUREMENT (PR2)
-- ─────────────────────────────────────────────

CREATE TABLE requisitions (
    id             TEXT PRIMARY KEY,                 -- 'REQ-1001'
    requested_by   TEXT REFERENCES users(id),
    raw_text       TEXT NOT NULL,                     -- original NLP input
    parsed         JSONB,                             -- {item, qty, plant, required_date,...}
    status         TEXT NOT NULL DEFAULT 'PARSED',     -- PARSED | REJECTED | CONVERTED
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Retains the AI decision, not just the final supplier — lets you
-- answer "why did the system pick Supplier B" during the demo.
CREATE TABLE supplier_recommendations (
    id                 TEXT PRIMARY KEY,             -- 'SR-001'
    requisition_id     TEXT REFERENCES requisitions(id),
    supplier_id        TEXT REFERENCES suppliers(id),
    price_score        NUMERIC,
    quality_score      NUMERIC,
    lead_time_score    NUMERIC,
    reliability_score  NUMERIC,
    risk_score         NUMERIC,
    overall_score      NUMERIC,
    rank               INTEGER,
    recommended        BOOLEAN DEFAULT FALSE,
    -- v4: what the supplier recommendation card actually displays above the
    -- five score columns. `reasoning` is LLM-written prose explaining the
    -- already-computed scores -- the AI narrates the decision, it does not
    -- make it (same principle as 3WAY_MATCH_POLICY.md).
    quoted_unit_price     NUMERIC,
    quoted_lead_time_days NUMERIC,
    reasoning             TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE purchase_orders (
    id                    TEXT PRIMARY KEY,           -- 'PO-10245'
    requisition_id        TEXT REFERENCES requisitions(id),
    supplier_id           TEXT REFERENCES suppliers(id),
    material_id           TEXT REFERENCES materials(id),
    qty                   NUMERIC NOT NULL,
    unit_price            NUMERIC NOT NULL,
    delivery_location_id  TEXT REFERENCES locations(id),  -- promised delivery point at PO time
    expected_delivery     TIMESTAMPTZ,                       -- promised date; compare against shipments.expected_arrival and actual arrival for KPIs
    terms                 JSONB DEFAULT '{}',              -- remaining contract terms (payment terms, currency, etc.)
    status                TEXT NOT NULL DEFAULT 'CREATED',
      -- CREATED | SHIPPED | PARTIALLY_RECEIVED | RECEIVED | MATCHED | CLOSED
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ─────────────────────────────────────────────
-- LOGISTICS (E2)
-- ─────────────────────────────────────────────

CREATE TABLE shipments (
    id                        TEXT PRIMARY KEY,       -- 'SHP-2031'
    po_id                     TEXT REFERENCES purchase_orders(id),
    tracking_number           TEXT,
    carrier                   TEXT,
    origin_location_id        TEXT REFERENCES locations(id),
    destination_location_id   TEXT REFERENCES locations(id),
    expected_arrival          TIMESTAMPTZ,
    status                    TEXT NOT NULL DEFAULT 'CREATED',
      -- CREATED | EN_ROUTE | ARRIVED | UNLOADED
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- one PO -> many shipments (partial delivery supported, not required in Tier 1)

CREATE TABLE trailers (
    id            TEXT PRIMARY KEY,                   -- 'TRL-3391'
    shipment_id   TEXT REFERENCES shipments(id),
    load_type     TEXT,
    priority      TEXT DEFAULT 'normal',               -- low | normal | high | critical
    eta           TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'EN_ROUTE',
      -- EN_ROUTE | ARRIVED | DOCKED | UNLOADED
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- one shipment -> many trailers (split loads supported, not required in Tier 1)

CREATE TABLE tracking_events (
    id             SERIAL PRIMARY KEY,
    trailer_id     TEXT REFERENCES trailers(id),
    latitude       NUMERIC,
    longitude      NUMERIC,
    speed          NUMERIC,
    eta_estimate   TIMESTAMPTZ,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- append-only time series; Tier 1 can insert one row per trailer,
-- a smarter "ETA model" later just means smarter values into eta_estimate

CREATE TABLE dock_assignments (
    id             TEXT PRIMARY KEY,                  -- 'DA-5521'
    trailer_id     TEXT REFERENCES trailers(id),
    dock_id        TEXT REFERENCES docks(id),
    assigned_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status         TEXT NOT NULL DEFAULT 'ASSIGNED',
      -- ASSIGNED | CONFIRMED | DELAYED | REASSIGNED | COMPLETED
      -- COMPLETED marks the dock released after unload — without it, a
      -- dock that finished unloading would read as occupied forever
      -- under the dock decision engine's hard-constraint check.
      -- See DOCK_DECISION_ENGINE.md.
    reason         TEXT,                               -- explainability: why this dock
    -- v4: the numbers behind `reason`. The Yard & Dock screen renders the
    -- weighted breakdown (priority 0.50 + specialization 0.30 - position 0.08
    -- = 0.72) and the runners-up, which a prose sentence cannot carry.
    score_breakdown JSONB DEFAULT '{}',
    --   {"hard_constraints":{"active":true,"load_type_ok":true,"unoccupied":true},
    --    "priority_score":0.50,"specialization_score":0.30,
    --    "position_penalty":-0.08,"final_score":0.72,
    --    "candidates":[{"dock_id":"DOCK-02","final_score":0.61}, ...]}
    -- v4: set when the trailer physically occupies the door. Drives the
    -- derived unload-progress percentage (see docks.metadata).
    docked_at      TIMESTAMPTZ
);

CREATE TABLE goods_receipts (
    id             TEXT PRIMARY KEY,                  -- 'GR-9876'
    trailer_id     TEXT REFERENCES trailers(id),
    shipment_id    TEXT REFERENCES shipments(id),
    po_id          TEXT REFERENCES purchase_orders(id),  -- denormalized, see note above
    qty_received   NUMERIC NOT NULL,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_by    TEXT DEFAULT 'simulated_cv'          -- swap for real CV/IoT later, no schema change
);
-- THIS TABLE IS OWNED AND WRITTEN BY THE E2/YARD SERVICE ONLY.
-- PR2 reads it, never writes it. (Locked decision — do not relitigate.)


-- ─────────────────────────────────────────────
-- INVOICING & MATCHING (PR2)
-- ─────────────────────────────────────────────

CREATE TABLE invoices (
    id                    TEXT PRIMARY KEY,           -- 'INV-7788'
    po_id                 TEXT REFERENCES purchase_orders(id),
    qty_invoiced          NUMERIC NOT NULL,
    unit_price_invoiced   NUMERIC NOT NULL,
    tax                   NUMERIC DEFAULT 0,
    total                 NUMERIC,
    ocr_confidence        NUMERIC,
    ocr_raw               JSONB,                       -- raw OCR output, swap OCR engine freely
    -- v4: path to the rendered invoice image on the local invoice volume,
    -- for "View Original Scan". A file path, NOT object storage -- the
    -- no-S3 decision in README §1 stands.
    document_path         TEXT,
    received_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE match_results (
    id                 TEXT PRIMARY KEY,               -- 'MR-4432'
    po_id              TEXT REFERENCES purchase_orders(id),
    goods_receipt_id   TEXT REFERENCES goods_receipts(id),
    invoice_id         TEXT REFERENCES invoices(id),
    status             TEXT NOT NULL,                   -- APPROVED | EXCEPTION
    reason             TEXT,
    resolved_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE exceptions (
    id                 TEXT PRIMARY KEY,               -- 'EXC-201'
    match_result_id    TEXT REFERENCES match_results(id),
    exception_type     TEXT NOT NULL,
      -- QTY_MISMATCH | PRICE_MISMATCH | MISSING_PO | DUPLICATE_INVOICE | OTHER
    status             TEXT NOT NULL DEFAULT 'OPEN',    -- OPEN | APPROVED | REJECTED
    assigned_to        TEXT REFERENCES users(id),
    -- v4: the Exceptions Command Center sorts and filters on these on every
    -- render, so they earn real columns rather than a JSONB blob.
    severity           TEXT DEFAULT 'medium',           -- low | medium | high | critical
    impact_amount      NUMERIC,                         -- |invoice total - PO total|
    resolution_notes   TEXT,
    resolved_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE payments (
    id                 TEXT PRIMARY KEY,               -- 'PAY-661'
    invoice_id         TEXT REFERENCES invoices(id),
    amount             NUMERIC NOT NULL,
    status             TEXT NOT NULL DEFAULT 'PENDING', -- PENDING | APPROVED | PAID | REJECTED
    approved_by        TEXT REFERENCES users(id),
    approved_at        TIMESTAMPTZ,
    paid_at            TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ─────────────────────────────────────────────
-- CROSS-CUTTING (shared by both modules)
-- ─────────────────────────────────────────────

CREATE TABLE alerts (
    id             TEXT PRIMARY KEY,                  -- 'ALT-991'
    entity_type    TEXT NOT NULL,                       -- 'trailer' | 'dock_assignment' | 'match_result'
    entity_id      TEXT NOT NULL,
    alert_type     TEXT NOT NULL,                        -- DELAY | DOCK_CONFLICT | MATCH_EXCEPTION
    message        TEXT,
    severity       TEXT DEFAULT 'info',                  -- info | warning | critical
    acknowledged   BOOLEAN DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE event_log (
    id                SERIAL PRIMARY KEY,           -- canonical event_id, mirrored as-is into Redis
    entity_type       TEXT NOT NULL,                 -- see redis-contract.md §4 for the locked vocabulary
    entity_id         TEXT NOT NULL,
    event_type        TEXT NOT NULL,                 -- see redis-contract.md §3 for the locked vocabulary
      -- NOTE: this vocabulary is intentionally not duplicated here.
      -- redis-contract.md §3 is the single source of truth (it must
      -- match what's XADD'd to Redis exactly) — keeping one copy
      -- instead of two prevents them silently drifting apart.
    payload           JSONB,
    redis_published   BOOLEAN NOT NULL DEFAULT FALSE, -- flips true once XADD succeeds; lets a
                                                        -- reconciler find and replay anything Redis missed
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- This IS the event backbone — an append-only ledger, no separate
-- message broker required. Real streaming later = a process that
-- tails this table and publishes. No other table changes for that.

-- "Why did dock assignment change from D4 to D2" answered by one query.
CREATE TABLE audit_logs (
    id            SERIAL PRIMARY KEY,
    user_id       TEXT REFERENCES users(id),
    action        TEXT NOT NULL,                       -- e.g. 'dock_reassigned', 'exception_resolved'
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    old_value     JSONB,
    new_value     JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Consumer idempotency, owned by Postgres (not Redis — see redis-contract.md
-- §8). (consumer_group, event_id) as primary key makes "claim this event
-- for this group" an atomic operation via INSERT ... ON CONFLICT DO
-- NOTHING: a second concurrent claim attempt for the same pair correctly
-- gets nothing back, with no race window. The same event_id legitimately
-- appears once per consumer group (dock-worker, match-worker, and
-- dashboard-ws each process every event independently).
CREATE TABLE processed_events (
    consumer_group   TEXT NOT NULL,
    event_id         INTEGER NOT NULL,
    processed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_group, event_id)
);


-- ─────────────────────────────────────────────
-- INDEXES — worth creating now, cost nothing to have early
-- ─────────────────────────────────────────────

CREATE INDEX idx_shipments_po ON shipments(po_id);
CREATE INDEX idx_shipments_status ON shipments(status);
CREATE INDEX idx_trailers_shipment ON trailers(shipment_id);
CREATE INDEX idx_trailers_status ON trailers(status);
CREATE INDEX idx_tracking_trailer_time ON tracking_events(trailer_id, recorded_at);
CREATE INDEX idx_dock_assignments_trailer ON dock_assignments(trailer_id);
CREATE INDEX idx_dock_assignments_status ON dock_assignments(status);
CREATE INDEX idx_goods_receipts_po ON goods_receipts(po_id);
CREATE INDEX idx_invoices_po ON invoices(po_id);
CREATE INDEX idx_match_results_po ON match_results(po_id);
CREATE INDEX idx_exceptions_status ON exceptions(status);
CREATE INDEX idx_alerts_entity ON alerts(entity_type, entity_id);
CREATE INDEX idx_event_log_entity ON event_log(entity_type, entity_id);
CREATE INDEX idx_event_log_type_time ON event_log(event_type, created_at);
CREATE INDEX idx_event_log_unpublished ON event_log(id) WHERE redis_published = FALSE;
CREATE INDEX idx_supplier_recommendations_req ON supplier_recommendations(requisition_id, rank);
CREATE INDEX idx_suppliers_location ON suppliers(location_id);
CREATE INDEX idx_locations_type ON locations(location_type);

-- Prevents the same invoice from ever being matched twice — a data
-- integrity guarantee, not just an app-level hope. Deliberately no
-- equivalent unique constraint on goods_receipts.trailer_id: the
-- schema explicitly allows split/partial-delivery scenarios there.
CREATE UNIQUE INDEX uq_match_results_invoice ON match_results(invoice_id);

-- v4 indexes: the exception queue sorts by severity then age on every render,
-- and the dock engine's Stage 1 hard-constraint check filters by
-- (dock_id, status) for every candidate dock on every scoring pass.
CREATE INDEX idx_exceptions_severity ON exceptions(severity, created_at DESC);
CREATE INDEX idx_dock_assignments_dock_status ON dock_assignments(dock_id, status);


-- ─────────────────────────────────────────────
-- ID SEQUENCES — concurrency-safe ID generation
-- ─────────────────────────────────────────────
-- Human-readable IDs stay exactly as designed (PO-10245 style), but the
-- numeric suffix now comes from a real Postgres sequence, not COUNT(*)+1
-- -- which can generate duplicate IDs under concurrent requests. Added
-- for every entity that generates its own ID, not just the ones Yard
-- API currently uses, so Procurement API doesn't have to re-decide this.
CREATE SEQUENCE user_id_seq START 100;   -- v5: signup-generated USR-100+, clear
                                         -- of the USR-000..USR-006 seeded block
CREATE SEQUENCE requisition_id_seq START 1001;
CREATE SEQUENCE supplier_recommendation_id_seq START 1001;
CREATE SEQUENCE purchase_order_id_seq START 1001;
CREATE SEQUENCE shipment_id_seq START 1001;
CREATE SEQUENCE trailer_id_seq START 1001;
CREATE SEQUENCE dock_assignment_id_seq START 1001;
CREATE SEQUENCE goods_receipt_id_seq START 1001;
CREATE SEQUENCE invoice_id_seq START 1001;
CREATE SEQUENCE match_result_id_seq START 1001;
CREATE SEQUENCE exception_id_seq START 1001;
CREATE SEQUENCE payment_id_seq START 1001;
CREATE SEQUENCE alert_id_seq START 1001;
