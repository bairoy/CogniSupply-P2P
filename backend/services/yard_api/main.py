"""
Yard API (E2). Owns: shipments, trailers, tracking_events, dock_assignments,
goods_receipts (write).

Every endpoint matches docs/api-contract.md -- method, URL, request/response
shape, tables touched, event(s) emitted, transaction boundary. The v4 additions
(POST /trailers/{id}/dock, the extended GET /yard-status response, and event
payload enrichment) are recorded in docs/BUILD_PLAN.md §2.3 before appearing
here.

Run:  uvicorn services.yard_api.main:app --port 8001 --reload   (from backend/)
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

# .env must load BEFORE event_bus is imported -- it reads REDIS_URL at module
# import time, so a later load would be ignored.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT.parent / ".env")

from event_bus import publish_to_redis, record_event  # noqa: E402
from shared.api import create_app  # noqa: E402
from shared.auth import PERM_YARD_WRITE, require  # noqa: E402
from shared.db import get_conn  # noqa: E402
from shared.dock_engine import decide  # noqa: E402
from shared.ids import next_id  # noqa: E402

app = create_app(
    "Yard API (E2)",
    description=(
        "Where's My Truck -- yard, dock door and delivery tracking. "
        "Owns shipments, trailers, tracking events, dock assignments, and is "
        "the ONLY writer of goods_receipts."
    ),
)

ETA_MATERIAL_CHANGE_MINUTES = 10


# ─────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────

class CreateShipmentRequest(BaseModel):
    po_id: str = Field(examples=["PO-1001"])
    tracking_number: Optional[str] = Field(default=None, examples=["TRK998231"])
    carrier: Optional[str] = Field(default=None, examples=["Swift Logistics"])
    origin_location_id: Optional[str] = Field(default=None, examples=["LOC-002"])
    destination_location_id: Optional[str] = Field(default=None, examples=["LOC-001"])
    expected_arrival: Optional[datetime] = None


class CreateTrailerRequest(BaseModel):
    load_type: Optional[str] = Field(default="dry_van", examples=["dry_van"])
    priority: str = Field(default="normal", examples=["high"])


class TrackingUpdateRequest(BaseModel):
    latitude: float
    longitude: float
    speed: Optional[float] = None
    eta_estimate: datetime


class UnloadRequest(BaseModel):
    qty_received: float = Field(examples=[500])


class ReassignRequest(BaseModel):
    new_dock_id: str = Field(examples=["DOCK-02"])
    reason: Optional[str] = Field(default="operator override", examples=["operator override"])


def _iso(value):
    return value.isoformat() if value else None


def _emit(conn, entity_type, entity_id, event_type, payload):
    """
    record_event inside the caller's open transaction. The caller commits, then
    calls _publish. Never commits here -- see event_bus.py's module docstring.
    """
    return record_event(conn, entity_type, entity_id, event_type, payload)


# ─────────────────────────────────────────────
# POST /shipments
# ─────────────────────────────────────────────

@app.post("/shipments", status_code=201, tags=["yard"],
          dependencies=[Depends(require(PERM_YARD_WRITE))])
def create_shipment(body: CreateShipmentRequest):
    """Supplier has begun fulfilling a PO -- the PR2 -> E2 handoff point."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT po.id, s.name FROM purchase_orders po
                   LEFT JOIN suppliers s ON s.id = po.supplier_id WHERE po.id = %s""",
                (body.po_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"purchase_order {body.po_id} not found")
            supplier_name = row[1] or "supplier"

            shipment_id = next_id(cur, "SHP")
            cur.execute(
                """INSERT INTO shipments (id, po_id, tracking_number, carrier,
                       origin_location_id, destination_location_id, expected_arrival, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'CREATED')""",
                (shipment_id, body.po_id, body.tracking_number, body.carrier,
                 body.origin_location_id, body.destination_location_id, body.expected_arrival),
            )

        payload = {
            "summary": f"{shipment_id} despatched by {body.carrier or 'carrier'} for {body.po_id}",
            "po_id": body.po_id,
            "carrier": body.carrier,
            "tracking_number": body.tracking_number,
            "supplier_name": supplier_name,
            "expected_arrival": _iso(body.expected_arrival),
        }
        event_id, created_at = _emit(conn, "shipment", shipment_id, "SHIPMENT_CREATED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "shipment", shipment_id,
                         "SHIPMENT_CREATED", payload, created_at)

    return {"id": shipment_id, "status": "CREATED"}


# ─────────────────────────────────────────────
# POST /shipments/{shipment_id}/trailers
# ─────────────────────────────────────────────

@app.post("/shipments/{shipment_id}/trailers", status_code=201, tags=["yard"],
          dependencies=[Depends(require(PERM_YARD_WRITE))])
def create_trailer(shipment_id: str, body: CreateTrailerRequest):
    """
    Trailer departs. This -- not SHIPMENT_CREATED -- is the real initial
    dock-scoring trigger: a trailer now exists to assign a dock to.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.id, s.po_id, s.carrier, s.expected_arrival
                   FROM shipments s WHERE s.id = %s""",
                (shipment_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"shipment {shipment_id} not found")
            _, po_id, carrier, expected_arrival = row

            trailer_id = next_id(cur, "TRL")
            cur.execute(
                """INSERT INTO trailers (id, shipment_id, load_type, priority, eta, status)
                   VALUES (%s,%s,%s,%s,%s,'EN_ROUTE')""",
                (trailer_id, shipment_id, body.load_type, body.priority, expected_arrival),
            )
            # Shipment moves CREATED -> EN_ROUTE. Specified in schema.sql's
            # status comment but never previously implemented, which left every
            # shipment stuck at CREATED and the pipeline funnel uncomputable.
            cur.execute("UPDATE shipments SET status='EN_ROUTE' WHERE id=%s", (shipment_id,))

        payload = {
            "summary": f"{trailer_id} departed for {po_id} "
                       f"({body.load_type}, {body.priority} priority)",
            "shipment_id": shipment_id,
            "po_id": po_id,
            "carrier": carrier,
            "load_type": body.load_type,
            "priority": body.priority,
            "eta": _iso(expected_arrival),
        }
        event_id, created_at = _emit(conn, "trailer", trailer_id, "TRAILER_DEPARTED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "trailer", trailer_id,
                         "TRAILER_DEPARTED", payload, created_at)

    return {"id": trailer_id, "status": "EN_ROUTE"}


# ─────────────────────────────────────────────
# POST /trailers/{trailer_id}/tracking
# ─────────────────────────────────────────────

@app.post("/trailers/{trailer_id}/tracking", status_code=201, tags=["yard"],
          dependencies=[Depends(require(PERM_YARD_WRITE))])
def post_tracking(trailer_id: str, body: TrackingUpdateRequest):
    """
    Simulator posts a GPS tick.

    Step order matters and is part of the locked contract: the OLD eta must be
    captured BEFORE the update, or the delta is always zero.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1. capture the OLD eta first
            cur.execute("SELECT eta, status FROM trailers WHERE id = %s", (trailer_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            old_eta, status = row

            # 2. append the tracking event
            cur.execute(
                """INSERT INTO tracking_events (trailer_id, latitude, longitude, speed, eta_estimate)
                   VALUES (%s,%s,%s,%s,%s)""",
                (trailer_id, body.latitude, body.longitude, body.speed, body.eta_estimate),
            )

            # 3. update the current eta
            cur.execute("UPDATE trailers SET eta=%s, updated_at=now() WHERE id=%s",
                        (body.eta_estimate, trailer_id))

        # 4. TRAILER_LOCATION_UPDATED always
        loc_payload = {
            "summary": f"{trailer_id} position updated",
            "latitude": body.latitude,
            "longitude": body.longitude,
            "speed": body.speed,
            "eta": _iso(body.eta_estimate),
            "status": status,
        }
        event_id, created_at = _emit(conn, "trailer", trailer_id,
                                     "TRAILER_LOCATION_UPDATED", loc_payload)

        # 5. ETA_UPDATED only when the change is material
        eta_changed = False
        eta_event = None
        eta_payload = None
        if old_eta is not None:
            if old_eta.tzinfo is None:
                old_eta = old_eta.replace(tzinfo=timezone.utc)
            delta_minutes = abs((body.eta_estimate - old_eta).total_seconds()) / 60
            if delta_minutes >= ETA_MATERIAL_CHANGE_MINUTES:
                eta_changed = True
                direction = "later" if body.eta_estimate > old_eta else "earlier"
                eta_payload = {
                    "summary": f"{trailer_id} ETA moved {round(delta_minutes)} min {direction}",
                    "previous_eta": _iso(old_eta),
                    "new_eta": _iso(body.eta_estimate),
                    "delta_minutes": round(delta_minutes, 1),
                    "direction": direction,
                }
                eta_event = _emit(conn, "trailer", trailer_id, "ETA_UPDATED", eta_payload)

        # 6. commit once, both events together
        conn.commit()
        publish_to_redis(conn, event_id, "trailer", trailer_id,
                         "TRAILER_LOCATION_UPDATED", loc_payload, created_at)
        if eta_changed and eta_event:
            publish_to_redis(conn, eta_event[0], "trailer", trailer_id,
                             "ETA_UPDATED", eta_payload, eta_event[1])

    return {"recorded": True, "eta_changed_materially": eta_changed}


# ─────────────────────────────────────────────
# POST /trailers/{trailer_id}/arrive
# ─────────────────────────────────────────────

@app.post("/trailers/{trailer_id}/arrive", tags=["yard"],
          dependencies=[Depends(require(PERM_YARD_WRITE))])
def arrive(trailer_id: str):
    """Trailer physically reaches the gate."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.status, t.shipment_id, d.dock_id
                   FROM trailers t
                   LEFT JOIN dock_assignments d
                     ON d.trailer_id = t.id AND d.status IN ('ASSIGNED','CONFIRMED')
                   WHERE t.id = %s""",
                (trailer_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            status, shipment_id, dock_id = row
            # Guard the transition. Without this an UNLOADED trailer could be
            # walked back to ARRIVED, corrupting turnaround-time KPIs.
            if status != "EN_ROUTE":
                raise HTTPException(409, f"trailer {trailer_id} is {status}, expected EN_ROUTE")

            cur.execute("UPDATE trailers SET status='ARRIVED', updated_at=now() WHERE id=%s",
                        (trailer_id,))
            cur.execute("UPDATE shipments SET status='ARRIVED' WHERE id=%s", (shipment_id,))

        payload = {
            "summary": f"{trailer_id} arrived at the gate"
                       + (f", assigned {dock_id}" if dock_id else ", no dock assigned"),
            "shipment_id": shipment_id,
            "dock_id": dock_id,
        }
        event_id, created_at = _emit(conn, "trailer", trailer_id, "TRAILER_ARRIVED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "trailer", trailer_id,
                         "TRAILER_ARRIVED", payload, created_at)

    return {"id": trailer_id, "status": "ARRIVED", "dock_id": dock_id}


# ─────────────────────────────────────────────
# POST /trailers/{trailer_id}/dock   (v4 -- BUILD_PLAN §2.3)
# ─────────────────────────────────────────────

@app.post("/trailers/{trailer_id}/dock", tags=["yard"],
          dependencies=[Depends(require(PERM_YARD_WRITE))])
def dock(trailer_id: str):
    """
    Trailer pulls into its assigned door and starts unloading.

    Nothing previously wrote trailers.status='DOCKED' or moved a dock
    assignment to CONFIRMED, so the yard board's Docked and Unloading states
    were unreachable even though both values were in the schema vocabulary.
    docked_at is what makes the unload-progress bar derivable.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM trailers WHERE id=%s", (trailer_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            if row[0] != "ARRIVED":
                raise HTTPException(409, f"trailer {trailer_id} is {row[0]}, expected ARRIVED")

            cur.execute(
                """SELECT id, dock_id FROM dock_assignments
                   WHERE trailer_id=%s AND status IN ('ASSIGNED','CONFIRMED')""",
                (trailer_id,),
            )
            assignment = cur.fetchone()
            if assignment is None:
                raise HTTPException(409, f"trailer {trailer_id} has no current dock assignment")
            assignment_id, dock_id = assignment

            cur.execute("UPDATE trailers SET status='DOCKED', updated_at=now() WHERE id=%s",
                        (trailer_id,))
            cur.execute(
                "UPDATE dock_assignments SET status='CONFIRMED', docked_at=now() WHERE id=%s",
                (assignment_id,),
            )

        payload = {
            "summary": f"{trailer_id} docked at {dock_id}, unloading started",
            "dock_id": dock_id,
            "dock_assignment_id": assignment_id,
        }
        event_id, created_at = _emit(conn, "trailer", trailer_id, "TRAILER_DOCKED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "trailer", trailer_id,
                         "TRAILER_DOCKED", payload, created_at)

    return {"id": trailer_id, "status": "DOCKED", "dock_id": dock_id,
            "dock_assignment_id": assignment_id}


# ─────────────────────────────────────────────
# POST /trailers/{trailer_id}/unload  -- the ONLY writer of goods_receipts
# ─────────────────────────────────────────────

@app.post("/trailers/{trailer_id}/unload", status_code=201, tags=["yard"],
          dependencies=[Depends(require(PERM_YARD_WRITE))])
def unload(trailer_id: str, body: UnloadRequest):
    """
    Unloading completes -- the E2 -> PR2 bridge point.

    goods_receipts is written ONLY here, by Yard API. PR2 reads it, never
    writes it. Locked rule, unchanged.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT shipment_id, status FROM trailers WHERE id=%s", (trailer_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            shipment_id, status = row
            if status not in ("ARRIVED", "DOCKED"):
                raise HTTPException(409, f"trailer {trailer_id} is {status}, cannot unload")

            cur.execute("SELECT po_id FROM shipments WHERE id=%s", (shipment_id,))
            po_id = cur.fetchone()[0]

            cur.execute("UPDATE trailers SET status='UNLOADED', updated_at=now() WHERE id=%s",
                        (trailer_id,))
            cur.execute("UPDATE shipments SET status='UNLOADED' WHERE id=%s", (shipment_id,))

            # Release the dock. Without this the dock engine's Stage 1 hard
            # constraint sees the door as occupied forever and it can never be
            # assigned again. Matches ASSIGNED/CONFIRMED/DELAYED, not just
            # ASSIGNED -- a trailer can reach unload in any of those states.
            cur.execute(
                """UPDATE dock_assignments SET status='COMPLETED'
                   WHERE trailer_id=%s AND status IN ('ASSIGNED','CONFIRMED','DELAYED')
                   RETURNING dock_id""",
                (trailer_id,),
            )
            released = cur.fetchone()
            released_dock = released[0] if released else None

            gr_id = next_id(cur, "GR")
            cur.execute(
                """INSERT INTO goods_receipts (id, trailer_id, shipment_id, po_id, qty_received)
                   VALUES (%s,%s,%s,%s,%s)""",
                (gr_id, trailer_id, shipment_id, po_id, body.qty_received),
            )

        payload = {
            "summary": f"{gr_id}: {body.qty_received} units received against {po_id}",
            "po_id": po_id,
            "qty_received": body.qty_received,
            "trailer_id": trailer_id,
            "shipment_id": shipment_id,
            "released_dock_id": released_dock,
        }
        event_id, created_at = _emit(conn, "goods_receipt", gr_id, "GOODS_RECEIVED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "goods_receipt", gr_id,
                         "GOODS_RECEIVED", payload, created_at)

    return {"goods_receipt_id": gr_id, "po_id": po_id, "released_dock_id": released_dock}


# ─────────────────────────────────────────────
# POST /dock-assignments/{id}/reassign
# ─────────────────────────────────────────────

@app.post("/dock-assignments/{assignment_id}/reassign", tags=["yard"],
          dependencies=[Depends(require(PERM_YARD_WRITE))])
def reassign(assignment_id: str, body: ReassignRequest):
    """
    Manual operator override. Never updates dock_id in place: the old row is
    marked REASSIGNED and a new one inserted, so dock_assignments stays a real
    history table and "why did the dock change from D4 to D2" is answerable.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trailer_id, dock_id, status FROM dock_assignments WHERE id=%s",
                (assignment_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"dock_assignment {assignment_id} not found")
            trailer_id, old_dock, old_status = row
            if old_status in ("REASSIGNED", "COMPLETED"):
                raise HTTPException(409, f"assignment {assignment_id} is already {old_status}")

            cur.execute("SELECT 1 FROM docks WHERE id=%s AND is_active", (body.new_dock_id,))
            if cur.fetchone() is None:
                raise HTTPException(404, f"dock {body.new_dock_id} not found or inactive")

            cur.execute("UPDATE dock_assignments SET status='REASSIGNED' WHERE id=%s",
                        (assignment_id,))

            new_id = next_id(cur, "DA")
            cur.execute(
                """INSERT INTO dock_assignments (id, trailer_id, dock_id, status, reason,
                                                 score_breakdown)
                   VALUES (%s,%s,%s,'ASSIGNED',%s,%s)""",
                (new_id, trailer_id, body.new_dock_id, body.reason,
                 '{"source": "manual_override"}'),
            )

        payload = {
            "summary": f"{trailer_id} reassigned {old_dock} -> {body.new_dock_id} "
                       f"({body.reason})",
            "old_assignment_id": assignment_id,
            "old_dock_id": old_dock,
            "new_dock_id": body.new_dock_id,
            "trailer_id": trailer_id,
            "reason": body.reason,
        }
        event_id, created_at = _emit(conn, "dock_assignment", new_id, "DOCK_REASSIGNED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "dock_assignment", new_id,
                         "DOCK_REASSIGNED", payload, created_at)

    return {"old_assignment_id": assignment_id, "new_assignment_id": new_id, "status": "ASSIGNED"}


# ─────────────────────────────────────────────
# GET /yard-status  (v4 extended response)
# ─────────────────────────────────────────────

@app.get("/yard-status", tags=["yard"])
def yard_status():
    """
    The E2 initial-load read. Current dock assignment only (ASSIGNED/CONFIRMED)
    -- REASSIGNED rows are history, not current state.

    Unload progress is DERIVED here, not stored: elapsed since docked_at over
    the dock's expected_unload_minutes.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.status, t.eta, t.priority, t.load_type, t.updated_at,
                       s.id, s.po_id, s.carrier, s.tracking_number,
                       da.id, da.dock_id, da.status, da.reason, da.docked_at,
                       d.metadata->>'expected_unload_minutes',
                       te.latitude, te.longitude
                FROM trailers t
                LEFT JOIN shipments s ON s.id = t.shipment_id
                LEFT JOIN dock_assignments da
                       ON da.trailer_id = t.id AND da.status IN ('ASSIGNED','CONFIRMED')
                LEFT JOIN docks d ON d.id = da.dock_id
                LEFT JOIN LATERAL (
                    SELECT latitude, longitude FROM tracking_events
                    WHERE trailer_id = t.id ORDER BY recorded_at DESC LIMIT 1
                ) te ON TRUE
                WHERE t.status <> 'UNLOADED'
                ORDER BY t.created_at DESC
            """)
            trailers = []
            for r in cur.fetchall():
                progress = None
                if r[14] and r[15]:
                    elapsed = (datetime.now(timezone.utc) - r[14]).total_seconds() / 60
                    progress = max(0, min(100, round(elapsed / float(r[15]) * 100)))
                trailers.append({
                    "id": r[0], "status": r[1], "eta": _iso(r[2]), "priority": r[3],
                    "load_type": r[4], "updated_at": _iso(r[5]),
                    "shipment_id": r[6], "po_id": r[7], "carrier": r[8],
                    "tracking_number": r[9],
                    "dock_assignment": (
                        {"id": r[10], "dock_id": r[11], "status": r[12], "reason": r[13],
                         "docked_at": _iso(r[14]), "unload_progress_pct": progress}
                        if r[10] else None
                    ),
                    "latitude": float(r[16]) if r[16] is not None else None,
                    "longitude": float(r[17]) if r[17] is not None else None,
                })

            cur.execute("""
                SELECT d.id, d.yard_position, d.compatible_load_types, d.is_active,
                       da.trailer_id, da.status, da.docked_at, da.reason,
                       d.metadata->>'expected_unload_minutes'
                FROM docks d
                LEFT JOIN dock_assignments da
                       ON da.dock_id = d.id AND da.status IN ('ASSIGNED','CONFIRMED')
                ORDER BY d.yard_position
            """)
            docks = []
            for r in cur.fetchall():
                progress = None
                if r[6] and r[8]:
                    elapsed = (datetime.now(timezone.utc) - r[6]).total_seconds() / 60
                    progress = max(0, min(100, round(elapsed / float(r[8]) * 100)))
                if not r[3]:
                    state = "BLOCKED"
                elif r[5] == "CONFIRMED":
                    state = "UNLOADING"
                elif r[5] == "ASSIGNED":
                    state = "RESERVED"
                else:
                    state = "EMPTY"
                docks.append({
                    "id": r[0], "yard_position": r[1], "compatible_load_types": r[2],
                    "is_active": r[3], "occupied": r[4] is not None,
                    "current_trailer_id": r[4], "assignment_status": r[5],
                    "assignment_reason": r[7], "state": state,
                    "unload_progress_pct": progress,
                })

        conn.rollback()

    return {"trailers": trailers, "docks": docks}


# ─────────────────────────────────────────────
# GET /trailers/{trailer_id}  -- full history
# ─────────────────────────────────────────────

@app.get("/trailers/{trailer_id}", tags=["yard"])
def trailer_detail(trailer_id: str):
    """
    The "show me what happened to TRL-3391" panel-Q&A endpoint.
    dock_assignments here is the FULL history, including REASSIGNED rows --
    that is what answers "why did the dock change", not just the current one.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.id, t.status, t.eta, t.priority, t.load_type, t.created_at,
                          s.id, s.po_id, s.carrier, s.tracking_number, s.expected_arrival
                   FROM trailers t LEFT JOIN shipments s ON s.id = t.shipment_id
                   WHERE t.id = %s""",
                (trailer_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            trailer = {
                "id": row[0], "status": row[1], "eta": _iso(row[2]), "priority": row[3],
                "load_type": row[4], "created_at": _iso(row[5]),
                "shipment_id": row[6], "po_id": row[7], "carrier": row[8],
                "tracking_number": row[9], "expected_arrival": _iso(row[10]),
            }

            cur.execute(
                """SELECT latitude, longitude, speed, eta_estimate, recorded_at
                   FROM tracking_events WHERE trailer_id=%s ORDER BY recorded_at""",
                (trailer_id,),
            )
            tracking = [
                {"latitude": float(r[0]) if r[0] is not None else None,
                 "longitude": float(r[1]) if r[1] is not None else None,
                 "speed": float(r[2]) if r[2] is not None else None,
                 "eta_estimate": _iso(r[3]), "recorded_at": _iso(r[4])}
                for r in cur.fetchall()
            ]

            cur.execute(
                """SELECT id, dock_id, status, reason, assigned_at, docked_at, score_breakdown
                   FROM dock_assignments WHERE trailer_id=%s ORDER BY assigned_at""",
                (trailer_id,),
            )
            dock_history = [
                {"id": r[0], "dock_id": r[1], "status": r[2], "reason": r[3],
                 "assigned_at": _iso(r[4]), "docked_at": _iso(r[5]), "score_breakdown": r[6]}
                for r in cur.fetchall()
            ]

            cur.execute(
                """SELECT event_type, created_at, payload FROM event_log
                   WHERE entity_type='trailer' AND entity_id=%s ORDER BY created_at""",
                (trailer_id,),
            )
            events = [
                {"event_type": r[0], "created_at": _iso(r[1]), "payload": r[2]}
                for r in cur.fetchall()
            ]

        conn.rollback()

    return {"trailer": trailer, "tracking_history": tracking,
            "dock_assignment_history": dock_history, "events": events}
