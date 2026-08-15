"""
Outbound yard operations (E2, v7). Mounted into Yard API by main.py.

Every endpoint matches docs/api-contract.md's v7 section -- method, URL,
request/response shape, tables touched, event(s) emitted, transaction boundary.

WHY THIS FILE IS SHORT

Outbound looks like it should be a mirror image of inbound, and it very nearly
is -- which is exactly why almost none of it is here. The middle of an outbound
truck's journey is byte-for-byte the inbound one:

    en route -> gate in -> yard -> DOCK ASSIGNMENT -> at dock

An outbound truck posts GPS to POST /trailers/{id}/tracking, arrives via
POST /trailers/{id}/arrive, and takes a door via POST /trailers/{id}/dock --
the same handlers in main.py, writing the same trailers/tracking_events/
dock_assignments rows, planned by the same CP-SAT pass. `direction` is only
read where the two genuinely differ.

So this file holds the two ENDS that are actually different:

    order -> pick/load plan -> staged        (before the truck matters)
    ...
    loading -> goods issue -> gate out -> delivered   (after the door frees)

That split is not a tidiness preference. A dock door is one scarce resource
that inbound and outbound trucks contend for AT THE SAME TIME. Giving outbound
its own scheduler -- or even its own trailer table -- would mean two planners
each believing they own the doors, and the first symptom would be two trucks
promised DOCK-07 for the same fifteen minutes. One optimiser over one set of
trailers is the only version of this that is correct, and CLAUDE.md locks it.

THE ONE ORDERING RULE OUTBOUND ADDS

Inbound has no readiness precondition -- goods arrive whether the yard is ready
or not. Outbound does: a door must never be committed to a load that has not
been picked. That is why POST /outbound-orders/{id}/dispatch refuses anything
that is not STAGED, and it is the only sequencing constraint inbound lacks.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from event_bus import publish_to_redis, record_event  # noqa: E402
from shared.auth import PERM_OUTBOUND_WRITE, require  # noqa: E402
from shared.db import get_conn  # noqa: E402
from shared.ids import next_id  # noqa: E402

router = APIRouter()

# Statuses a load line is finished moving through at staging time. A line is
# either picked in full (STAGED) or picked short (SHORT) -- both are resolved,
# and a short pick still ships, so both let the order proceed.
STAGED_TERMINAL = ("STAGED", "SHORT")


def _iso(value):
    return value.isoformat() if value else None


def _num(value):
    """NUMERIC comes back from psycopg2 as Decimal, which json cannot encode."""
    return float(value) if value is not None else None


def _emit(conn, entity_type, entity_id, event_type, payload):
    """record_event inside the caller's open transaction -- never commits here."""
    return record_event(conn, entity_type, entity_id, event_type, payload)


# ─────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────

class OrderLine(BaseModel):
    material_id: str = Field(examples=["MAT-003"])
    qty: float = Field(examples=[120])


class CreateOutboundOrderRequest(BaseModel):
    customer_name: str = Field(examples=["DMart Retail"])
    destination_location_id: Optional[str] = Field(default=None, examples=["LOC-004"])
    requested_ship_date: Optional[datetime] = None
    priority: str = Field(default="normal", examples=["high"])
    lines: list[OrderLine] = Field(min_length=1)


class StageLine(BaseModel):
    load_plan_id: str = Field(examples=["LP-1001"])
    qty_staged: float = Field(examples=[120])


class StageRequest(BaseModel):
    # Empty body means "everything picked in full", which is both the simulator's
    # path and the common warehouse case. Making the detailed form optional keeps
    # the short-pick scenario expressible without making the happy path verbose.
    lines: Optional[list[StageLine]] = None


class DispatchRequest(BaseModel):
    carrier: Optional[str] = Field(default=None, examples=["VRL Logistics"])
    tracking_number: Optional[str] = Field(default=None, examples=["TRK441902"])
    load_type: Optional[str] = Field(default="dry_van", examples=["dry_van"])
    priority: Optional[str] = Field(default=None, examples=["high"])
    eta: Optional[datetime] = Field(
        default=None,
        description="When the collecting truck reaches OUR gate -- the dock "
                    "scheduler plans against this, exactly as it does for an "
                    "inbound trailer's ETA.",
    )


class LoadLine(BaseModel):
    load_plan_id: str
    qty_loaded: float


class LoadRequest(BaseModel):
    lines: Optional[list[LoadLine]] = None


# ─────────────────────────────────────────────
# POST /outbound-orders
# ─────────────────────────────────────────────

@router.post("/outbound-orders", status_code=201, tags=["outbound"],
             dependencies=[Depends(require(PERM_OUTBOUND_WRITE))])
def create_outbound_order(body: CreateOutboundOrderRequest):
    """
    A customer order enters the yard's world, together with the pick lines that
    fulfil it.

    The order and its load plan are written in ONE transaction and the order
    lands on PLANNED, not CREATED. An order whose lines failed to write would be
    an order the warehouse can never pick -- there is no useful intermediate
    state where one exists without the other, so there is no intermediate commit.
    Two events are still emitted, because "an order arrived" and "here is what
    we will pick" are genuinely different facts to anyone watching the stream.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if body.destination_location_id:
                cur.execute("SELECT id FROM locations WHERE id=%s",
                            (body.destination_location_id,))
                if cur.fetchone() is None:
                    raise HTTPException(404, f"location {body.destination_location_id} not found")

            material_ids = [ln.material_id for ln in body.lines]
            cur.execute("SELECT id, name FROM materials WHERE id = ANY(%s)", (material_ids,))
            known = dict(cur.fetchall())
            missing = [m for m in material_ids if m not in known]
            if missing:
                raise HTTPException(404, f"unknown material(s): {', '.join(sorted(set(missing)))}")

            order_id = next_id(cur, "OBO")
            cur.execute(
                """INSERT INTO outbound_orders
                       (id, customer_name, destination_location_id, requested_ship_date,
                        priority, status)
                   VALUES (%s,%s,%s,%s,%s,'PLANNED')""",
                (order_id, body.customer_name, body.destination_location_id,
                 body.requested_ship_date, body.priority),
            )

            lines_out = []
            for ln in body.lines:
                lp_id = next_id(cur, "LP")
                cur.execute(
                    """INSERT INTO load_plans
                           (id, outbound_order_id, material_id, qty_ordered, status)
                       VALUES (%s,%s,%s,%s,'PLANNED')""",
                    (lp_id, order_id, ln.material_id, ln.qty),
                )
                lines_out.append({"load_plan_id": lp_id, "material_id": ln.material_id,
                                  "material_name": known[ln.material_id], "qty_ordered": ln.qty})

        total_qty = sum(ln["qty_ordered"] for ln in lines_out)
        created_payload = {
            "summary": f"{order_id}: {body.customer_name} ordered "
                       f"{total_qty:g} units across {len(lines_out)} line(s)",
            "outbound_order_id": order_id,
            "customer_name": body.customer_name,
            "destination_location_id": body.destination_location_id,
            "priority": body.priority,
            "requested_ship_date": _iso(body.requested_ship_date),
            "line_count": len(lines_out),
            "total_qty": total_qty,
            "direction": "OUTBOUND",
        }
        plan_payload = {
            "summary": f"{order_id}: load plan created, {len(lines_out)} line(s) to pick",
            "outbound_order_id": order_id,
            "lines": lines_out,
            "total_qty": total_qty,
            "direction": "OUTBOUND",
        }
        ev1, at1 = _emit(conn, "outbound_order", order_id,
                         "OUTBOUND_ORDER_CREATED", created_payload)
        ev2, at2 = _emit(conn, "outbound_order", order_id,
                         "LOAD_PLAN_CREATED", plan_payload)
        conn.commit()
        publish_to_redis(conn, ev1, "outbound_order", order_id,
                         "OUTBOUND_ORDER_CREATED", created_payload, at1)
        publish_to_redis(conn, ev2, "outbound_order", order_id,
                         "LOAD_PLAN_CREATED", plan_payload, at2)

    return {"id": order_id, "status": "PLANNED", "lines": lines_out}


# ─────────────────────────────────────────────
# POST /outbound-orders/{id}/stage
# ─────────────────────────────────────────────

@router.post("/outbound-orders/{order_id}/stage", tags=["outbound"],
             dependencies=[Depends(require(PERM_OUTBOUND_WRITE))])
def stage_order(order_id: str, body: StageRequest = StageRequest()):
    """
    Goods are picked to the staging lane.

    This is outbound's readiness gate, and it has no inbound equivalent: goods
    arrive whether or not the yard is ready, but a truck must not be given a
    door for a load nobody has picked yet. Only once every line is resolved
    does the order become STAGED and therefore dispatchable.

    A short pick is recorded as SHORT rather than rejected. A truck that turns
    up for 500 units and can only be given 480 still leaves with 480 -- refusing
    to model that would mean the system disagrees with the warehouse floor.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, customer_name FROM outbound_orders WHERE id=%s",
                        (order_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"outbound order {order_id} not found")
            status, customer = row
            if status not in ("CREATED", "PLANNED", "STAGED"):
                raise HTTPException(409, f"outbound order {order_id} is {status}, cannot stage")

            cur.execute(
                """SELECT id, material_id, qty_ordered, qty_staged, status
                   FROM load_plans WHERE outbound_order_id=%s ORDER BY id""",
                (order_id,),
            )
            plans = {r[0]: {"material_id": r[1], "qty_ordered": _num(r[2]),
                            "qty_staged": _num(r[3]), "status": r[4]}
                     for r in cur.fetchall()}
            if not plans:
                raise HTTPException(409, f"outbound order {order_id} has no load plan")

            if body.lines:
                requested = {ln.load_plan_id: ln.qty_staged for ln in body.lines}
                unknown = [k for k in requested if k not in plans]
                if unknown:
                    raise HTTPException(404, f"load plan line(s) not on this order: {unknown}")
            else:
                # Default: pick every line in full.
                requested = {lp_id: p["qty_ordered"] for lp_id, p in plans.items()}

            staged_lines = []
            for lp_id, qty in requested.items():
                plan = plans[lp_id]
                if qty < 0:
                    raise HTTPException(422, f"{lp_id}: qty_staged cannot be negative")
                line_status = "STAGED" if qty >= plan["qty_ordered"] else "SHORT"
                cur.execute(
                    "UPDATE load_plans SET qty_staged=%s, status=%s WHERE id=%s",
                    (qty, line_status, lp_id),
                )
                plan["qty_staged"] = qty
                plan["status"] = line_status
                staged_lines.append({"load_plan_id": lp_id, "material_id": plan["material_id"],
                                     "qty_ordered": plan["qty_ordered"], "qty_staged": qty,
                                     "status": line_status})

            all_resolved = all(p["status"] in STAGED_TERMINAL for p in plans.values())
            new_status = "STAGED" if all_resolved else "PLANNED"
            cur.execute(
                "UPDATE outbound_orders SET status=%s, updated_at=now() WHERE id=%s",
                (new_status, order_id),
            )

        total_staged = sum(ln["qty_staged"] for ln in staged_lines)
        short_lines = [ln["load_plan_id"] for ln in staged_lines if ln["status"] == "SHORT"]
        payload = {
            "summary": f"{order_id}: {total_staged:g} units staged for {customer}"
                       + (f" ({len(short_lines)} line(s) short)" if short_lines else ""),
            "outbound_order_id": order_id,
            "customer_name": customer,
            "status": new_status,
            "lines": staged_lines,
            "total_staged": total_staged,
            "short_lines": short_lines,
            "ready_to_dispatch": new_status == "STAGED",
            "direction": "OUTBOUND",
        }
        event_id, created_at = _emit(conn, "outbound_order", order_id, "LOAD_STAGED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "outbound_order", order_id,
                         "LOAD_STAGED", payload, created_at)

    return {"id": order_id, "status": new_status, "lines": staged_lines,
            "ready_to_dispatch": new_status == "STAGED"}


# ─────────────────────────────────────────────
# POST /outbound-orders/{id}/dispatch
# ─────────────────────────────────────────────

@router.post("/outbound-orders/{order_id}/dispatch", status_code=201, tags=["outbound"],
             dependencies=[Depends(require(PERM_OUTBOUND_WRITE))])
def dispatch_order(order_id: str, body: DispatchRequest = DispatchRequest()):
    """
    A truck is assigned to collect the staged order.

    Writes an OUTBOUND shipment and its trailer, then emits exactly the pair an
    inbound dispatch emits -- SHIPMENT_CREATED, then TRAILER_DEPARTED. That
    reuse is the whole point: dock-worker already subscribes to
    TRAILER_DEPARTED, so this truck enters the next CP-SAT pass and competes
    for doors against every inbound trailer with no new subscription, no new
    trigger, and no second scheduler.

    Note which way the locations point. On an inbound shipment origin is the
    supplier and destination is us; here origin is our warehouse and destination
    is the customer. `expected_arrival` keeps its meaning from README §4 -- the
    current operational ETA -- which for a collection is when the truck reaches
    OUR gate, and that is precisely the number the scheduler needs.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT status, customer_name, destination_location_id, priority
                   FROM outbound_orders WHERE id=%s""",
                (order_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"outbound order {order_id} not found")
            status, customer, destination, order_priority = row
            if status != "STAGED":
                raise HTTPException(
                    409,
                    f"outbound order {order_id} is {status}, expected STAGED -- a dock "
                    f"door is never committed to a load that has not been picked",
                )

            cur.execute(
                "SELECT id FROM shipments WHERE outbound_order_id=%s AND status <> 'DELIVERED'",
                (order_id,),
            )
            if cur.fetchone() is not None:
                raise HTTPException(409, f"outbound order {order_id} already has a live shipment")

            # Our own site is the origin of an outbound move.
            cur.execute(
                "SELECT id FROM locations WHERE location_type='WAREHOUSE' ORDER BY id LIMIT 1")
            warehouse = cur.fetchone()
            origin = warehouse[0] if warehouse else None

            eta = body.eta or datetime.now(timezone.utc)
            priority = body.priority or order_priority or "normal"

            shipment_id = next_id(cur, "SHP")
            cur.execute(
                """INSERT INTO shipments
                       (id, po_id, tracking_number, carrier, origin_location_id,
                        destination_location_id, expected_arrival, status,
                        direction, outbound_order_id)
                   VALUES (%s, NULL, %s, %s, %s, %s, %s, 'CREATED', 'OUTBOUND', %s)""",
                (shipment_id, body.tracking_number, body.carrier, origin,
                 destination, eta, order_id),
            )

            trailer_id = next_id(cur, "TRL")
            cur.execute(
                """INSERT INTO trailers
                       (id, shipment_id, load_type, priority, eta, status, direction)
                   VALUES (%s,%s,%s,%s,%s,'EN_ROUTE','OUTBOUND')""",
                (trailer_id, shipment_id, body.load_type, priority, eta),
            )
            cur.execute("UPDATE shipments SET status='EN_ROUTE' WHERE id=%s", (shipment_id,))
            cur.execute(
                "UPDATE outbound_orders SET updated_at=now() WHERE id=%s", (order_id,))

        ship_payload = {
            "summary": f"{shipment_id} raised to collect {order_id} for {customer}",
            "outbound_order_id": order_id,
            "customer_name": customer,
            "carrier": body.carrier,
            "tracking_number": body.tracking_number,
            "expected_arrival": _iso(eta),
            "direction": "OUTBOUND",
            "po_id": None,
        }
        trailer_payload = {
            "summary": f"{trailer_id} en route to collect {order_id} "
                       f"({body.load_type}, {priority} priority)",
            "shipment_id": shipment_id,
            "outbound_order_id": order_id,
            "customer_name": customer,
            "carrier": body.carrier,
            "load_type": body.load_type,
            "priority": priority,
            "eta": _iso(eta),
            "direction": "OUTBOUND",
            "po_id": None,
        }
        ev1, at1 = _emit(conn, "shipment", shipment_id, "SHIPMENT_CREATED", ship_payload)
        ev2, at2 = _emit(conn, "trailer", trailer_id, "TRAILER_DEPARTED", trailer_payload)
        conn.commit()
        publish_to_redis(conn, ev1, "shipment", shipment_id,
                         "SHIPMENT_CREATED", ship_payload, at1)
        publish_to_redis(conn, ev2, "trailer", trailer_id,
                         "TRAILER_DEPARTED", trailer_payload, at2)

    return {"outbound_order_id": order_id, "shipment_id": shipment_id,
            "trailer_id": trailer_id, "status": "EN_ROUTE", "eta": _iso(eta)}


# ─────────────────────────────────────────────
# POST /trailers/{id}/load  -- the ONLY writer of goods_issues
# ─────────────────────────────────────────────

@router.post("/trailers/{trailer_id}/load", status_code=201, tags=["outbound"],
             dependencies=[Depends(require(PERM_OUTBOUND_WRITE))])
def load_trailer(trailer_id: str, body: LoadRequest = LoadRequest()):
    """
    Loading completes -- the exact mirror of POST /trailers/{id}/unload, and the
    ONLY writer of goods_issues, just as /unload is the only writer of
    goods_receipts.

    Releasing the door here (not at gate-out) is the same rule inbound follows,
    and it matters more than it looks: GOODS_ISSUED is dock-worker's outbound
    dock-release signal, so the trailers queued behind this door -- inbound ones
    included -- are re-planned the moment the load is on the truck rather than
    whenever the tractor happens to clear the gate.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.status, t.direction, t.shipment_id,
                          s.outbound_order_id, o.customer_name
                   FROM trailers t
                   LEFT JOIN shipments s ON s.id = t.shipment_id
                   LEFT JOIN outbound_orders o ON o.id = s.outbound_order_id
                   WHERE t.id=%s""",
                (trailer_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            status, direction, shipment_id, order_id, customer = row

            if direction != "OUTBOUND":
                raise HTTPException(
                    409,
                    f"trailer {trailer_id} is {direction} -- use POST /trailers/{trailer_id}"
                    f"/unload for an inbound trailer",
                )
            if status not in ("ARRIVED", "DOCKED"):
                raise HTTPException(409, f"trailer {trailer_id} is {status}, cannot load")
            if not order_id:
                raise HTTPException(409, f"trailer {trailer_id} has no outbound order")

            cur.execute(
                """SELECT id, material_id, qty_ordered, qty_staged
                   FROM load_plans WHERE outbound_order_id=%s ORDER BY id""",
                (order_id,),
            )
            plans = {r[0]: {"material_id": r[1], "qty_ordered": _num(r[2]),
                            "qty_staged": _num(r[3])} for r in cur.fetchall()}
            if not plans:
                raise HTTPException(409, f"outbound order {order_id} has no load plan")

            if body.lines:
                requested = {ln.load_plan_id: ln.qty_loaded for ln in body.lines}
                unknown = [k for k in requested if k not in plans]
                if unknown:
                    raise HTTPException(404, f"load plan line(s) not on this order: {unknown}")
            else:
                # Default: everything that made it to the staging lane goes on
                # the truck. Loading more than was staged is not expressible,
                # which is correct -- you cannot load goods nobody picked.
                requested = {lp_id: p["qty_staged"] for lp_id, p in plans.items()}

            lines_out = []
            for lp_id, qty in requested.items():
                plan = plans[lp_id]
                if qty < 0:
                    raise HTTPException(422, f"{lp_id}: qty_loaded cannot be negative")
                if qty > plan["qty_staged"]:
                    raise HTTPException(
                        422,
                        f"{lp_id}: cannot load {qty:g}, only {plan['qty_staged']:g} staged",
                    )
                cur.execute(
                    "UPDATE load_plans SET qty_loaded=%s, status='LOADED' WHERE id=%s",
                    (qty, lp_id),
                )
                lines_out.append({"load_plan_id": lp_id, "material_id": plan["material_id"],
                                  "qty_ordered": plan["qty_ordered"], "qty_loaded": qty})

            total_qty = sum(ln["qty_loaded"] for ln in lines_out)

            cur.execute("UPDATE trailers SET status='LOADED', updated_at=now() WHERE id=%s",
                        (trailer_id,))
            cur.execute("UPDATE shipments SET status='LOADED' WHERE id=%s", (shipment_id,))
            cur.execute(
                "UPDATE outbound_orders SET status='SHIPPED', updated_at=now() WHERE id=%s",
                (order_id,),
            )

            # Release the door, exactly as /unload does. Without this the
            # scheduler's overlap check sees the window as live forever.
            cur.execute(
                """UPDATE dock_assignments SET status='COMPLETED'
                   WHERE trailer_id=%s AND status IN ('ASSIGNED','CONFIRMED','DELAYED')
                   RETURNING dock_id""",
                (trailer_id,),
            )
            released = cur.fetchone()
            released_dock = released[0] if released else None

            gi_id = next_id(cur, "GI")
            cur.execute(
                """INSERT INTO goods_issues
                       (id, trailer_id, shipment_id, outbound_order_id, qty_issued, lines)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (gi_id, trailer_id, shipment_id, order_id, total_qty, json.dumps(lines_out)),
            )

        payload = {
            "summary": f"{gi_id}: {total_qty:g} units loaded for {customer or order_id}",
            "outbound_order_id": order_id,
            "customer_name": customer,
            "qty_issued": total_qty,
            "trailer_id": trailer_id,
            "shipment_id": shipment_id,
            "released_dock_id": released_dock,
            "lines": lines_out,
            "direction": "OUTBOUND",
        }
        event_id, created_at = _emit(conn, "goods_issue", gi_id, "GOODS_ISSUED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "goods_issue", gi_id,
                         "GOODS_ISSUED", payload, created_at)

    return {"goods_issue_id": gi_id, "outbound_order_id": order_id,
            "qty_issued": total_qty, "released_dock_id": released_dock,
            "lines": lines_out}


# ─────────────────────────────────────────────
# POST /trailers/{id}/deliver
# ─────────────────────────────────────────────

@router.post("/trailers/{trailer_id}/deliver", tags=["outbound"],
             dependencies=[Depends(require(PERM_OUTBOUND_WRITE))])
def deliver(trailer_id: str):
    """
    The outbound truck reaches the customer -- the end of the story.

    An inbound trailer stops at DEPARTED because where it goes after our gate is
    not our yard's business. An outbound one does not: we promised a customer a
    delivery, so the movement is not finished until it lands. That asymmetry is
    why DELIVERED exists as a state and why it is outbound-only.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.status, t.direction, t.shipment_id,
                          s.outbound_order_id, o.customer_name
                   FROM trailers t
                   LEFT JOIN shipments s ON s.id = t.shipment_id
                   LEFT JOIN outbound_orders o ON o.id = s.outbound_order_id
                   WHERE t.id=%s""",
                (trailer_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"trailer {trailer_id} not found")
            status, direction, shipment_id, order_id, customer = row

            if direction != "OUTBOUND":
                raise HTTPException(409, f"trailer {trailer_id} is {direction}, not deliverable")
            if status != "DEPARTED":
                raise HTTPException(409, f"trailer {trailer_id} is {status}, expected DEPARTED")

            cur.execute("UPDATE trailers SET status='DELIVERED', updated_at=now() WHERE id=%s",
                        (trailer_id,))
            cur.execute("UPDATE shipments SET status='DELIVERED' WHERE id=%s", (shipment_id,))
            cur.execute(
                "UPDATE outbound_orders SET status='DELIVERED', updated_at=now() WHERE id=%s",
                (order_id,),
            )

        payload = {
            "summary": f"{order_id} delivered to {customer or 'customer'}",
            "outbound_order_id": order_id,
            "customer_name": customer,
            "trailer_id": trailer_id,
            "shipment_id": shipment_id,
            "direction": "OUTBOUND",
        }
        event_id, created_at = _emit(conn, "outbound_order", order_id,
                                     "OUTBOUND_DELIVERED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "outbound_order", order_id,
                         "OUTBOUND_DELIVERED", payload, created_at)

    return {"trailer_id": trailer_id, "status": "DELIVERED",
            "outbound_order_id": order_id}


# ─────────────────────────────────────────────
# GET /outbound-orders
# ─────────────────────────────────────────────

@router.get("/outbound-orders", tags=["outbound"])
def list_outbound_orders(status: Optional[str] = None, limit: int = 100):
    """
    The outbound queue: every order with its staging progress, its truck, and
    where that truck currently is. One query with lateral joins rather than a
    row-per-order follow-up, because this is a list screen that refreshes.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.id, o.customer_name, o.status, o.priority,
                       o.requested_ship_date, o.created_at,
                       loc.name,
                       lp.line_count, lp.qty_ordered, lp.qty_staged, lp.qty_loaded,
                       t.id, t.status, t.eta, t.direction,
                       s.id, s.carrier, s.tracking_number,
                       da.dock_id, da.status, da.planned_start
                FROM outbound_orders o
                LEFT JOIN locations loc ON loc.id = o.destination_location_id
                LEFT JOIN LATERAL (
                    SELECT count(*) AS line_count,
                           COALESCE(sum(qty_ordered),0) AS qty_ordered,
                           COALESCE(sum(qty_staged),0)  AS qty_staged,
                           COALESCE(sum(qty_loaded),0)  AS qty_loaded
                    FROM load_plans WHERE outbound_order_id = o.id
                ) lp ON TRUE
                LEFT JOIN LATERAL (
                    SELECT id, carrier, tracking_number FROM shipments
                    WHERE outbound_order_id = o.id ORDER BY created_at DESC LIMIT 1
                ) s ON TRUE
                LEFT JOIN LATERAL (
                    SELECT id, status, eta, direction FROM trailers
                    WHERE shipment_id = s.id ORDER BY created_at DESC LIMIT 1
                ) t ON TRUE
                LEFT JOIN LATERAL (
                    SELECT dock_id, status, planned_start FROM dock_assignments
                    WHERE trailer_id = t.id AND status IN ('ASSIGNED','CONFIRMED')
                    LIMIT 1
                ) da ON TRUE
                WHERE (%s IS NULL OR o.status = %s)
                ORDER BY o.created_at DESC
                LIMIT %s
            """, (status, status, limit))

            orders = []
            for r in cur.fetchall():
                qty_ordered, qty_staged = _num(r[8]) or 0, _num(r[9]) or 0
                orders.append({
                    "id": r[0], "customer_name": r[1], "status": r[2], "priority": r[3],
                    "requested_ship_date": _iso(r[4]), "created_at": _iso(r[5]),
                    "destination": r[6],
                    "line_count": r[7],
                    "qty_ordered": qty_ordered,
                    "qty_staged": qty_staged,
                    "qty_loaded": _num(r[10]) or 0,
                    # Derived, never stored -- a stored percentage needs a writer.
                    "staged_pct": round(qty_staged / qty_ordered * 100) if qty_ordered else 0,
                    "trailer": ({"id": r[11], "status": r[12], "eta": _iso(r[13])}
                                if r[11] else None),
                    "shipment_id": r[15], "carrier": r[16], "tracking_number": r[17],
                    "dock_assignment": ({"dock_id": r[18], "status": r[19],
                                         "planned_start": _iso(r[20])} if r[18] else None),
                })
        conn.rollback()

    return {"orders": orders, "count": len(orders)}


# ─────────────────────────────────────────────
# GET /outbound-orders/{id}
# ─────────────────────────────────────────────

@router.get("/outbound-orders/{order_id}", tags=["outbound"])
def get_outbound_order(order_id: str):
    """
    One order end to end: lines, truck, the FULL dock-assignment history, the
    goods issue, and the event timeline.

    Dock history is every row for the trailer, not just the live one -- the same
    choice GET /trailers/{id} makes, and for the same reason: "why did this
    truck's door change" is only answerable if the superseded rows are shown.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.id, o.customer_name, o.status, o.priority,
                       o.requested_ship_date, o.created_at, o.updated_at,
                       o.destination_location_id, loc.name, loc.latitude, loc.longitude
                FROM outbound_orders o
                LEFT JOIN locations loc ON loc.id = o.destination_location_id
                WHERE o.id=%s
            """, (order_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"outbound order {order_id} not found")

            order = {
                "id": row[0], "customer_name": row[1], "status": row[2],
                "priority": row[3], "requested_ship_date": _iso(row[4]),
                "created_at": _iso(row[5]), "updated_at": _iso(row[6]),
                "destination": {"id": row[7], "name": row[8],
                                "latitude": _num(row[9]), "longitude": _num(row[10])},
            }

            cur.execute("""
                SELECT lp.id, lp.material_id, m.name, lp.qty_ordered,
                       lp.qty_staged, lp.qty_loaded, lp.status
                FROM load_plans lp
                LEFT JOIN materials m ON m.id = lp.material_id
                WHERE lp.outbound_order_id=%s ORDER BY lp.id
            """, (order_id,))
            order["lines"] = [
                {"load_plan_id": r[0], "material_id": r[1], "material_name": r[2],
                 "qty_ordered": _num(r[3]), "qty_staged": _num(r[4]),
                 "qty_loaded": _num(r[5]), "status": r[6]}
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT s.id, s.carrier, s.tracking_number, s.status, s.expected_arrival,
                       t.id, t.status, t.eta, t.load_type, t.priority, t.updated_at
                FROM shipments s
                LEFT JOIN LATERAL (
                    SELECT id, status, eta, load_type, priority, updated_at
                    FROM trailers WHERE shipment_id = s.id
                    ORDER BY created_at DESC LIMIT 1
                ) t ON TRUE
                WHERE s.outbound_order_id=%s ORDER BY s.created_at DESC LIMIT 1
            """, (order_id,))
            ship = cur.fetchone()
            trailer_id = None
            if ship:
                trailer_id = ship[5]
                order["shipment"] = {"id": ship[0], "carrier": ship[1],
                                     "tracking_number": ship[2], "status": ship[3],
                                     "expected_arrival": _iso(ship[4])}
                order["trailer"] = ({"id": ship[5], "status": ship[6], "eta": _iso(ship[7]),
                                     "load_type": ship[8], "priority": ship[9],
                                     "updated_at": _iso(ship[10])} if ship[5] else None)
            else:
                order["shipment"] = None
                order["trailer"] = None

            order["dock_assignments"] = []
            if trailer_id:
                cur.execute("""
                    SELECT id, dock_id, status, assigned_at, reason, docked_at,
                           planned_start, planned_end, score_breakdown
                    FROM dock_assignments WHERE trailer_id=%s ORDER BY assigned_at
                """, (trailer_id,))
                order["dock_assignments"] = [
                    {"id": r[0], "dock_id": r[1], "status": r[2], "assigned_at": _iso(r[3]),
                     "reason": r[4], "docked_at": _iso(r[5]),
                     "planned_start": _iso(r[6]), "planned_end": _iso(r[7]),
                     "score_breakdown": r[8]}
                    for r in cur.fetchall()
                ]

            cur.execute("""
                SELECT id, trailer_id, qty_issued, lines, issued_at, verified_by
                FROM goods_issues WHERE outbound_order_id=%s ORDER BY issued_at DESC LIMIT 1
            """, (order_id,))
            gi = cur.fetchone()
            order["goods_issue"] = ({"id": gi[0], "trailer_id": gi[1],
                                     "qty_issued": _num(gi[2]), "lines": gi[3],
                                     "issued_at": _iso(gi[4]), "verified_by": gi[5]}
                                    if gi else None)

            # Timeline across every entity this order touches. Same shape as the
            # gateway's /traceability/{po_id} does for the inbound side.
            entity_filters = [("outbound_order", order_id)]
            if order.get("shipment"):
                entity_filters.append(("shipment", order["shipment"]["id"]))
            if trailer_id:
                entity_filters.append(("trailer", trailer_id))
            if order["goods_issue"]:
                entity_filters.append(("goods_issue", order["goods_issue"]["id"]))
            for da in order["dock_assignments"]:
                entity_filters.append(("dock_assignment", da["id"]))

            # `IN %s` with a tuple of tuples, not `= ANY(%s)` with a list: psycopg2
            # sends a Python list of tuples as an array of UNKNOWN-typed records,
            # and Postgres refuses to compare (text, text) against that. The
            # tuple form is adapted to a plain row-value list, which it will.
            cur.execute("""
                SELECT id, entity_type, entity_id, event_type, payload, created_at
                FROM event_log
                WHERE (entity_type, entity_id) IN %s
                ORDER BY created_at, id
            """, (tuple(entity_filters),))
            order["timeline"] = [
                {"event_id": r[0], "entity_type": r[1], "entity_id": r[2],
                 "event_type": r[3], "payload": r[4], "timestamp": _iso(r[5])}
                for r in cur.fetchall()
            ]

        conn.rollback()

    return order
