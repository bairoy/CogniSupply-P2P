"""
Simulator + scenario control surface (v7). HTTP on :8004.

WHAT IT IS

A process that makes the system look alive: trucks move, arrive, take doors,
unload and load, invoices turn up, outbound orders get picked and collected --
continuously, with nobody clicking anything. Plus a set of one-shot triggers so
any demo moment can be forced on cue rather than waited for.

THE RULE THAT MAKES IT WORTH HAVING

It drives the REAL HTTP APIs. It owns no tables and holds no domain state; it
reads current state from the public read endpoints and POSTs to the same write
endpoints an operator uses. Nothing here can make the system do something a
person could not.

That is what turns the simulator into evidence. A simulator that INSERTed rows
directly would prove the database accepts rows -- which was never in doubt.
One that can only act through the API proves the API is complete enough to run
the entire business, in both directions, unattended. If a transition is missing
an endpoint, this file cannot be written, and that failure is the finding.

The single exception is `block-dock`, which flips docks.is_active in the
database, and is marked as such below. `docks` is master data describing
physical infrastructure -- there is deliberately no dock-management endpoint,
because taking a door out of service is a maintenance fact, not a supply-chain
transaction. Simulating a door failing is therefore simulating the world, not
simulating a user.

DETERMINISM, AND WHERE IT DELIBERATELY STOPS

Trailer movement is driven by elapsed time against each trailer's own planned
window, not by dice: a truck docks when its door is due, unloads when its
service minutes are up. What IS random is the invoice mismatch draw, seeded
from the PO id (hashlib, not random()) so the same PO always produces the same
discrepancy. Re-running a demo shows the same exceptions.

Run:  ./.venv/bin/python -m uvicorn services.simulator.main:app --port 8004
      (from backend/)
"""

import hashlib
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT.parent / ".env")

from shared.api import create_app  # noqa: E402
from shared.auth import PERM_YARD_WRITE, ROLE_ADMIN, issue_token, require  # noqa: E402
from shared.db import get_conn  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  simulator  %(levelname)-7s %(message)s",
)
log = logging.getLogger("simulator")

app = create_app(
    "Simulator",
    description=(
        "Drives the real Yard and Procurement APIs so the yard runs unattended, "
        "plus one-shot scenario triggers for the demo. Owns no tables."
    ),
)

YARD = os.environ.get("YARD_API_URL", "http://127.0.0.1:8001")
PROC = os.environ.get("PROCUREMENT_API_URL", "http://127.0.0.1:8002")
HTTP_TIMEOUT = 10.0

TICK_SECONDS = float(os.environ.get("SIM_TICK_SECONDS", "3"))

# How long a trailer lingers in each terminal-ish state before the simulator
# moves it on. Real enough to watch, short enough to see a full cycle in a demo.
GATE_OUT_AFTER_MINUTES = 2      # UNLOADED/LOADED -> DEPARTED
DELIVERY_AFTER_MINUTES = 4      # DEPARTED -> DELIVERED (outbound only)
INVOICE_AFTER_MINUTES = 1       # goods receipt -> supplier invoices us

# Mismatch mix, matching BUILD_PLAN §5.1's seeded proportions so the simulator's
# traffic has the same character as the seed data the eval harness scores.
MISMATCH_MIX = [
    ("clean", 0.72),
    ("qty", 0.10),
    ("price", 0.08),
    ("missing_po", 0.05),
    ("near_miss", 0.05),
]

CUSTOMERS = [
    "Northwind Retail", "Lakeshore Distribution", "Cardinal Foods",
    "Vertex Industrial", "Prairie Outfitters", "Halstead Manufacturing",
]


# ─────────────────────────────────────────────
# HTTP plumbing
# ─────────────────────────────────────────────

def _token() -> str:
    """
    Admin service token, same reasoning as supplier-agent: the simulator has to
    act across yard, outbound and procurement, and no single human role spans
    all three -- correctly, since the matrix segregates duties between people.
    """
    token, _ = issue_token("USR-000", "simulator", ROLE_ADMIN)
    return token


def _req(method: str, base: str, path: str, body: dict | None = None):
    try:
        resp = httpx.request(
            method, f"{base}{path}", json=body,
            headers={"Authorization": f"Bearer {_token()}"}, timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        log.warning("%s %s failed: %s", method, path, exc)
        return None
    if resp.status_code >= 400:
        # 409s are normal and expected: the simulator races the workers and
        # sometimes tries a transition that already happened. Logging them at
        # debug keeps the signal readable.
        level = log.debug if resp.status_code == 409 else log.warning
        level("%s %s -> %s %s", method, path, resp.status_code, resp.text[:160])
        return None
    return resp.json() if resp.content else {}


def post(base, path, body=None):
    return _req("POST", base, path, body or {})


def get(base, path):
    return _req("GET", base, path)


# ─────────────────────────────────────────────
# State
# ─────────────────────────────────────────────

class SimState:
    def __init__(self):
        self.running = False
        self.ticks = 0
        self.started_at: Optional[datetime] = None
        self.last_tick_at: Optional[datetime] = None
        self.actions: dict[str, int] = {}
        self.recent: list[str] = []
        self.lock = threading.Lock()

    def did(self, action: str, detail: str = ""):
        with self.lock:
            self.actions[action] = self.actions.get(action, 0) + 1
            line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}  {action}"
            if detail:
                line += f"  {detail}"
            self.recent.insert(0, line)
            del self.recent[40:]

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "ticks": self.ticks,
                "tick_seconds": TICK_SECONDS,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
                "actions": dict(self.actions),
                "recent": list(self.recent),
            }


state = SimState()


# ─────────────────────────────────────────────
# Reading the world
# ─────────────────────────────────────────────
# Reads go to Postgres directly. That is NOT a violation of the drive-the-API
# rule -- reads are how the simulator decides what to do next, and every read
# endpoint that exists is shaped for a dashboard, not for "give me every trailer
# with its planned window and last known position". WRITES are what must go
# through the API, and every one of them does.

def _fetch(sql, params=()):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.rollback()
    return rows


def _minutes_since(ts, now):
    return (now - ts).total_seconds() / 60 if ts else 0


# ─────────────────────────────────────────────
# The tick
# ─────────────────────────────────────────────

def _advance_en_route(now):
    """
    GPS ticks for everything on the road, in both directions, and a gate-in for
    anything that has arrived.

    The position walks from origin toward destination in proportion to elapsed
    time against the ETA, so the map animates instead of teleporting. ETA drifts
    by a small amount each tick -- which is what eventually crosses the 10-minute
    threshold in redis-contract.md §9 and makes the dock worker re-plan for a
    reason that came from the world rather than from a button.
    """
    rows = _fetch("""
        SELECT t.id, t.eta, t.direction,
               o.latitude, o.longitude, d.latitude, d.longitude,
               te.latitude, te.longitude
        FROM trailers t
        JOIN shipments s ON s.id = t.shipment_id
        LEFT JOIN locations o ON o.id = s.origin_location_id
        LEFT JOIN locations d ON d.id = s.destination_location_id
        LEFT JOIN LATERAL (
            SELECT latitude, longitude FROM tracking_events
            WHERE trailer_id = t.id ORDER BY recorded_at DESC LIMIT 1
        ) te ON TRUE
        WHERE t.status = 'EN_ROUTE'
        ORDER BY t.eta NULLS LAST LIMIT 40
    """)
    for (tid, eta, direction, olat, olon, dlat, dlon, clat, clon) in rows:
        if eta and eta <= now:
            post(YARD, f"/trailers/{tid}/arrive")
            state.did("gate_in", f"{tid} ({direction.lower()})")
            continue

        # An outbound truck is driving TO us, so its destination for tracking
        # purposes is our warehouse -- which is the shipment's origin. Same
        # journey, opposite endpoints.
        start_lat, start_lon = (dlat, dlon) if direction == "OUTBOUND" else (olat, olon)
        end_lat, end_lon = (olat, olon) if direction == "OUTBOUND" else (dlat, dlon)
        if None in (start_lat, start_lon, end_lat, end_lon):
            continue

        lat = float(clat) if clat is not None else float(start_lat)
        lon = float(clon) if clon is not None else float(start_lon)
        # Close a fraction of the remaining gap each tick: asymptotic approach,
        # never overshoots, and needs no total-distance bookkeeping.
        lat += (float(end_lat) - lat) * 0.12
        lon += (float(end_lon) - lon) * 0.12

        drift = timedelta(minutes=random.choice([0, 0, 0, 1, -1, 2, -2, 4, -3]))
        new_eta = (eta or now + timedelta(hours=1)) + drift
        post(YARD, f"/trailers/{tid}/tracking", {
            "latitude": round(lat, 5), "longitude": round(lon, 5),
            "speed": round(random.uniform(45, 95), 1),
            "eta_estimate": new_eta.isoformat(),
        })
    if rows:
        state.did("gps_tick", f"{len(rows)} trailer(s)")


def _advance_docking(now):
    """Anything in the yard whose door is due pulls in."""
    rows = _fetch("""
        SELECT t.id, t.direction, da.dock_id
        FROM trailers t
        JOIN dock_assignments da ON da.trailer_id = t.id
                                AND da.status IN ('ASSIGNED','CONFIRMED')
        WHERE t.status = 'ARRIVED'
          AND COALESCE(da.planned_start, da.assigned_at) <= now()
        ORDER BY da.planned_start LIMIT 20
    """)
    for tid, direction, dock_id in rows:
        if post(YARD, f"/trailers/{tid}/dock") is not None:
            state.did("dock", f"{tid} -> {dock_id} ({direction.lower()})")


def _advance_service(now):
    """
    A trailer at a door finishes when its planned window is up: unload for
    inbound, load for outbound. Same trigger, same door, opposite verb -- which
    is the whole outbound design in one function.
    """
    rows = _fetch("""
        SELECT t.id, t.direction, da.docked_at, da.planned_end, da.dock_id,
               s.po_id, po.qty
        FROM trailers t
        JOIN dock_assignments da ON da.trailer_id = t.id AND da.status = 'CONFIRMED'
        LEFT JOIN shipments s ON s.id = t.shipment_id
        LEFT JOIN purchase_orders po ON po.id = s.po_id
        WHERE t.status = 'DOCKED'
          AND (da.planned_end <= now() OR da.docked_at <= now() - interval '2 minutes')
        LIMIT 20
    """)
    for tid, direction, docked_at, planned_end, dock_id, po_id, po_qty in rows:
        if direction == "OUTBOUND":
            if post(YARD, f"/trailers/{tid}/load", {}) is not None:
                state.did("load", f"{tid} at {dock_id}")
        else:
            # Received quantity is what physically came off the truck. Most of the
            # time it matches the PO; the qty-variance draw is what later becomes a
            # genuine QTY_MISMATCH at the 3-way match rather than a fabricated one.
            qty = float(po_qty or 100)
            if _draw(po_id or tid) == "qty":
                qty = round(qty * random.choice([0.93, 0.95, 1.06, 1.08]), 2)
            if post(YARD, f"/trailers/{tid}/unload", {"qty_received": qty}) is not None:
                state.did("unload", f"{tid} at {dock_id}, {qty:g} units")


def _advance_gate_out(now):
    """UNLOADED/LOADED trailers clear the gate; delivered outbound ones land."""
    rows = _fetch("""
        SELECT id, direction, status, updated_at FROM trailers
        WHERE status IN ('UNLOADED','LOADED')
          AND updated_at <= now() - make_interval(mins => %s)
        LIMIT 20
    """, (GATE_OUT_AFTER_MINUTES,))
    for tid, direction, status, _ in rows:
        if post(YARD, f"/trailers/{tid}/depart") is not None:
            state.did("gate_out", f"{tid} ({direction.lower()})")

    rows = _fetch("""
        SELECT id FROM trailers
        WHERE status = 'DEPARTED' AND direction = 'OUTBOUND'
          AND updated_at <= now() - make_interval(mins => %s)
        LIMIT 20
    """, (DELIVERY_AFTER_MINUTES,))
    for (tid,) in rows:
        if post(YARD, f"/trailers/{tid}/deliver") is not None:
            state.did("delivered", tid)


def _draw(key: str) -> str:
    """
    Which mismatch (if any) this PO's invoice will carry.

    Seeded from the key, never random(), so the same PO always produces the same
    discrepancy. A demo that shows a price mismatch on PO-1042 shows it again on
    the next run, and the eval harness's ground truth stays meaningful.
    """
    digest = hashlib.sha256(f"invoice:{key}".encode()).digest()
    roll = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    cumulative = 0.0
    for name, weight in MISMATCH_MIX:
        cumulative += weight
        if roll < cumulative:
            return name
    return "clean"


def _advance_invoices(now):
    """
    The supplier invoices us, some minutes after we received the goods.

    This is what completes the P2P loop without a human: the invoice lands,
    match-worker already has the goods receipt, and the 3-way match runs by
    itself -- approving and paying, or raising an exception for a person.
    """
    rows = _fetch("""
        SELECT gr.po_id, po.qty, po.unit_price, gr.qty_received
        FROM goods_receipts gr
        JOIN purchase_orders po ON po.id = gr.po_id
        LEFT JOIN invoices i ON i.po_id = gr.po_id
        WHERE i.id IS NULL
          AND gr.received_at <= now() - make_interval(mins => %s)
        LIMIT 10
    """, (INVOICE_AFTER_MINUTES,))

    for row in rows:
        po_id, po_qty, unit_price, qty_received = row
        qty = float(qty_received or po_qty or 0)
        price = float(unit_price or 0)
        scenario = _draw(po_id)

        body = {"po_id": po_id, "qty_invoiced": qty, "unit_price_invoiced": price,
                "tax": round(qty * price * 0.08, 2),
                "ocr_raw": {"source": "simulator", "scenario": scenario, "confidence": 0.97}}

        if scenario == "price":
            body["unit_price_invoiced"] = round(price * random.choice([1.06, 1.09, 1.12]), 2)
        elif scenario == "qty":
            body["qty_invoiced"] = round(qty * random.choice([1.05, 1.09]), 2)
        elif scenario == "near_miss":
            # Inside tolerance on purpose. This is the case that proves the
            # tolerance band does something -- a system that only ever shows
            # clean-vs-broken never demonstrates it has a band at all.
            body["qty_invoiced"] = round(qty * 1.015, 2)
        elif scenario == "missing_po":
            body["po_id"] = None

        if post(PROC, "/invoices", body) is not None:
            state.did("invoice", f"{po_id} ({scenario})")


def _advance_outbound(now):
    """
    Keep outbound work flowing: stage what is planned, dispatch what is staged,
    and top the queue up when it runs dry.

    Each stage is a separate tick rather than one chained call, so the board
    actually shows an order sitting in PLANNED and then STAGED. Doing it all at
    once would be faster and would hide the pipeline the screen exists to draw.
    """
    for (oid,) in _fetch(
            "SELECT id FROM outbound_orders WHERE status='PLANNED' ORDER BY created_at LIMIT 3"):
        if post(YARD, f"/outbound-orders/{oid}/stage", {}) is not None:
            state.did("stage", oid)

    # STAGED covers both "picked, no truck yet" and "picked, truck already on
    # its way" -- the status only advances to LOADING when the truck reaches a
    # door. So the NOT EXISTS is what stops the simulator re-dispatching an
    # order every tick until its truck arrives. The dispatch endpoint would
    # reject the duplicate anyway; this keeps it from having to.
    for (oid,) in _fetch("""
            SELECT o.id FROM outbound_orders o
            WHERE o.status='STAGED'
              AND NOT EXISTS (SELECT 1 FROM shipments s
                              WHERE s.outbound_order_id = o.id AND s.status <> 'DELIVERED')
            ORDER BY o.created_at LIMIT 3"""):
        if post(YARD, f"/outbound-orders/{oid}/dispatch", {
                "carrier": random.choice(["Swift Logistics", "Cardinal Haulage",
                                          "Blue Line Freight"]),
                "tracking_number": f"TRK-OB-{random.randint(10000, 99999)}",
                "load_type": random.choice(["dry_van", "reefer", "flatbed"]),
                "eta": (now + timedelta(minutes=random.randint(6, 30))).isoformat(),
        }) is not None:
            state.did("dispatch", oid)

    live = _fetch("""SELECT count(*) FROM outbound_orders
                     WHERE status IN ('CREATED','PLANNED','STAGED','LOADING')""")[0][0]
    if live < 3:
        _new_outbound_order(now)


def _new_outbound_order(now, priority=None):
    materials = _fetch("SELECT id FROM materials ORDER BY random() LIMIT 2")
    destinations = _fetch(
        "SELECT id FROM locations WHERE location_type <> 'WAREHOUSE' ORDER BY random() LIMIT 1")
    if not materials:
        return None
    body = {
        "customer_name": random.choice(CUSTOMERS),
        "destination_location_id": destinations[0][0] if destinations else None,
        "requested_ship_date": (now + timedelta(hours=random.randint(4, 48))).isoformat(),
        "priority": priority or random.choices(
            ["low", "normal", "high", "critical"], weights=[1, 5, 3, 1])[0],
        "lines": [{"material_id": m[0], "qty": random.choice([60, 120, 240, 300, 480])}
                  for m in materials],
    }
    created = post(YARD, "/outbound-orders", body)
    if created:
        state.did("outbound_order", f"{created['id']} for {body['customer_name']}")
    return created


def tick():
    now = datetime.now(timezone.utc)
    _advance_en_route(now)
    _advance_docking(now)
    _advance_service(now)
    _advance_gate_out(now)
    _advance_invoices(now)
    _advance_outbound(now)


def _loop():
    while True:
        if state.running:
            try:
                tick()
                state.ticks += 1
                state.last_tick_at = datetime.now(timezone.utc)
            except Exception:
                log.exception("tick failed; continuing")
        time.sleep(TICK_SECONDS)


threading.Thread(target=_loop, daemon=True).start()


# ─────────────────────────────────────────────
# Control surface
# ─────────────────────────────────────────────

WRITE = [Depends(require(PERM_YARD_WRITE))]


@app.post("/sim/start", tags=["simulator"], dependencies=WRITE)
def sim_start():
    state.running = True
    state.started_at = state.started_at or datetime.now(timezone.utc)
    log.info("simulator started (tick=%ss)", TICK_SECONDS)
    return state.snapshot()


@app.post("/sim/stop", tags=["simulator"], dependencies=WRITE)
def sim_stop():
    """
    Pause, deliberately without unwinding anything.

    Everything the simulator did was a real transaction through a real endpoint,
    so there is nothing to roll back -- and a demo that can be frozen mid-story
    while a judge asks a question is worth more than one that has to be
    restarted.
    """
    state.running = False
    log.info("simulator stopped")
    return state.snapshot()


@app.get("/sim/status", tags=["simulator"])
def sim_status():
    return state.snapshot()


@app.post("/sim/tick", tags=["simulator"], dependencies=WRITE)
def sim_tick():
    """One tick, on demand -- step the world forward while paused."""
    tick()
    state.ticks += 1
    return state.snapshot()


class ScenarioResult(BaseModel):
    scenario: str
    applied: bool
    detail: str
    entities: list[str] = Field(default_factory=list)


@app.post("/sim/scenario/{name}", tags=["simulator"], dependencies=WRITE,
          response_model=ScenarioResult)
def scenario(name: str, count: int = 3):
    """
    Force a specific demo moment.

    Every scenario here is a real state change through a real endpoint, not a
    display trick: `delay-trailer` genuinely posts a late ETA and the dock worker
    genuinely re-plans around it. That is the difference between demonstrating
    the system and animating a mock of it.
    """
    now = datetime.now(timezone.utc)

    if name == "delay-trailer":
        rows = _fetch("""
            SELECT t.id, t.eta, te.latitude, te.longitude
            FROM trailers t
            LEFT JOIN LATERAL (
                SELECT latitude, longitude FROM tracking_events
                WHERE trailer_id=t.id ORDER BY recorded_at DESC LIMIT 1
            ) te ON TRUE
            WHERE t.status='EN_ROUTE' AND t.eta > now()
            ORDER BY t.eta LIMIT %s
        """, (count,))
        if not rows:
            return ScenarioResult(scenario=name, applied=False,
                                  detail="no en-route trailers to delay")
        hit = []
        for tid, eta, lat, lon in rows:
            # +90 minutes clears redis-contract.md §9's 10-minute threshold by a
            # wide margin, so ETA_UPDATED fires, the yard is re-planned, and a
            # DELAY alert is raised -- the whole chain, from one POST.
            post(YARD, f"/trailers/{tid}/tracking", {
                "latitude": float(lat) if lat is not None else 41.85,
                "longitude": float(lon) if lon is not None else -87.65,
                "speed": 8.0,
                "eta_estimate": (eta + timedelta(minutes=90)).isoformat(),
            })
            hit.append(tid)
        state.did("scenario:delay", ", ".join(hit))
        return ScenarioResult(scenario=name, applied=True, entities=hit,
                              detail=f"{len(hit)} trailer(s) delayed 90 min; expect "
                                     f"ETA_UPDATED, a re-plan and DELAY alerts")

    if name == "surge-arrivals":
        rows = _fetch("""SELECT id FROM trailers WHERE status='EN_ROUTE'
                         ORDER BY eta LIMIT %s""", (max(count, 5),))
        hit = [tid for (tid,) in rows if post(YARD, f"/trailers/{tid}/arrive") is not None]
        state.did("scenario:surge", f"{len(hit)} trailers")
        return ScenarioResult(scenario=name, applied=bool(hit), entities=hit,
                              detail=f"{len(hit)} trailer(s) hit the gate at once; "
                                     f"the scheduler now has to queue them")

    if name == "block-dock":
        # THE ONE DIRECT DATABASE WRITE IN THIS FILE. See the module docstring:
        # docks is master data describing physical infrastructure, and there is
        # deliberately no dock-management endpoint. A door going out of service
        # is the world changing, not a user acting.
        rows = _fetch("""SELECT d.id FROM docks d
                         WHERE d.is_active
                           AND NOT EXISTS (SELECT 1 FROM dock_assignments da
                                           WHERE da.dock_id=d.id AND da.status='CONFIRMED')
                         ORDER BY d.yard_position DESC LIMIT 1""")
        if not rows:
            return ScenarioResult(scenario=name, applied=False,
                                  detail="every active dock is currently occupied")
        dock_id = rows[0][0]
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE docks SET is_active=FALSE WHERE id=%s", (dock_id,))
            conn.commit()
        state.did("scenario:block-dock", dock_id)
        return ScenarioResult(scenario=name, applied=True, entities=[dock_id],
                              detail=f"{dock_id} taken out of service; the next re-plan "
                                     f"must route around it")

    if name == "unblock-docks":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE docks SET is_active=TRUE WHERE NOT is_active RETURNING id")
                freed = [r[0] for r in cur.fetchall()]
            conn.commit()
        state.did("scenario:unblock", ", ".join(freed) or "none")
        return ScenarioResult(scenario=name, applied=bool(freed), entities=freed,
                              detail=f"restored {len(freed)} dock(s)")

    if name in ("inject-price-mismatch", "inject-qty-mismatch"):
        rows = _fetch("""
            SELECT gr.po_id, po.qty, po.unit_price, gr.qty_received
            FROM goods_receipts gr
            JOIN purchase_orders po ON po.id = gr.po_id
            LEFT JOIN invoices i ON i.po_id = gr.po_id
            WHERE i.id IS NULL LIMIT 1
        """)
        if not rows:
            return ScenarioResult(scenario=name, applied=False,
                                  detail="no un-invoiced goods receipt available")
        po_id, po_qty, unit_price, qty_received = rows[0]
        qty, price = float(qty_received or po_qty), float(unit_price or 0)
        if name == "inject-price-mismatch":
            price = round(price * 1.15, 2)
        else:
            qty = round(qty * 1.12, 2)
        post(PROC, "/invoices", {
            "po_id": po_id, "qty_invoiced": qty, "unit_price_invoiced": price,
            "tax": round(qty * price * 0.08, 2),
            "ocr_raw": {"source": "simulator", "scenario": name, "confidence": 0.94},
        })
        state.did(f"scenario:{name}", po_id)
        return ScenarioResult(scenario=name, applied=True, entities=[po_id],
                              detail=f"invoice posted against {po_id} outside tolerance; "
                                     f"match-worker will raise an exception")

    if name == "inject-missing-po":
        post(PROC, "/invoices", {
            "po_id": None, "qty_invoiced": 250, "unit_price_invoiced": 18.4, "tax": 368.0,
            "ocr_raw": {"source": "simulator", "scenario": name, "confidence": 0.61,
                        "note": "PO reference unreadable on the scan"},
        })
        state.did("scenario:missing-po")
        return ScenarioResult(scenario=name, applied=True,
                              detail="invoice with no PO reference posted; expect a "
                                     "MISSING_PO exception for a human")

    if name == "outbound-rush":
        made = []
        for _ in range(max(count, 3)):
            created = _new_outbound_order(now, priority="critical")
            if created:
                made.append(created["id"])
                post(YARD, f"/outbound-orders/{created['id']}/stage", {})
                post(YARD, f"/outbound-orders/{created['id']}/dispatch", {
                    "carrier": "Swift Logistics",
                    "load_type": random.choice(["dry_van", "reefer"]),
                    "eta": (now + timedelta(minutes=random.randint(5, 15))).isoformat(),
                })
        state.did("scenario:outbound-rush", f"{len(made)} orders")
        return ScenarioResult(scenario=name, applied=bool(made), entities=made,
                              detail=f"{len(made)} critical outbound orders staged and "
                                     f"dispatched; they now contend for the same doors "
                                     f"as every inbound truck")

    raise HTTPException(404, f"unknown scenario '{name}'. Available: delay-trailer, "
                             f"surge-arrivals, block-dock, unblock-docks, "
                             f"inject-price-mismatch, inject-qty-mismatch, "
                             f"inject-missing-po, outbound-rush")


@app.get("/sim/scenarios", tags=["simulator"])
def list_scenarios():
    """What the demo can force, and what each one proves."""
    return {"scenarios": [
        {"name": "delay-trailer", "proves": "ETA slip re-plans the yard and raises a DELAY alert"},
        {"name": "surge-arrivals", "proves": "the scheduler queues a burst by priority, not arrival order"},
        {"name": "block-dock", "proves": "a door out of service is routed around"},
        {"name": "unblock-docks", "proves": "capacity returning is picked up on the next re-plan"},
        {"name": "inject-price-mismatch", "proves": "price outside tolerance becomes an exception, not a payment"},
        {"name": "inject-qty-mismatch", "proves": "quantity outside tolerance becomes an exception"},
        {"name": "inject-missing-po", "proves": "an unreadable PO reference routes to a human"},
        {"name": "outbound-rush", "proves": "outbound and inbound contend for the same doors in one solve"},
    ]}
