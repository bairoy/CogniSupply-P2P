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

import json
import sys
from datetime import datetime, timedelta, timezone
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
from shared.dock_engine import DEFAULT_SERVICE_MINUTES  # noqa: E402
from shared.ids import next_id  # noqa: E402

app = create_app(
    "CogniSupply P2P — Yard API (E2)",
    description=(
        "Where's My Truck -- yard, dock door and delivery tracking, inbound "
        "and outbound. Owns shipments, trailers, tracking events, dock "
        "assignments, outbound orders and load plans, and is the ONLY writer "
        "of goods_receipts and goods_issues."
    ),
)

# v7: outbound lives in its own module, but the SAME app -- an outbound truck
# uses this file's /tracking, /arrive and /dock handlers unchanged. See
# outbound.py's docstring for why only the two ends of the journey differ.
from services.yard_api.outbound import router as outbound_router  # noqa: E402

app.include_router(outbound_router)

ETA_MATERIAL_CHANGE_MINUTES = 10

# How far ahead GET /dock-schedule looks when it reports utilisation. Long
# enough to cover every trailer currently inbound, short enough that a door
# booked once tomorrow does not read as "busy".
SCHEDULE_HORIZON_HOURS = 12


# ─────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────

class CreateShipmentRequest(BaseModel):
    po_id: str = Field(examples=["PO-1001"])
    tracking_number: Optional[str] = Field(default=None, examples=["TRK998231"])
    carrier: Optional[str] = Field(default=None, examples=["VRL Logistics"])
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


def _earliest_free_slot(cur, dock_id, ready, service_minutes, *, exclude_assignment_id=None):
    """
    First moment at or after `ready` when `dock_id` is free for long enough.

    Same first-fit rule the scheduler uses (shared/dock_engine._earliest_slot),
    expressed against the database because this path has one dock and one
    trailer to place, not a whole yard to optimise -- pulling the full planner
    in for that would be machinery, not intelligence.
    """
    cur.execute(
        """SELECT COALESCE(docked_at, planned_start), planned_end
           FROM dock_assignments
           WHERE dock_id=%s AND status IN ('ASSIGNED','CONFIRMED')
             AND planned_end IS NOT NULL AND (%s IS NULL OR id <> %s)
           ORDER BY 1""",
        (dock_id, exclude_assignment_id, exclude_assignment_id),
    )
    start = ready
    for busy_start, busy_end in cur.fetchall():
        if busy_start is None or busy_end <= start:
            continue
        if (busy_start - start).total_seconds() / 60 >= service_minutes:
            return start
        start = max(start, busy_end)
    return start


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
            cur.execute("SELECT status, direction FROM trailers WHERE id=%s", (trailer_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            if row[0] != "ARRIVED":
                raise HTTPException(409, f"trailer {trailer_id} is {row[0]}, expected ARRIVED")
            direction = row[1]

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
            # v7: an outbound order is LOADING from the moment its truck is at the
            # door. Without this the status sits at STAGED until the load
            # finishes, which would leave LOADING declared in the schema
            # vocabulary and never reachable -- the exact defect v4 had to fix for
            # DOCKED and CONFIRMED.
            if direction == "OUTBOUND":
                cur.execute(
                    """UPDATE outbound_orders SET status='LOADING', updated_at=now()
                       WHERE id = (SELECT outbound_order_id FROM shipments
                                   WHERE id = (SELECT shipment_id FROM trailers WHERE id=%s))
                         AND status = 'STAGED'""",
                    (trailer_id,),
                )

        payload = {
            "summary": f"{trailer_id} docked at {dock_id}, "
                       + ("loading started" if direction == "OUTBOUND" else "unloading started"),
            "dock_id": dock_id,
            "dock_assignment_id": assignment_id,
            "direction": direction,
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
            cur.execute("SELECT shipment_id, status, direction FROM trailers WHERE id=%s",
                        (trailer_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            shipment_id, status, direction = row
            # v7: the mirror of the guard in outbound.load_trailer(). Unloading an
            # outbound trailer would write a goods RECEIPT for goods that are
            # leaving, which would then be fed to the 3-way matcher against a PO
            # that does not exist. Cheap check, expensive failure.
            if direction == "OUTBOUND":
                raise HTTPException(
                    409,
                    f"trailer {trailer_id} is OUTBOUND -- use POST /trailers/{trailer_id}/load",
                )
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
# POST /trailers/{trailer_id}/depart   (v6)
# ─────────────────────────────────────────────

@app.post("/trailers/{trailer_id}/depart", tags=["yard"],
          dependencies=[Depends(require(PERM_YARD_WRITE))])
def depart(trailer_id: str):
    """
    Trailer clears the gate.

    UNLOADED and DEPARTED are deliberately different states. The door is
    released when the goods move; the tractor is still occupying the yard until
    it leaves. Before v6 an unloaded trailer simply disappeared from the board,
    which made "how many trailers are actually in my yard" unanswerable.

    v7: one gate, both directions. The valid predecessor is UNLOADED for an
    inbound trailer and LOADED for an outbound one -- the same moment in each
    story (goods have moved, door is free, truck is still on site), which is
    why it is one endpoint emitting one event rather than two of each. A gate
    does not care which way the pallets went.

    No dock work happens here -- the door was already released by unload/load.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, shipment_id, direction FROM trailers WHERE id=%s",
                        (trailer_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            status, shipment_id, direction = row
            expected = "LOADED" if direction == "OUTBOUND" else "UNLOADED"
            if status != expected:
                raise HTTPException(
                    409,
                    f"trailer {trailer_id} is {status}, expected {expected} "
                    f"for an {direction.lower()} trailer",
                )

            cur.execute("UPDATE trailers SET status='DEPARTED', updated_at=now() WHERE id=%s",
                        (trailer_id,))
            # An outbound shipment is not finished at the gate -- it still has to
            # land at the customer -- so the shipment advances here too. The
            # inbound side deliberately does not: its shipment reached its
            # terminal UNLOADED state when the goods came off.
            if direction == "OUTBOUND":
                cur.execute("UPDATE shipments SET status='DEPARTED' WHERE id=%s", (shipment_id,))

        payload = {
            "summary": f"{trailer_id} cleared the gate",
            "shipment_id": shipment_id,
            "trailer_id": trailer_id,
            "direction": direction,
        }
        event_id, created_at = _emit(conn, "trailer", trailer_id, "TRAILER_EXITED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "trailer", trailer_id,
                         "TRAILER_EXITED", payload, created_at)

    return {"id": trailer_id, "status": "DEPARTED"}


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

    v6: the override also gets a planned window, placed at the earliest moment
    the chosen door is genuinely free at or after the trailer is ready. The
    operator picks the door -- that choice is honoured verbatim and pinned, so
    the scheduler will never quietly undo it -- but two trailers cannot occupy
    one door at once, so the WHEN is still computed rather than asserted.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT da.trailer_id, da.dock_id, da.status, t.eta, t.status
                   FROM dock_assignments da
                   JOIN trailers t ON t.id = da.trailer_id
                   WHERE da.id=%s""",
                (assignment_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"dock_assignment {assignment_id} not found")
            trailer_id, old_dock, old_status, eta, trailer_status = row
            if old_status in ("REASSIGNED", "COMPLETED"):
                raise HTTPException(409, f"assignment {assignment_id} is already {old_status}")

            cur.execute(
                """SELECT COALESCE((metadata->>'expected_unload_minutes')::numeric, %s)::int
                   FROM docks WHERE id=%s AND is_active""",
                (DEFAULT_SERVICE_MINUTES, body.new_dock_id),
            )
            dock_row = cur.fetchone()
            if dock_row is None:
                raise HTTPException(404, f"dock {body.new_dock_id} not found or inactive")
            service_minutes = dock_row[0]

            now = datetime.now(timezone.utc)
            ready = now if trailer_status == "ARRIVED" else max(eta or now, now)
            planned_start = _earliest_free_slot(cur, body.new_dock_id, ready, service_minutes,
                                                exclude_assignment_id=assignment_id)
            planned_end = planned_start + timedelta(minutes=service_minutes)

            cur.execute("UPDATE dock_assignments SET status='REASSIGNED' WHERE id=%s",
                        (assignment_id,))

            new_id = next_id(cur, "DA")
            breakdown = {
                "source": "manual_override",
                "planned_start": planned_start.isoformat(),
                "planned_end": planned_end.isoformat(),
                "wait_minutes": max(0, round((planned_start - ready).total_seconds() / 60)),
                "service_minutes": service_minutes,
                "overridden_from": old_dock,
                "note": ("operator override -- pinned, the scheduler plans other "
                         "trailers around it rather than reversing it"),
            }
            cur.execute(
                """INSERT INTO dock_assignments (id, trailer_id, dock_id, status, reason,
                                                 score_breakdown, planned_start, planned_end)
                   VALUES (%s,%s,%s,'ASSIGNED',%s,%s,%s,%s)""",
                (new_id, trailer_id, body.new_dock_id, body.reason,
                 json.dumps(breakdown), planned_start, planned_end),
            )

        payload = {
            "summary": f"{trailer_id} reassigned {old_dock} -> {body.new_dock_id} "
                       f"({body.reason})",
            "old_assignment_id": assignment_id,
            "old_dock_id": old_dock,
            "new_dock_id": body.new_dock_id,
            "trailer_id": trailer_id,
            "reason": body.reason,
            "planned_start": planned_start.isoformat(),
            "planned_end": planned_end.isoformat(),
            "wait_minutes": breakdown["wait_minutes"],
            # dock-worker re-plans every other trailer around this override
            # instead of ignoring the event as one of its own. See its module
            # docstring on why that cannot loop.
            "source": "operator",
        }
        event_id, created_at = _emit(conn, "dock_assignment", new_id, "DOCK_REASSIGNED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "dock_assignment", new_id,
                         "DOCK_REASSIGNED", payload, created_at)

    return {"old_assignment_id": assignment_id, "new_assignment_id": new_id,
            "status": "ASSIGNED", "planned_start": _iso(planned_start),
            "planned_end": _iso(planned_end), "wait_minutes": breakdown["wait_minutes"]}


# ─────────────────────────────────────────────
# GET /yard-status  (v4 extended response)
# ─────────────────────────────────────────────

@app.get("/yard-status", tags=["yard"])
def yard_status(direction: Optional[str] = None):
    """
    The E2 initial-load read. Current dock assignment only (ASSIGNED/CONFIRMED)
    -- REASSIGNED rows are history, not current state.

    Two things are DERIVED here rather than stored, because storing either
    would need a writer ticking it every few seconds:
      * unload/load progress -- elapsed since docked_at over expected minutes
      * waiting time         -- elapsed since the trailer arrived at the gate,
                                which is the E2 KPI the use case actually names

    v6: trailers that are UNLOADED but have not yet cleared the gate stay on
    the board (status DEPARTED is what removes them), and every assignment
    carries its planned window so the board can show WHEN, not just WHERE.

    v7: one board, both directions. `direction` filters to INBOUND or OUTBOUND;
    omitting it returns BOTH, which is the honest default -- a yard is one yard,
    the doors are one pool, and a board that hid half the trucks contending for
    them would misrepresent the thing it is drawing. The filter exists for the
    UI's tabs, not because the two halves are separate systems.
    """
    if direction is not None:
        direction = direction.upper()
        if direction not in ("INBOUND", "OUTBOUND"):
            raise HTTPException(422, "direction must be INBOUND or OUTBOUND")

    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.status, t.eta, t.priority, t.load_type, t.updated_at,
                       s.id, s.po_id, s.carrier, s.tracking_number,
                       da.id, da.dock_id, da.status, da.reason, da.docked_at,
                       d.metadata->>'expected_unload_minutes',
                       te.latitude, te.longitude,
                       da.planned_start, da.planned_end,
                       (da.score_breakdown->>'wait_minutes')::int,
                       arr.arrived_at,
                       t.direction, s.outbound_order_id, o.customer_name
                FROM trailers t
                LEFT JOIN shipments s ON s.id = t.shipment_id
                LEFT JOIN outbound_orders o ON o.id = s.outbound_order_id
                LEFT JOIN dock_assignments da
                       ON da.trailer_id = t.id AND da.status IN ('ASSIGNED','CONFIRMED')
                LEFT JOIN docks d ON d.id = da.dock_id
                LEFT JOIN LATERAL (
                    SELECT latitude, longitude FROM tracking_events
                    WHERE trailer_id = t.id ORDER BY recorded_at DESC LIMIT 1
                ) te ON TRUE
                LEFT JOIN LATERAL (
                    SELECT created_at AS arrived_at FROM event_log
                    WHERE entity_type='trailer' AND entity_id = t.id
                      AND event_type='TRAILER_ARRIVED'
                    ORDER BY created_at DESC LIMIT 1
                ) arr ON TRUE
                WHERE t.status NOT IN ('DEPARTED','DELIVERED')
                  AND (t.status NOT IN ('UNLOADED','LOADED')
                       OR t.updated_at > now() - interval '12 hours')
                  AND (%s IS NULL OR t.direction = %s)
                ORDER BY t.created_at DESC
            """, (direction, direction))
            trailers = []
            for r in cur.fetchall():
                progress = None
                if r[14] and r[15]:
                    elapsed = (now - r[14]).total_seconds() / 60
                    progress = max(0, min(100, round(elapsed / float(r[15]) * 100)))
                # Waiting time is only meaningful between arriving at the gate
                # and getting to a door -- once docked, it stops accruing.
                waiting = None
                if r[21] and r[1] == "ARRIVED":
                    waiting = max(0, round((now - r[21]).total_seconds() / 60))
                trailers.append({
                    "id": r[0], "status": r[1], "eta": _iso(r[2]), "priority": r[3],
                    "load_type": r[4], "updated_at": _iso(r[5]),
                    "shipment_id": r[6], "po_id": r[7], "carrier": r[8],
                    "tracking_number": r[9],
                    "dock_assignment": (
                        {"id": r[10], "dock_id": r[11], "status": r[12], "reason": r[13],
                         "docked_at": _iso(r[14]), "unload_progress_pct": progress,
                         "planned_start": _iso(r[18]), "planned_end": _iso(r[19]),
                         "planned_wait_minutes": r[20]}
                        if r[10] else None
                    ),
                    "latitude": float(r[16]) if r[16] is not None else None,
                    "longitude": float(r[17]) if r[17] is not None else None,
                    "arrived_at": _iso(r[21]),
                    "waiting_minutes": waiting,
                    # v7. An inbound trailer is identified by the PO it fulfils,
                    # an outbound one by the customer order it collects -- the
                    # board's "what is this truck here for" column, whichever
                    # way it is pointing.
                    "direction": r[22],
                    "outbound_order_id": r[23],
                    "customer_name": r[24],
                })

            cur.execute("""
                SELECT d.id, d.yard_position, d.compatible_load_types, d.is_active,
                       da.trailer_id, da.status, da.docked_at, da.reason,
                       COALESCE((d.metadata->>'expected_unload_minutes')::numeric, %s)::int,
                       da.planned_start, da.planned_end,
                       nxt.trailer_id, nxt.planned_start,
                       cur_t.direction, nxt_t.direction
                FROM docks d
                -- The window in progress right now: a trailer physically at the
                -- door, else the booking whose slot has already started. One row
                -- per dock -- a door has at most one current occupant, and the
                -- rest of its bookings belong to /dock-schedule, not here.
                LEFT JOIN LATERAL (
                    SELECT id, trailer_id, status, docked_at, reason,
                           planned_start, planned_end
                    FROM dock_assignments
                    WHERE dock_id = d.id AND status IN ('ASSIGNED','CONFIRMED')
                      AND (docked_at IS NOT NULL
                           OR COALESCE(planned_start, assigned_at) <= now())
                    ORDER BY docked_at DESC NULLS LAST, planned_start
                    LIMIT 1
                ) da ON TRUE
                LEFT JOIN LATERAL (
                    SELECT trailer_id, planned_start FROM dock_assignments
                    WHERE dock_id = d.id AND status IN ('ASSIGNED','CONFIRMED')
                      AND docked_at IS NULL AND planned_start > now()
                    ORDER BY planned_start LIMIT 1
                ) nxt ON TRUE
                LEFT JOIN trailers cur_t ON cur_t.id = da.trailer_id
                LEFT JOIN trailers nxt_t ON nxt_t.id = nxt.trailer_id
                ORDER BY d.yard_position
            """, (DEFAULT_SERVICE_MINUTES,))
            docks = []
            for r in cur.fetchall():
                progress = None
                if r[6]:
                    elapsed = (now - r[6]).total_seconds() / 60
                    progress = max(0, min(100, round(elapsed / float(r[8]) * 100)))
                current_direction = r[13]
                if not r[3]:
                    state = "BLOCKED"
                elif r[5] == "CONFIRMED":
                    # v7: same door, same occupancy, opposite verb. The board has
                    # to say which is happening -- "DOCK-07 busy" is not
                    # actionable, "DOCK-07 loading, 60%" is.
                    state = "LOADING" if current_direction == "OUTBOUND" else "UNLOADING"
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
                    "service_minutes": r[8],
                    "window_start": _iso(r[9]), "window_end": _iso(r[10]),
                    "next_trailer_id": r[11], "next_start": _iso(r[12]),
                    "direction": current_direction,
                    "next_direction": r[14],
                })

        conn.rollback()

    return {"trailers": trailers, "docks": docks, "summary": _yard_summary(trailers, docks)}


def _yard_summary(trailers, docks):
    """
    The movement picture in one object: what is approaching, what is at a door,
    what is waiting to leave. Computed from the rows already fetched rather
    than by a second round of queries.

    v7 keeps the original keys meaning exactly what they always meant, and adds
    direction-split ones alongside. `inbound` still counts EN_ROUTE inbound
    trailers -- redefining an existing key to mean "approaching, either
    direction" would silently change every dashboard already reading it, which
    is the status-vocabulary append-only rule applied to a response shape.
    """
    def count(*statuses, direction=None):
        return sum(1 for t in trailers
                   if t["status"] in statuses
                   and (direction is None or t.get("direction") == direction))

    waits = [t["waiting_minutes"] for t in trailers if t["waiting_minutes"] is not None]
    active_docks = [d for d in docks if d["is_active"]]
    busy = [d for d in active_docks if d["occupied"]]
    return {
        # Unchanged meaning: the inbound picture.
        "inbound": count("EN_ROUTE", direction="INBOUND"),
        "in_yard_waiting": count("ARRIVED"),
        "at_door": count("DOCKED"),
        "awaiting_exit": count("UNLOADED", "LOADED"),
        "unassigned": sum(1 for t in trailers
                          if t["dock_assignment"] is None
                          and t["status"] in ("EN_ROUTE", "ARRIVED")),
        # v7 additions.
        "outbound_en_route": count("EN_ROUTE", direction="OUTBOUND"),
        "outbound_in_yard": count("ARRIVED", direction="OUTBOUND"),
        "outbound_at_door": count("DOCKED", direction="OUTBOUND"),
        "outbound_loaded": count("LOADED", direction="OUTBOUND"),
        "inbound_at_door": count("DOCKED", direction="INBOUND"),
        "trailers_on_site": count("ARRIVED", "DOCKED", "UNLOADED", "LOADED"),
        "docks_total": len(docks),
        "docks_active": len(active_docks),
        "docks_busy": len(busy),
        "docks_loading": sum(1 for d in active_docks if d["state"] == "LOADING"),
        "docks_unloading": sum(1 for d in active_docks if d["state"] == "UNLOADING"),
        "dock_occupancy_pct": round(len(busy) / len(active_docks) * 100) if active_docks else 0,
        "avg_wait_minutes": round(sum(waits) / len(waits)) if waits else 0,
        "longest_wait_minutes": max(waits) if waits else 0,
    }


# ─────────────────────────────────────────────
# GET /dock-schedule   (v6)
# ─────────────────────────────────────────────

@app.get("/dock-schedule", tags=["yard"])
def dock_schedule(hours: int = SCHEDULE_HORIZON_HOURS):
    """
    The door timeline: for each dock, the windows committed on it over the next
    `hours`, plus the utilisation that follows from them.

    This is the read that /yard-status could never be -- a yard board answers
    "what is happening now", a schedule answers "what is this door doing for
    the rest of the shift", which is the question dock-door availability
    actually means. Booked minutes come from the planned windows the scheduler
    wrote, so utilisation here is measured against the real plan, not estimated.
    """
    now = datetime.now(timezone.utc)
    horizon_end = now + timedelta(hours=hours)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, yard_position, compatible_load_types, is_active,
                          COALESCE((metadata->>'expected_unload_minutes')::numeric, %s)::int
                   FROM docks ORDER BY yard_position""",
                (DEFAULT_SERVICE_MINUTES,),
            )
            docks = {
                r[0]: {"id": r[0], "yard_position": r[1], "compatible_load_types": r[2],
                       "is_active": r[3], "service_minutes": r[4], "bookings": [],
                       "committed_minutes": 0, "utilisation_pct": 0}
                for r in cur.fetchall()
            }

            cur.execute(
                """SELECT da.id, da.dock_id, da.trailer_id, da.status, da.reason,
                          COALESCE(da.docked_at, da.planned_start) AS window_start,
                          da.planned_end, da.docked_at IS NOT NULL AS in_progress,
                          t.priority, t.load_type, t.status, s.po_id, s.carrier,
                          (da.score_breakdown->>'wait_minutes')::int,
                          da.score_breakdown->>'source'
                   FROM dock_assignments da
                   JOIN trailers t ON t.id = da.trailer_id
                   LEFT JOIN shipments s ON s.id = t.shipment_id
                   WHERE da.status IN ('ASSIGNED','CONFIRMED')
                     AND da.planned_end IS NOT NULL
                     AND da.planned_end >= %s AND COALESCE(da.docked_at, da.planned_start) <= %s
                   ORDER BY da.dock_id, window_start""",
                (now - timedelta(hours=1), horizon_end),
            )
            for r in cur.fetchall():
                dock = docks.get(r[1])
                if dock is None:
                    continue
                start, end = r[5], r[6]
                dock["bookings"].append({
                    "assignment_id": r[0], "trailer_id": r[2], "assignment_status": r[3],
                    "reason": r[4], "start": _iso(start), "end": _iso(end),
                    "in_progress": r[7], "priority": r[8], "load_type": r[9],
                    "trailer_status": r[10], "po_id": r[11], "carrier": r[12],
                    "wait_minutes": r[13], "source": r[14] or "dock-worker",
                })
                overlap_start, overlap_end = max(start, now), min(end, horizon_end)
                if overlap_end > overlap_start:
                    dock["committed_minutes"] += round(
                        (overlap_end - overlap_start).total_seconds() / 60)

        conn.rollback()

    horizon_minutes = hours * 60
    for dock in docks.values():
        if dock["is_active"]:
            dock["utilisation_pct"] = min(
                100, round(dock["committed_minutes"] / horizon_minutes * 100))

    active = [d for d in docks.values() if d["is_active"]]
    return {
        "generated_at": _iso(now),
        "horizon_hours": hours,
        "docks": list(docks.values()),
        "summary": {
            "docks_total": len(docks),
            "docks_active": len(active),
            "booked_windows": sum(len(d["bookings"]) for d in docks.values()),
            "utilisation_pct": (
                round(sum(d["committed_minutes"] for d in active)
                      / (len(active) * horizon_minutes) * 100) if active else 0),
        },
    }


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
