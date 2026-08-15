"""
Outbound operations against the REAL running stack (v7).

Same discipline as test_dock_scheduling_live.py, and for the same reason: the
claims worth proving about outbound are all cross-process. That an outbound
truck is planned by the SAME optimiser as inbound traffic, that loading
releases the door, that the ownership and direction guards actually refuse the
wrong call -- none of that exists inside one function, so none of it can be
tested by calling one.

Run:  ./run.sh start && ./.venv/bin/python -m pytest backend/tests/test_outbound_live.py -v

Creates its own outbound orders and deletes every row it created, including
event_log entries -- test artefacts have no business in a demo's live event rail.
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT.parent / ".env")

from shared.db import get_conn  # noqa: E402

YARD = os.environ.get("YARD_API", "http://127.0.0.1:8001")
GATEWAY = os.environ.get("GATEWAY_API", "http://127.0.0.1:8003")
OPERATOR = os.environ.get("DEMO_OPERATOR", "priya@cognisupply.in")
FINANCE = os.environ.get("DEMO_FINANCE", "sneha@cognisupply.in")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "inbound2026")

WORKER_TIMEOUT_SECONDS = 25


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

def _login(email):
    res = httpx.post(f"{GATEWAY}/auth/login",
                     json={"email": email, "password": DEMO_PASSWORD}, timeout=15)
    res.raise_for_status()
    return res.json()["token"]


@pytest.fixture(scope="module")
def api():
    try:
        httpx.get(f"{GATEWAY}/health", timeout=5).raise_for_status()
        httpx.get(f"{YARD}/health", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.skip(f"stack not running ({exc}); start it with ./run.sh start")
    return httpx.Client(base_url=YARD,
                        headers={"Authorization": f"Bearer {_login(OPERATOR)}"}, timeout=20)


@pytest.fixture(scope="module")
def finance_api():
    return httpx.Client(base_url=YARD,
                        headers={"Authorization": f"Bearer {_login(FINANCE)}"}, timeout=20)


@pytest.fixture
def orders():
    """Tracks every order this test made, and removes all of it afterwards."""
    created: list[str] = []
    yield created

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM shipments WHERE outbound_order_id = ANY(%s)", (created,))
            shipments = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT id FROM trailers WHERE shipment_id = ANY(%s)", (shipments,))
            trailers = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT id FROM dock_assignments WHERE trailer_id = ANY(%s)", (trailers,))
            assignments = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT id FROM goods_issues WHERE outbound_order_id = ANY(%s)", (created,))
            issues = [r[0] for r in cur.fetchall()]

            cur.execute("DELETE FROM alerts WHERE entity_id = ANY(%s)", (trailers + assignments,))
            cur.execute("DELETE FROM event_log WHERE entity_id = ANY(%s)",
                        (trailers + assignments + shipments + issues + created,))
            cur.execute("DELETE FROM goods_issues WHERE outbound_order_id = ANY(%s)", (created,))
            cur.execute("DELETE FROM dock_assignments WHERE trailer_id = ANY(%s)", (trailers,))
            cur.execute("DELETE FROM tracking_events WHERE trailer_id = ANY(%s)", (trailers,))
            cur.execute("DELETE FROM trailers WHERE id = ANY(%s)", (trailers,))
            cur.execute("DELETE FROM shipments WHERE outbound_order_id = ANY(%s)", (created,))
            cur.execute("DELETE FROM load_plans WHERE outbound_order_id = ANY(%s)", (created,))
            cur.execute("DELETE FROM outbound_orders WHERE id = ANY(%s)", (created,))
        conn.commit()


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def query(sql, params=()):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.rollback()
    return rows


def wait_for(fn, *, what, timeout=WORKER_TIMEOUT_SECONDS):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.4)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def make_order(api, orders, *, priority="normal", lines=2, qty=120):
    materials = [r[0] for r in query("SELECT id FROM materials ORDER BY id LIMIT %s", (lines,))]
    res = api.post("/outbound-orders", json={
        "customer_name": "Outbound Test Co",
        "priority": priority,
        "requested_ship_date": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
        "lines": [{"material_id": m, "qty": qty} for m in materials],
    })
    res.raise_for_status()
    body = res.json()
    orders.append(body["id"])
    return body


def current_assignment(trailer_id):
    rows = query(
        """SELECT id, dock_id, status, planned_start, planned_end
           FROM dock_assignments
           WHERE trailer_id = %s AND status IN ('ASSIGNED','CONFIRMED')""",
        (trailer_id,),
    )
    return rows[0] if rows else None


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

def test_order_and_load_plan_commit_together(api, orders):
    """
    An order and its pick lines are one transaction, and both events land.

    There is no useful state where an order exists without the lines that
    fulfil it -- the warehouse could never pick it -- so a partial write here
    would be a genuine defect rather than a cosmetic one.
    """
    order = make_order(api, orders, lines=2)
    assert order["status"] == "PLANNED"
    assert len(order["lines"]) == 2

    lines = query("SELECT status, qty_staged, qty_loaded FROM load_plans "
                  "WHERE outbound_order_id=%s", (order["id"],))
    assert len(lines) == 2
    assert all(status == "PLANNED" and staged == 0 and loaded == 0
               for status, staged, loaded in lines)

    events = [r[0] for r in query(
        "SELECT event_type FROM event_log WHERE entity_id=%s ORDER BY id", (order["id"],))]
    assert events == ["OUTBOUND_ORDER_CREATED", "LOAD_PLAN_CREATED"]


def test_dispatch_refuses_an_unstaged_order(api, orders):
    """
    Outbound's one ordering rule, which inbound has no equivalent of: a dock
    door is never committed to a load nobody has picked yet.
    """
    order = make_order(api, orders)
    res = api.post(f"/outbound-orders/{order['id']}/dispatch", json={})
    assert res.status_code == 409
    assert "STAGED" in res.json()["detail"]
    assert query("SELECT count(*) FROM shipments WHERE outbound_order_id=%s",
                 (order["id"],))[0][0] == 0


def test_short_pick_is_recorded_not_rejected(api, orders):
    """
    A truck that turns up for 120 and can only be given 80 still leaves with 80.
    Refusing to model that would put the system at odds with the warehouse floor.
    """
    order = make_order(api, orders, lines=1, qty=120)
    line_id = order["lines"][0]["load_plan_id"]

    res = api.post(f"/outbound-orders/{order['id']}/stage",
                   json={"lines": [{"load_plan_id": line_id, "qty_staged": 80}]})
    res.raise_for_status()
    assert res.json()["lines"][0]["status"] == "SHORT"
    # Every line is resolved, so the order is still dispatchable.
    assert res.json()["ready_to_dispatch"] is True

    status, staged = query(
        "SELECT status, qty_staged FROM load_plans WHERE id=%s", (line_id,))[0]
    assert status == "SHORT" and float(staged) == 80


def test_outbound_trailer_is_scheduled_by_the_same_planner(api, orders):
    """
    THE claim of the whole v7 design: an outbound truck is planned by the same
    dock-worker, in the same CP-SAT solve, against the same doors -- with no
    outbound-specific trigger, subscription or scheduling path.

    Nothing in this test tells the worker the trailer is outbound. It gets a
    door because TRAILER_DEPARTED is TRAILER_DEPARTED.
    """
    order = make_order(api, orders, priority="high")
    api.post(f"/outbound-orders/{order['id']}/stage", json={}).raise_for_status()

    res = api.post(f"/outbound-orders/{order['id']}/dispatch", json={
        "carrier": "Outbound Test Lines", "load_type": "dry_van",
        "eta": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    })
    res.raise_for_status()
    trailer_id = res.json()["trailer_id"]

    assert query("SELECT direction FROM trailers WHERE id=%s", (trailer_id,))[0][0] == "OUTBOUND"
    assert query("SELECT direction, po_id FROM shipments WHERE id=%s",
                 (res.json()["shipment_id"],))[0] == ("OUTBOUND", None)

    assignment = wait_for(lambda: current_assignment(trailer_id),
                          what=f"dock-worker to schedule outbound {trailer_id}")
    _, dock_id, status, planned_start, planned_end = assignment
    assert status == "ASSIGNED"
    assert planned_start is not None and planned_end > planned_start

    # It was planned by the real engine, not a fallback, and the door it got is
    # one that genuinely accepts its load type.
    breakdown = query("SELECT score_breakdown FROM dock_assignments WHERE trailer_id=%s "
                      "AND status='ASSIGNED'", (trailer_id,))[0][0]
    assert breakdown.get("source") == "dock-worker"
    compatible = query("SELECT compatible_load_types FROM docks WHERE id=%s", (dock_id,))[0][0]
    assert "dry_van" in compatible


def test_one_door_is_never_double_booked_across_directions(api, orders):
    """
    The invariant that a second, direction-specific scheduler would break: no
    two live windows overlap on the same door, whichever way their trucks face.
    """
    order = make_order(api, orders, priority="critical")
    api.post(f"/outbound-orders/{order['id']}/stage", json={}).raise_for_status()
    res = api.post(f"/outbound-orders/{order['id']}/dispatch", json={"load_type": "dry_van"})
    res.raise_for_status()
    wait_for(lambda: current_assignment(res.json()["trailer_id"]),
             what="the outbound trailer to enter the plan")

    overlaps = query("""
        SELECT a.dock_id, a.trailer_id, b.trailer_id
        FROM dock_assignments a
        JOIN dock_assignments b
          ON a.dock_id = b.dock_id AND a.id < b.id
         AND a.status IN ('ASSIGNED','CONFIRMED')
         AND b.status IN ('ASSIGNED','CONFIRMED')
         AND a.planned_start < b.planned_end
         AND b.planned_start < a.planned_end
    """)
    assert overlaps == [], f"doors double-booked: {overlaps}"


def test_loading_releases_the_door_and_writes_a_goods_issue(api, orders):
    """
    The outbound mirror of unload: goods_issues is written, the door is
    released, and GOODS_ISSUED is emitted so queued trailers re-plan.
    """
    order = make_order(api, orders, lines=2, qty=150)
    api.post(f"/outbound-orders/{order['id']}/stage", json={}).raise_for_status()
    dispatched = api.post(f"/outbound-orders/{order['id']}/dispatch",
                          json={"load_type": "dry_van"}).json()
    trailer_id = dispatched["trailer_id"]

    assignment = wait_for(lambda: current_assignment(trailer_id), what="a door")
    dock_id = assignment[1]

    api.post(f"/trailers/{trailer_id}/arrive").raise_for_status()
    api.post(f"/trailers/{trailer_id}/dock").raise_for_status()
    # The order reaches LOADING -- a status that would otherwise be declared in
    # schema.sql and never reachable.
    assert query("SELECT status FROM outbound_orders WHERE id=%s",
                 (order["id"],))[0][0] == "LOADING"

    loaded = api.post(f"/trailers/{trailer_id}/load", json={})
    loaded.raise_for_status()
    body = loaded.json()
    assert body["qty_issued"] == 300  # 2 lines x 150
    assert body["released_dock_id"] == dock_id

    assert query("SELECT status FROM trailers WHERE id=%s", (trailer_id,))[0][0] == "LOADED"
    assert query("SELECT status FROM outbound_orders WHERE id=%s",
                 (order["id"],))[0][0] == "SHIPPED"
    # The door is genuinely free again, not merely marked done.
    assert query("""SELECT count(*) FROM dock_assignments
                    WHERE trailer_id=%s AND status IN ('ASSIGNED','CONFIRMED')""",
                 (trailer_id,))[0][0] == 0

    gi = query("SELECT qty_issued, lines FROM goods_issues WHERE outbound_order_id=%s",
               (order["id"],))
    assert len(gi) == 1 and float(gi[0][0]) == 300
    assert len(gi[0][1]) == 2

    assert query("""SELECT count(*) FROM event_log
                    WHERE event_type='GOODS_ISSUED'
                      AND payload->>'outbound_order_id'=%s""", (order["id"],))[0][0] == 1


def test_gate_out_and_delivery_close_the_outbound_story(api, orders):
    order = make_order(api, orders, lines=1)
    api.post(f"/outbound-orders/{order['id']}/stage", json={}).raise_for_status()
    trailer_id = api.post(f"/outbound-orders/{order['id']}/dispatch",
                          json={"load_type": "dry_van"}).json()["trailer_id"]
    wait_for(lambda: current_assignment(trailer_id), what="a door")

    api.post(f"/trailers/{trailer_id}/arrive").raise_for_status()
    api.post(f"/trailers/{trailer_id}/dock").raise_for_status()

    # Delivery before the goods are even on the truck must not be possible.
    assert api.post(f"/trailers/{trailer_id}/deliver").status_code == 409

    api.post(f"/trailers/{trailer_id}/load", json={}).raise_for_status()
    api.post(f"/trailers/{trailer_id}/depart").raise_for_status()
    assert query("SELECT status FROM trailers WHERE id=%s", (trailer_id,))[0][0] == "DEPARTED"

    api.post(f"/trailers/{trailer_id}/deliver").raise_for_status()
    assert query("SELECT status FROM trailers WHERE id=%s", (trailer_id,))[0][0] == "DELIVERED"
    assert query("SELECT status FROM outbound_orders WHERE id=%s",
                 (order["id"],))[0][0] == "DELIVERED"

    events = [r[0] for r in query(
        "SELECT event_type FROM event_log WHERE entity_id=%s ORDER BY id", (order["id"],))]
    assert events[0] == "OUTBOUND_ORDER_CREATED"
    assert events[-1] == "OUTBOUND_DELIVERED"


def test_direction_guards_refuse_the_wrong_verb(api, orders):
    """
    Unloading an outbound trailer would write a goods RECEIPT for goods that
    are leaving, which match-worker would then feed to the 3-way matcher
    against a PO that does not exist. Cheap guard, expensive failure.
    """
    order = make_order(api, orders, lines=1)
    api.post(f"/outbound-orders/{order['id']}/stage", json={}).raise_for_status()
    trailer_id = api.post(f"/outbound-orders/{order['id']}/dispatch",
                          json={"load_type": "dry_van"}).json()["trailer_id"]

    res = api.post(f"/trailers/{trailer_id}/unload", json={"qty_received": 10})
    assert res.status_code == 409 and "OUTBOUND" in res.json()["detail"]
    assert query("SELECT count(*) FROM goods_receipts WHERE trailer_id=%s",
                 (trailer_id,))[0][0] == 0

    inbound = query("""SELECT id FROM trailers
                       WHERE direction='INBOUND' AND status IN ('ARRIVED','DOCKED') LIMIT 1""")
    if inbound:
        res = api.post(f"/trailers/{inbound[0][0]}/load", json={})
        assert res.status_code == 409 and "INBOUND" in res.json()["detail"]


def test_outbound_write_is_a_separate_capability(finance_api, api, orders):
    """
    outbound:write is its own capability, not folded into yard:write -- so a
    site can delegate outbound to a 3PL without handing over the whole yard.
    Finance holds neither.
    """
    res = finance_api.post("/outbound-orders", json={
        "customer_name": "Should Not Exist",
        "lines": [{"material_id": "MAT-001", "qty": 1}],
    })
    assert res.status_code == 403
    assert "outbound:write" in res.json()["detail"]
    assert query("SELECT count(*) FROM outbound_orders WHERE customer_name='Should Not Exist'"
                 )[0][0] == 0


def test_unknown_material_is_rejected_before_anything_is_written(api, orders):
    res = api.post("/outbound-orders", json={
        "customer_name": "Outbound Test Co",
        "lines": [{"material_id": "MAT-DOES-NOT-EXIST", "qty": 10}],
    })
    assert res.status_code == 404
    assert query("SELECT count(*) FROM outbound_orders WHERE customer_name='Outbound Test Co' "
                 "AND created_at > now() - interval '10 seconds'")[0][0] == 0
