"""
Yard API (E2). Owns: shipments, trailers, tracking_events, dock_assignments,
goods_receipts (write). Every endpoint here matches api-contract.md exactly
— method, URL, request/response shape, tables touched, event(s) emitted,
transaction boundary. No new tables, columns, event types, or entity types
are introduced; this is implementation of what's already locked, not a
design decision.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# backend/services/yard_api/main.py -> backend/ is three levels up.
# This resolves relative to this file's own location, so it works
# regardless of where the repo is checked out -- no hardcoded absolute
# paths. shared/db.py and event_bus.py both live directly under backend/.
BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.db import get_conn
from event_bus import record_event, publish_to_redis

app = FastAPI(title="Yard API (E2)")

ETA_MATERIAL_CHANGE_MINUTES = 10


# ─────────────────────────────────────────────
# Request/response models (mirroring api-contract.md exactly)
# ─────────────────────────────────────────────

class CreateShipmentRequest(BaseModel):
    po_id: str
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    origin_location_id: Optional[str] = None
    destination_location_id: Optional[str] = None
    expected_arrival: Optional[datetime] = None


class CreateTrailerRequest(BaseModel):
    load_type: Optional[str] = None
    priority: str = "normal"


class TrackingUpdateRequest(BaseModel):
    latitude: float
    longitude: float
    speed: Optional[float] = None
    eta_estimate: datetime


class UnloadRequest(BaseModel):
    qty_received: float


class ReassignRequest(BaseModel):
    new_dock_id: str
    reason: Optional[str] = None


# ─────────────────────────────────────────────
# POST /shipments
# ─────────────────────────────────────────────

@app.post("/shipments", status_code=201)
def create_shipment(body: CreateShipmentRequest):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM purchase_orders WHERE id = %s", (body.po_id,))
            if cur.fetchone() is None:
                raise HTTPException(404, f"purchase_order {body.po_id} not found")

            shipment_id = _next_id(cur, "SHP", "shipment_id_seq")
            cur.execute(
                """
                INSERT INTO shipments (id, po_id, tracking_number, carrier,
                    origin_location_id, destination_location_id, expected_arrival, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'CREATED')
                """,
                (shipment_id, body.po_id, body.tracking_number, body.carrier,
                 body.origin_location_id, body.destination_location_id, body.expected_arrival),
            )

        event_id, created_at = record_event(conn, "shipment", shipment_id, "SHIPMENT_CREATED", {})
        conn.commit()
        publish_to_redis(conn, event_id, "shipment", shipment_id, "SHIPMENT_CREATED", {}, created_at)

    return {"id": shipment_id, "status": "CREATED"}


# ─────────────────────────────────────────────
# POST /shipments/{shipment_id}/trailers
# ─────────────────────────────────────────────

@app.post("/shipments/{shipment_id}/trailers", status_code=201)
def create_trailer(shipment_id: str, body: CreateTrailerRequest):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM shipments WHERE id = %s", (shipment_id,))
            if cur.fetchone() is None:
                raise HTTPException(404, f"shipment {shipment_id} not found")

            trailer_id = _next_id(cur, "TRL", "trailer_id_seq")
            cur.execute(
                """
                INSERT INTO trailers (id, shipment_id, load_type, priority, status)
                VALUES (%s, %s, %s, %s, 'EN_ROUTE')
                """,
                (trailer_id, shipment_id, body.load_type, body.priority),
            )

        event_id, created_at = record_event(conn, "trailer", trailer_id, "TRAILER_DEPARTED", {})
        conn.commit()
        publish_to_redis(conn, event_id, "trailer", trailer_id, "TRAILER_DEPARTED", {}, created_at)

    return {"id": trailer_id, "status": "EN_ROUTE"}


# ─────────────────────────────────────────────
# POST /trailers/{trailer_id}/tracking
# ─────────────────────────────────────────────

@app.post("/trailers/{trailer_id}/tracking", status_code=201)
def post_tracking(trailer_id: str, body: TrackingUpdateRequest):
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Step 1: capture the OLD eta BEFORE any write — order matters,
            # per the locked contract's explicit fix. Computing the delta
            # after overwriting trailers.eta would always yield zero.
            cur.execute("SELECT eta FROM trailers WHERE id = %s", (trailer_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            old_eta = row[0]

            # Step 2: append the tracking event
            cur.execute(
                """
                INSERT INTO tracking_events (trailer_id, latitude, longitude, speed, eta_estimate)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (trailer_id, body.latitude, body.longitude, body.speed, body.eta_estimate),
            )

            # Step 3: update the current eta
            cur.execute("UPDATE trailers SET eta = %s, updated_at = now() WHERE id = %s",
                        (body.eta_estimate, trailer_id))

        # Step 4: always emit TRAILER_LOCATION_UPDATED
        payload = {"latitude": body.latitude, "longitude": body.longitude, "eta": body.eta_estimate.isoformat()}
        event_id, created_at = record_event(conn, "trailer", trailer_id, "TRAILER_LOCATION_UPDATED", payload)

        # Step 5: conditionally also emit ETA_UPDATED, same transaction
        eta_changed_materially = False
        eta_event_id, eta_created_at = None, None
        if old_eta is not None:
            delta_minutes = abs((body.eta_estimate - old_eta.replace(tzinfo=timezone.utc)
                                  if old_eta.tzinfo is None else body.eta_estimate - old_eta).total_seconds()) / 60
            if delta_minutes >= ETA_MATERIAL_CHANGE_MINUTES:
                eta_changed_materially = True
                eta_payload = {"previous_eta": old_eta.isoformat(), "new_eta": body.eta_estimate.isoformat(),
                                "delta_minutes": round(delta_minutes, 1)}
                eta_event_id, eta_created_at = record_event(conn, "trailer", trailer_id, "ETA_UPDATED", eta_payload)

        # Step 6: commit once, both events together
        conn.commit()
        publish_to_redis(conn, event_id, "trailer", trailer_id, "TRAILER_LOCATION_UPDATED", payload, created_at)
        if eta_changed_materially:
            publish_to_redis(conn, eta_event_id, "trailer", trailer_id, "ETA_UPDATED", eta_payload, eta_created_at)

    return {"recorded": True, "eta_changed_materially": eta_changed_materially}


# ─────────────────────────────────────────────
# POST /trailers/{trailer_id}/arrive
# ─────────────────────────────────────────────

@app.post("/trailers/{trailer_id}/arrive")
def arrive(trailer_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM trailers WHERE id = %s", (trailer_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            cur.execute("UPDATE trailers SET status = 'ARRIVED', updated_at = now() WHERE id = %s", (trailer_id,))

        event_id, created_at = record_event(conn, "trailer", trailer_id, "TRAILER_ARRIVED", {})
        conn.commit()
        publish_to_redis(conn, event_id, "trailer", trailer_id, "TRAILER_ARRIVED", {}, created_at)

    return {"id": trailer_id, "status": "ARRIVED"}


# ─────────────────────────────────────────────
# POST /trailers/{trailer_id}/unload  — the ONLY writer of goods_receipts
# ─────────────────────────────────────────────

@app.post("/trailers/{trailer_id}/unload", status_code=201)
def unload(trailer_id: str, body: UnloadRequest):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT shipment_id FROM trailers WHERE id = %s", (trailer_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            shipment_id = row[0]

            cur.execute("SELECT po_id FROM shipments WHERE id = %s", (shipment_id,))
            po_id = cur.fetchone()[0]

            cur.execute("UPDATE trailers SET status = 'UNLOADED', updated_at = now() WHERE id = %s", (trailer_id,))

            # Release the dock: without this, the dock decision engine's
            # hard-constraint check would see this dock as occupied
            # forever. Matches ASSIGNED/CONFIRMED/DELAYED, not just
            # ASSIGNED -- a trailer can legitimately reach unload while
            # its assignment sits in any of those three states.
            # See DOCK_DECISION_ENGINE.md.
            cur.execute(
                "UPDATE dock_assignments SET status = 'COMPLETED' WHERE trailer_id = %s AND status IN ('ASSIGNED', 'CONFIRMED', 'DELAYED')",
                (trailer_id,),
            )

            gr_id = _next_id(cur, "GR", "goods_receipt_id_seq")
            cur.execute(
                """
                INSERT INTO goods_receipts (id, trailer_id, shipment_id, po_id, qty_received)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (gr_id, trailer_id, shipment_id, po_id, body.qty_received),
            )

        payload = {"po_id": po_id, "qty_received": body.qty_received}
        event_id, created_at = record_event(conn, "goods_receipt", gr_id, "GOODS_RECEIVED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "goods_receipt", gr_id, "GOODS_RECEIVED", payload, created_at)

    return {"goods_receipt_id": gr_id}


# ─────────────────────────────────────────────
# POST /dock-assignments/{id}/reassign — locked history-preserving pattern
# ─────────────────────────────────────────────

@app.post("/dock-assignments/{assignment_id}/reassign")
def reassign(assignment_id: str, body: ReassignRequest):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT trailer_id FROM dock_assignments WHERE id = %s", (assignment_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"dock_assignment {assignment_id} not found")
            trailer_id = row[0]

            # Never UPDATE dock_id in place — mark old REASSIGNED, insert new ASSIGNED.
            cur.execute("UPDATE dock_assignments SET status = 'REASSIGNED' WHERE id = %s", (assignment_id,))

            new_id = _next_id(cur, "DA", "dock_assignment_id_seq")
            cur.execute(
                """
                INSERT INTO dock_assignments (id, trailer_id, dock_id, status, reason)
                VALUES (%s, %s, %s, 'ASSIGNED', %s)
                """,
                (new_id, trailer_id, body.new_dock_id, body.reason),
            )

        payload = {"old_assignment_id": assignment_id, "new_dock_id": body.new_dock_id, "reason": body.reason}
        event_id, created_at = record_event(conn, "dock_assignment", new_id, "DOCK_REASSIGNED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "dock_assignment", new_id, "DOCK_REASSIGNED", payload, created_at)

    return {"old_assignment_id": assignment_id, "new_assignment_id": new_id, "status": "ASSIGNED"}


# ─────────────────────────────────────────────
# GET /yard-status — current state only (status='ASSIGNED', not history)
# ─────────────────────────────────────────────

@app.get("/yard-status")
def yard_status():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.status, t.eta, da.dock_id, da.status
                FROM trailers t
                LEFT JOIN dock_assignments da ON da.trailer_id = t.id AND da.status IN ('ASSIGNED', 'CONFIRMED')
                ORDER BY t.created_at DESC
            """)
            trailers = [
                {"id": r[0], "status": r[1], "eta": r[2].isoformat() if r[2] else None,
                 "dock_assignment": {"dock_id": r[3], "status": r[4]} if r[3] else None}
                for r in cur.fetchall()
            ]
            cur.execute("""
                SELECT d.id, d.yard_position,
                       EXISTS(SELECT 1 FROM dock_assignments WHERE dock_id = d.id AND status IN ('ASSIGNED', 'CONFIRMED')) AS occupied
                FROM docks d ORDER BY d.yard_position
            """)
            docks = [{"id": r[0], "yard_position": r[1], "occupied": r[2]} for r in cur.fetchall()]
    return {"trailers": trailers, "docks": docks}


# ─────────────────────────────────────────────
# GET /trailers/{trailer_id} — full history, not just current
# ─────────────────────────────────────────────

@app.get("/trailers/{trailer_id}")
def trailer_detail(trailer_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, status, eta, priority, load_type FROM trailers WHERE id = %s", (trailer_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            trailer = {"id": row[0], "status": row[1], "eta": row[2].isoformat() if row[2] else None,
                       "priority": row[3], "load_type": row[4]}

            cur.execute("""SELECT latitude, longitude, speed, eta_estimate, recorded_at
                           FROM tracking_events WHERE trailer_id = %s ORDER BY recorded_at""", (trailer_id,))
            tracking_history = [{"latitude": r[0], "longitude": r[1], "speed": r[2],
                                  "eta_estimate": r[3].isoformat() if r[3] else None,
                                  "recorded_at": r[4].isoformat()} for r in cur.fetchall()]

            cur.execute("""SELECT id, dock_id, status, reason, assigned_at
                           FROM dock_assignments WHERE trailer_id = %s ORDER BY assigned_at""", (trailer_id,))
            dock_history = [{"id": r[0], "dock_id": r[1], "status": r[2], "reason": r[3],
                              "assigned_at": r[4].isoformat()} for r in cur.fetchall()]

            cur.execute("""SELECT event_type, created_at, payload FROM event_log
                           WHERE entity_type = 'trailer' AND entity_id = %s ORDER BY created_at""", (trailer_id,))
            events = [{"event_type": r[0], "created_at": r[1].isoformat(), "payload": r[2]} for r in cur.fetchall()]

    return {"trailer": trailer, "tracking_history": tracking_history,
            "dock_assignment_history": dock_history, "events": events}


# ─────────────────────────────────────────────
def _next_id(cur, prefix: str, seq_name: str) -> str:
    """
    Concurrency-safe human-readable ID via a real Postgres sequence.
    Replaces the earlier COUNT(*)+1 approach, which could generate
    duplicate IDs under concurrent requests -- SELECT nextval() is
    atomic at the database level, COUNT(*) is not.
    """
    cur.execute(f"SELECT nextval('{seq_name}')")
    return f"{prefix}-{cur.fetchone()[0]}"
