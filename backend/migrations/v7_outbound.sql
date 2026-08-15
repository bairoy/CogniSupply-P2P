-- ============================================================
-- v7 — OUTBOUND YARD OPERATIONS (additive only)
-- ============================================================
-- docker-compose mounts schema.sql as an initdb script, which Postgres runs
-- ONLY on an empty data directory. A database that is already up and seeded
-- therefore never sees a schema.sql edit. This file applies the v7 delta to
-- such a database, and is written so that running it produces a schema
-- identical to a fresh build of schema.sql.
--
--   ./run.sh migrate          (or)
--   docker compose exec -T postgres psql -U postgres -d inbound_test \
--       < backend/migrations/v7_outbound.sql
--
-- Idempotent: every statement is IF NOT EXISTS. Safe to run repeatedly, and
-- safe against a fresh database that already has v7 from schema.sql.
-- Nothing is dropped, renamed, or retyped.
--
-- What it enables: until v7 the yard only understood goods coming IN. Every
-- trailer row implied a supplier at one end and our warehouse at the other, so
-- "a truck is coming to collect a customer order" had nowhere to live. v7 adds
-- outbound orders, their pick/load plans, and the goods-issue record — and
-- teaches the EXISTING trailer/dock/tracking tables to carry both directions
-- rather than mirroring them, so one dock scheduler keeps serving one set of
-- doors.
-- ============================================================

BEGIN;

-- ── New tables ──────────────────────────────────────────────
-- Created before the ALTERs below, because shipments.outbound_order_id
-- references outbound_orders.

CREATE TABLE IF NOT EXISTS outbound_orders (
    id                       TEXT PRIMARY KEY,
    customer_name            TEXT NOT NULL,
    destination_location_id  TEXT REFERENCES locations(id),
    requested_ship_date      TIMESTAMPTZ,
    priority                 TEXT DEFAULT 'normal',
    status                   TEXT NOT NULL DEFAULT 'CREATED',
      -- CREATED | PLANNED | STAGED | LOADING | SHIPPED | DELIVERED | CANCELLED
    metadata                 JSONB DEFAULT '{}',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS load_plans (
    id                  TEXT PRIMARY KEY,
    outbound_order_id   TEXT REFERENCES outbound_orders(id),
    material_id         TEXT REFERENCES materials(id),
    qty_ordered         NUMERIC NOT NULL,
    qty_staged          NUMERIC NOT NULL DEFAULT 0,
    qty_loaded          NUMERIC NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'PLANNED',
      -- PLANNED | PICKING | STAGED | LOADED | SHORT
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS goods_issues (
    id                 TEXT PRIMARY KEY,
    trailer_id         TEXT REFERENCES trailers(id),
    shipment_id        TEXT REFERENCES shipments(id),
    outbound_order_id  TEXT REFERENCES outbound_orders(id),
    qty_issued         NUMERIC NOT NULL,
    lines              JSONB DEFAULT '[]',
    issued_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_by        TEXT DEFAULT 'simulated_cv'
);

-- ── Direction discriminators on the existing tables ─────────
-- DEFAULT 'INBOUND' is what makes this safe on a seeded database: every row
-- that already exists IS inbound, so the backfill is the default and there is
-- no UPDATE to run. NOT NULL is therefore free here, which it would not be if
-- the column had to be nullable first and tightened later.

ALTER TABLE shipments ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'INBOUND';
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS outbound_order_id TEXT REFERENCES outbound_orders(id);
ALTER TABLE trailers  ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'INBOUND';

-- ── Indexes ─────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_trailers_direction_status ON trailers(direction, status);
CREATE INDEX IF NOT EXISTS idx_shipments_direction ON shipments(direction, status);
CREATE INDEX IF NOT EXISTS idx_shipments_outbound_order ON shipments(outbound_order_id);
CREATE INDEX IF NOT EXISTS idx_outbound_orders_status ON outbound_orders(status, requested_ship_date);
CREATE INDEX IF NOT EXISTS idx_load_plans_order ON load_plans(outbound_order_id);
CREATE INDEX IF NOT EXISTS idx_goods_issues_order ON goods_issues(outbound_order_id);
CREATE INDEX IF NOT EXISTS idx_goods_issues_trailer ON goods_issues(trailer_id);

-- ── Sequences ───────────────────────────────────────────────
-- CREATE SEQUENCE IF NOT EXISTS keeps this re-runnable. START values match
-- schema.sql so IDs read the same whether the database was built fresh or
-- migrated.

CREATE SEQUENCE IF NOT EXISTS outbound_order_id_seq START 1001;
CREATE SEQUENCE IF NOT EXISTS load_plan_id_seq START 1001;
CREATE SEQUENCE IF NOT EXISTS goods_issue_id_seq START 1001;

COMMIT;
