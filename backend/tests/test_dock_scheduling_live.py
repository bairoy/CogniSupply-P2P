"""
Dock scheduling against the REAL running stack.

Per CLAUDE.md's testing discipline, the unit tests in test_dock_engine.py are
not enough on their own: they prove the planner computes the right plan, not
that dock-worker consumes the right events, writes the locked reassignment
pattern, releases doors on unload, or that the yard-wide no-overlap invariant
actually holds in Postgres. That only exists across processes, so this file
makes real HTTP calls and reads real rows.

Run:  ./run.sh start && ./.venv/bin/python -m pytest backend/tests/test_dock_scheduling_live.py -v

This file CREATES data, unlike test_auth.py -- a scheduling decision cannot be
observed without a trailer to schedule. It therefore owns a disposable PO chain
of its own and deletes every row it created (including its event_log entries,
which are test artefacts and have no business in a demo's live event rail).
Seeded data is read but never modified.
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
OPERATOR = os.environ.get("DEMO_OPERATOR", "priya@inbound.dev")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "inbound2026")

# The worker reacts through Redis, so every assertion about its output has to
# be waited for rather than assumed. Generous: a re-plan is milliseconds, but
# the reconciler publishes on a ~1s tick.
WORKER_TIMEOUT_SECONDS = 25


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def token():
    try:
        httpx.get(f"{GATEWAY}/health", timeout=5).raise_for_status()
        httpx.get(f"{YARD}/health", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.skip(f"stack not running ({exc}); start it with ./run.sh start")

    res = httpx.post(f"{GATEWAY}/auth/login",
                     json={"email": OPERATOR, "password": DEMO_PASSWORD}, timeout=15)
    res.raise_for_status()
    return res.json()["token"]


@pytest.fixture(scope="module")
def api(token):
    return httpx.Client(base_url=YARD, headers={"Authorization": f"Bearer {token}"},
                        timeout=20)


@pytest.fixture(scope="module")
def scratch_po():
    """
    A disposable PO for this file's trailers, and the cleanup that removes
    every trace of them afterwards.

    Deliberately not one of the seeded POs: unloading against a PO that already
    has an invoice would wake match-worker and write PR2 rows the ground-truth
    file does not expect, which would turn a scheduling test into a source of
    false 3-way-match failures.
    """
    po_id = f"PO-TEST-{int(time.time())}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM suppliers ORDER BY id LIMIT 1")
            supplier = cur.fetchone()[0]
            cur.execute("SELECT id FROM materials ORDER BY id LIMIT 1")
            material = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO purchase_orders (id, supplier_id, material_id, qty,
                                                unit_price, delivery_location_id,
                                                expected_delivery, status)
                   VALUES (%s,%s,%s,100,10,'LOC-001',now() + interval '1 day','CREATED')""",
                (po_id, supplier, material),
            )
        conn.commit()

    yield po_id

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM shipments WHERE po_id = %s", (po_id,))
            shipments = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT id FROM trailers WHERE shipment_id = ANY(%s)", (shipments,))
            trailers = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT id FROM dock_assignments WHERE trailer_id = ANY(%s)",
                        (trailers,))
            assignments = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT id FROM goods_receipts WHERE po_id = %s", (po_id,))
            receipts = [r[0] for r in cur.fetchall()]

            cur.execute("DELETE FROM alerts WHERE entity_id = ANY(%s)",
                        (trailers + assignments,))
            cur.execute("DELETE FROM event_log WHERE entity_id = ANY(%s)",
                        (trailers + assignments + shipments + receipts + [po_id],))
            cur.execute("DELETE FROM goods_receipts WHERE po_id = %s", (po_id,))
            cur.execute("DELETE FROM dock_assignments WHERE trailer_id = ANY(%s)", (trailers,))
            cur.execute("DELETE FROM tracking_events WHERE trailer_id = ANY(%s)", (trailers,))
            cur.execute("DELETE FROM trailers WHERE id = ANY(%s)", (trailers,))
            cur.execute("DELETE FROM shipments WHERE po_id = %s", (po_id,))
            cur.execute("DELETE FROM purchase_orders WHERE id = %s", (po_id,))
        conn.commit()


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def create_trailer(api, po_id, *, load_type="dry_van", priority="normal", eta_minutes=45):
    eta = datetime.now(timezone.utc) + timedelta(minutes=eta_minutes)
    shipment = api.post("/shipments", json={
        "po_id": po_id, "carrier": "Scheduling Test Lines",
        "expected_arrival": eta.isoformat(),
    })
    shipment.raise_for_status()
    trailer = api.post(f"/shipments/{shipment.json()['id']}/trailers",
                       json={"load_type": load_type, "priority": priority})
    trailer.raise_for_status()
    return trailer.json()["id"]


def query(sql, params=()):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.rollback()
    return rows


def wait_for(fn, *, what, timeout=WORKER_TIMEOUT_SECONDS):
    """Poll until the worker has done something, or fail saying what was missed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.4)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def current_assignment(trailer_id):
    rows = query(
        """SELECT id, dock_id, status, planned_start, planned_end, reason,
                  score_breakdown
           FROM dock_assignments
           WHERE trailer_id = %s AND status IN ('ASSIGNED','CONFIRMED')""",
        (trailer_id,),
    )
    return rows[0] if rows else None


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

def test_a_departing_trailer_is_scheduled_with_a_window_and_a_reason(api, scratch_po):
    """TRAILER_DEPARTED -> dock-worker plans -> a real row with a real window."""
    trailer_id = create_trailer(api, scratch_po, load_type="reefer", priority="high")

    assignment = wait_for(lambda: current_assignment(trailer_id),
                          what=f"dock-worker to schedule {trailer_id}")
    da_id, dock_id, status, planned_start, planned_end, reason, detail = assignment

    assert status == "ASSIGNED"
    assert planned_start is not None and planned_end is not None
    assert planned_end > planned_start

    # Hard constraints actually held against the real docks table.
    compatible, active, service = query(
        """SELECT %s = ANY(compatible_load_types), is_active,
                  COALESCE((metadata->>'expected_unload_minutes')::numeric, 45)::int
           FROM docks WHERE id = %s""", ("reefer", dock_id))[0]
    assert compatible and active
    assert planned_end - planned_start == timedelta(minutes=service)

    # Explainability is persisted, not just computed.
    assert dock_id in reason
    assert detail["cost_terms"]["total"] == detail["cost"]
    assert detail["engine"] in ("cp-sat", "greedy")
    assert detail["plan"]["greedy_baseline_cost"] >= detail["plan"]["total_cost"]
    assert "alternatives" in detail and "rejected" in detail


def test_no_two_live_assignments_overlap_on_the_same_door(api, scratch_po):
    """
    The yard-wide invariant, checked in Postgres across seeded rows and
    everything this file has created: one door, one trailer, at a time.
    """
    create_trailer(api, scratch_po, load_type="dry_van", priority="critical", eta_minutes=20)
    wait_for(lambda: query(
        """SELECT 1 FROM dock_assignments da JOIN trailers t ON t.id = da.trailer_id
           JOIN shipments s ON s.id = t.shipment_id
           WHERE s.po_id = %s AND da.status IN ('ASSIGNED','CONFIRMED')""",
        (scratch_po,)), what="the second trailer to be scheduled")

    overlaps = query(
        """SELECT a.id, b.id, a.dock_id
           FROM dock_assignments a
           JOIN dock_assignments b
             ON a.dock_id = b.dock_id AND a.id < b.id
           WHERE a.status IN ('ASSIGNED','CONFIRMED')
             AND b.status IN ('ASSIGNED','CONFIRMED')
             AND a.planned_start < b.planned_end
             AND b.planned_start < a.planned_end"""
    )
    assert overlaps == [], f"doors double-booked: {overlaps}"


def test_every_live_assignment_satisfies_the_hard_constraints(api, scratch_po):
    """No live row anywhere in the yard points at an inactive or incompatible door."""
    violations = query(
        """SELECT da.id, da.dock_id, t.load_type, d.compatible_load_types, d.is_active
           FROM dock_assignments da
           JOIN trailers t ON t.id = da.trailer_id
           JOIN docks d ON d.id = da.dock_id
           WHERE da.status IN ('ASSIGNED','CONFIRMED')
             AND (NOT d.is_active
                  OR (t.load_type IS NOT NULL
                      AND NOT (t.load_type = ANY(d.compatible_load_types))))"""
    )
    assert violations == []


def test_arriving_then_docking_then_unloading_releases_the_door(api, scratch_po):
    """
    The whole movement, and the release that matters: after unload the
    assignment is COMPLETED, so the scheduler can give that door to someone
    else. This is the state transition the engine's hard constraint depends on.
    """
    trailer_id = create_trailer(api, scratch_po, load_type="dry_van", eta_minutes=5)
    assignment = wait_for(lambda: current_assignment(trailer_id),
                          what=f"{trailer_id} to be scheduled")
    da_id, dock_id = assignment[0], assignment[1]

    api.post(f"/trailers/{trailer_id}/arrive").raise_for_status()
    assert query("SELECT status FROM trailers WHERE id=%s", (trailer_id,))[0][0] == "ARRIVED"

    api.post(f"/trailers/{trailer_id}/dock").raise_for_status()
    docked_status, docked_at = query(
        "SELECT status, docked_at FROM dock_assignments WHERE id=%s", (da_id,))[0]
    assert docked_status == "CONFIRMED"
    assert docked_at is not None

    released = api.post(f"/trailers/{trailer_id}/unload", json={"qty_received": 100})
    released.raise_for_status()
    assert released.json()["released_dock_id"] == dock_id

    assert query("SELECT status FROM dock_assignments WHERE id=%s", (da_id,))[0][0] == "COMPLETED"
    assert current_assignment(trailer_id) is None

    # ...and the outbound leg exists, distinct from unloading.
    departed = api.post(f"/trailers/{trailer_id}/depart")
    departed.raise_for_status()
    assert departed.json()["status"] == "DEPARTED"
    assert query("SELECT status FROM trailers WHERE id=%s", (trailer_id,))[0][0] == "DEPARTED"
    assert query("""SELECT 1 FROM event_log WHERE entity_id=%s AND event_type='TRAILER_EXITED'""",
                 (trailer_id,))


def test_unloading_cannot_be_walked_back_and_depart_needs_unload(api, scratch_po):
    """State-machine guards: the KPIs are only meaningful if the order is real."""
    trailer_id = create_trailer(api, scratch_po, eta_minutes=30)
    wait_for(lambda: current_assignment(trailer_id), what=f"{trailer_id} to be scheduled")

    assert api.post(f"/trailers/{trailer_id}/dock").status_code == 409     # not ARRIVED yet
    assert api.post(f"/trailers/{trailer_id}/depart").status_code == 409   # not UNLOADED yet

    api.post(f"/trailers/{trailer_id}/arrive").raise_for_status()
    assert api.post(f"/trailers/{trailer_id}/arrive").status_code == 409   # already ARRIVED


def test_manual_override_is_honoured_and_never_silently_reverted(api, scratch_po):
    """
    The operator's door choice is pinned. A subsequent re-plan (triggered here
    by a new trailer arriving in the yard) must plan around it, not undo it --
    otherwise the override button would be a lie.
    """
    trailer_id = create_trailer(api, scratch_po, load_type="dry_van", eta_minutes=60)
    assignment = wait_for(lambda: current_assignment(trailer_id),
                          what=f"{trailer_id} to be scheduled")
    da_id, original_dock = assignment[0], assignment[1]

    target = query(
        """SELECT id FROM docks
           WHERE is_active AND 'dry_van' = ANY(compatible_load_types) AND id <> %s
           ORDER BY id DESC LIMIT 1""", (original_dock,))[0][0]

    override = api.post(f"/dock-assignments/{da_id}/reassign",
                        json={"new_dock_id": target, "reason": "test override"})
    override.raise_for_status()
    body = override.json()
    assert body["planned_start"] and body["planned_end"]

    # Old row kept as history, new row live -- the locked reassignment pattern.
    assert query("SELECT status FROM dock_assignments WHERE id=%s", (da_id,))[0][0] == "REASSIGNED"
    new_id, new_dock, _, _, _, _, detail = current_assignment(trailer_id)
    assert new_id == body["new_assignment_id"]
    assert new_dock == target
    assert detail["source"] == "manual_override"

    # Force several re-plans, then confirm the override still stands.
    create_trailer(api, scratch_po, load_type="dry_van", priority="critical", eta_minutes=10)
    time.sleep(6)
    assert current_assignment(trailer_id)[1] == target


def test_a_material_eta_change_reschedules_and_a_trivial_one_does_not(api, scratch_po):
    """redis-contract.md §9: the 10-minute threshold, observed end to end."""
    trailer_id = create_trailer(api, scratch_po, load_type="dry_van", eta_minutes=120)
    before = wait_for(lambda: current_assignment(trailer_id),
                      what=f"{trailer_id} to be scheduled")
    original_window = before[3]

    def tick(eta_minutes):
        eta = datetime.now(timezone.utc) + timedelta(minutes=eta_minutes)
        res = api.post(f"/trailers/{trailer_id}/tracking", json={
            "latitude": 41.85, "longitude": -87.65, "speed": 55,
            "eta_estimate": eta.isoformat(),
        })
        res.raise_for_status()
        return res.json()

    assert tick(118)["eta_changed_materially"] is False
    time.sleep(3)
    assert current_assignment(trailer_id)[3] == original_window, \
        "a 2-minute ETA jitter must not move the plan"

    assert tick(240)["eta_changed_materially"] is True
    wait_for(lambda: current_assignment(trailer_id)[3] != original_window,
             what="the plan to follow a two-hour ETA slip")
    assert current_assignment(trailer_id)[3] > original_window


def test_dock_schedule_endpoint_reports_the_committed_windows(api, scratch_po):
    trailer_id = create_trailer(api, scratch_po, load_type="flatbed", eta_minutes=90)
    assignment = wait_for(lambda: current_assignment(trailer_id),
                          what=f"{trailer_id} to be scheduled")
    dock_id = assignment[1]

    schedule = api.get("/dock-schedule", params={"hours": 12})
    schedule.raise_for_status()
    body = schedule.json()

    door = next(d for d in body["docks"] if d["id"] == dock_id)
    assert any(b["trailer_id"] == trailer_id for b in door["bookings"])
    assert 0 <= door["utilisation_pct"] <= 100
    assert door["committed_minutes"] > 0
    assert body["summary"]["docks_active"] >= 1
    # Every door in the response is reported, including the out-of-service one,
    # because "which doors exist and why can't I use one" is the question.
    assert len(body["docks"]) == query("SELECT count(*) FROM docks")[0][0]


def test_yard_status_summarises_movement_in_both_directions(api, scratch_po):
    res = api.get("/yard-status")
    res.raise_for_status()
    body = res.json()
    summary = body["summary"]

    for key in ("inbound", "in_yard_waiting", "at_door", "awaiting_exit", "unassigned",
                "docks_active", "docks_busy", "dock_occupancy_pct",
                "avg_wait_minutes", "longest_wait_minutes"):
        assert key in summary, f"missing {key}"

    assert summary["inbound"] == sum(1 for t in body["trailers"] if t["status"] == "EN_ROUTE")
    assert 0 <= summary["dock_occupancy_pct"] <= 100
    assert all(t["status"] != "DEPARTED" for t in body["trailers"])

    # A scheduled trailer carries its window to the UI, not just its door.
    scheduled = [t for t in body["trailers"] if t["dock_assignment"]]
    assert scheduled, "seeded yard should have scheduled trailers"
    assert all(t["dock_assignment"]["planned_start"] for t in scheduled)
