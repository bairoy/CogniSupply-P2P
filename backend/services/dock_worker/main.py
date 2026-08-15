"""
Dock scheduling worker. Background consume() loop, no HTTP surface.

Consumer group: dock-worker
allowed_event_types: SHIPMENT_CREATED, TRAILER_DEPARTED, ETA_UPDATED,
                     TRAILER_LOCATION_UPDATED, TRAILER_ARRIVED, TRAILER_DOCKED,
                     GOODS_RECEIVED, GOODS_ISSUED, DOCK_REASSIGNED
                     (exactly redis-contract.md §5)

v7: ONE PLANNER, BOTH DIRECTIONS

This worker schedules outbound trucks too, and there is nothing in here that
says so -- which is the design, not an oversight. An outbound trailer moves
through EN_ROUTE and ARRIVED exactly as an inbound one does and emits the same
TRAILER_DEPARTED / TRAILER_ARRIVED / TRAILER_DOCKED events, so it enters
_load_state's sweep and the same CP-SAT solve with no branch anywhere.

GOODS_ISSUED joins the allowed set for the same reason GOODS_RECEIVED is in it:
both mean a door just freed. The only genuinely outbound rule -- never commit a
door to a load that has not been picked -- is enforced upstream, by Yard API
refusing to dispatch an order that is not STAGED. By the time a trailer row
exists for the planner to see, its goods are already in the staging lane, so
the planner needs no readiness concept of its own.

WHAT THIS FILE IS

The scheduling itself lives in shared/dock_engine.py. This file is the I/O
shell around it: read the whole yard's current state, hand it to the planner,
write back the difference, emit the events.

v6 changed the shape of the job. Before, each event scored ONE trailer against
whatever doors happened to be free at that instant. Now every triggering event
re-plans the whole pending set over time, because that is the only way the
answer can account for the questions the use case actually asks -- will this
door be free during that truck's service window, how long will this truck wait,
what does giving it this door do to the truck behind it.

Re-planning everything on every event is affordable at yard scale (tens of
docks, tens of pending trailers, a CP-SAT solve in milliseconds) and it is what
makes recommendations self-correcting: any state change re-derives the whole
plan from the database, so there is no incremental bookkeeping to drift.

WHAT IS IMMOVABLE

Two things are never re-planned, and enter the model as fixed windows instead:

  * a trailer physically at a door (trailers.status = DOCKED). Re-assigning
    steel that is already backed into a door is not a plan, it's a fiction.
  * an operator's manual override (score_breakdown.source = 'manual_override',
    written by Yard API's reassign endpoint). If the optimiser could quietly
    undo a human decision on the next GPS tick, the override button would be a
    lie.

WHY THIS DOES NOT LOOP

The worker consumes DOCK_REASSIGNED so that an operator override immediately
re-plans everything around it. It also *emits* DOCK_REASSIGNED, so the payload
carries `source`, and the worker ignores its own. Even without that guard the
plan is a pure function of committed state and is stable under re-planning: a
trailer that was moved to a door despite paying the churn cost keeps that door
when the move is already paid for.

Run:  ./.venv/bin/python backend/services/dock_worker/main.py
"""

import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

# .env must load BEFORE event_bus is imported -- it reads REDIS_URL at module
# import time, so a later load would be ignored.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT.parent / ".env")

from event_bus import consume, reconcile_unpublished, record_event  # noqa: E402
from shared.db import get_conn  # noqa: E402
from shared.dock_engine import (  # noqa: E402
    DEFAULT_SERVICE_MINUTES,
    LONG_WAIT_ALERT_MINUTES,
    Booking,
    DockState,
    TrailerRequest,
    breakdown,
    explain,
    plan_docks,
)
from shared.ids import next_id  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  dock-worker  %(levelname)-7s %(message)s",
)
log = logging.getLogger("dock-worker")

GROUP = "dock-worker"
ALLOWED = {
    "SHIPMENT_CREATED",
    "TRAILER_DEPARTED",
    "ETA_UPDATED",
    "TRAILER_LOCATION_UPDATED",
    "TRAILER_ARRIVED",
    "TRAILER_DOCKED",
    "GOODS_RECEIVED",
    "GOODS_ISSUED",     # v7 -- the outbound dock-release signal
    "DOCK_REASSIGNED",
}

# redis-contract.md §9: re-plan on an ETA change only when the new ETA differs
# from the ETA last used for planning by >= 10 minutes. The API applies the same
# threshold before emitting ETA_UPDATED at all; this is the worker's own
# defensive re-check against its own last-planned ETA, so two small emitted
# changes that each cleared 10 min from the previous tick cannot compound into a
# re-plan the worker never evaluated. Belt and braces, cheap to keep both.
RESCORE_THRESHOLD_MINUTES = 10

# A trailer already at a door is immovable until it leaves, so if it overruns
# its planned window the door is still genuinely blocked. Extending the fixed
# booking by this much keeps the scheduler from promising the door to someone
# else at a moment it demonstrably is not free.
OVERRUN_GRACE_MINUTES = 5

# HOW WORKER-EMITTED EVENTS REACH REDIS
#
# consume() owns the transaction boundary: it calls the handler, then commits
# the processed_events claim together with whatever the handler wrote. A
# handler therefore must NOT publish inline -- at handler time the event_log
# row is not committed yet, and publishing something that might roll back is
# exactly what the write-ordering contract forbids.
#
# So handlers only record_event() (redis_published stays FALSE), and a small
# background thread runs reconcile_unpublished() on its OWN connection.
# That is the mechanism event_bus.py already documents for this: "finds
# committed events that never reached Redis and republishes them". Using it
# here means the worker adds no new publishing path -- one sanctioned
# mechanism, roughly one second of delivery latency, and no risk of an event
# reaching Redis for work that never committed.

RECONCILE_INTERVAL_SECONDS = 1.0


def _reconciler_loop():
    """Publish committed-but-unpublished events. Own connection, own thread."""
    while True:
        try:
            with get_conn() as conn:
                reconcile_unpublished(conn, limit=200)
                conn.commit()
        except Exception:
            log.exception("reconciler pass failed; retrying")
        time.sleep(RECONCILE_INTERVAL_SECONDS)


# ─────────────────────────────────────────────
# Reading the yard
# ─────────────────────────────────────────────

def _load_state(cur, now):
    """
    Everything the planner needs, in three queries.

    Returns (docks, requests, live) where `live` maps trailer_id -> the current
    ASSIGNED/CONFIRMED row, so the caller can diff the plan against it.
    """
    cur.execute(
        """SELECT id, compatible_load_types, yard_position, is_active,
                  COALESCE((metadata->>'expected_unload_minutes')::numeric, %s)::int
           FROM docks ORDER BY id""",
        (DEFAULT_SERVICE_MINUTES,),
    )
    docks = {
        r[0]: DockState(dock_id=r[0], yard_position=r[2] or 0,
                        compatible_load_types=list(r[1] or []), is_active=r[3],
                        service_minutes=r[4])
        for r in cur.fetchall()
    }

    cur.execute(
        """SELECT da.id, da.trailer_id, da.dock_id, da.status, da.planned_start,
                  da.planned_end, da.docked_at, da.score_breakdown->>'source',
                  (da.score_breakdown->>'wait_minutes')::int,
                  t.status, t.load_type, t.priority, t.eta
           FROM dock_assignments da
           JOIN trailers t ON t.id = da.trailer_id
           WHERE da.status IN ('ASSIGNED','CONFIRMED')
           ORDER BY da.id"""
    )
    live, pinned = {}, set()
    for (da_id, trailer_id, dock_id, da_status, planned_start, planned_end, docked_at,
         source, prior_wait, trailer_status, load_type, priority, eta) in cur.fetchall():
        live[trailer_id] = {
            "assignment_id": da_id, "dock_id": dock_id, "status": da_status,
            "planned_start": planned_start, "planned_end": planned_end,
            "docked_at": docked_at, "source": source, "prior_wait": prior_wait,
        }
        # Immovable: at the door, or an operator said so.
        if trailer_status == "DOCKED" or source == "manual_override":
            pinned.add(trailer_id)
            service = docks[dock_id].service_minutes if dock_id in docks else DEFAULT_SERVICE_MINUTES
            start = docked_at or planned_start or eta or now
            end = planned_end or (start + timedelta(minutes=service))
            if trailer_status == "DOCKED":
                # Still unloading past its window: the door is not free yet.
                end = max(end, now + timedelta(minutes=OVERRUN_GRACE_MINUTES))
            if dock_id in docks:
                docks[dock_id].bookings.append(
                    Booking(trailer_id=trailer_id, start=start, end=end, assignment_id=da_id))

    # v7: no direction filter, deliberately. Outbound trailers reach EN_ROUTE and
    # ARRIVED through exactly the same states as inbound ones, so they land in
    # this sweep without a special case -- which is the point. Both directions
    # are handed to ONE plan_docks() call and compete for the same doors in the
    # same solve. Filtering by direction here, and solving twice, would let two
    # trucks be promised the same door for the same fifteen minutes.
    cur.execute(
        """SELECT id, load_type, priority, eta, status, direction FROM trailers
           WHERE status IN ('EN_ROUTE','ARRIVED') ORDER BY id"""
    )
    requests = []
    for trailer_id, load_type, priority, eta, status, direction in cur.fetchall():
        if trailer_id in pinned:
            continue
        # A trailer already in the yard is ready now; one still on the road is
        # ready when it gets here, and never earlier than now. True in both
        # directions -- an outbound truck's ETA is its arrival at OUR gate to
        # collect, so the same arithmetic applies unchanged.
        ready = now if status == "ARRIVED" else max(eta or now, now)
        current = live.get(trailer_id)
        requests.append(TrailerRequest(
            trailer_id=trailer_id, load_type=load_type, priority=priority or "normal",
            ready_at=ready, direction=direction or "INBOUND",
            current_dock_id=current["dock_id"] if current else None,
            current_assignment_id=current["assignment_id"] if current else None,
        ))

    return list(docks.values()), requests, live


# ─────────────────────────────────────────────
# Writing the plan back
# ─────────────────────────────────────────────

def _open_conflict_alert(cur, trailer_id) -> bool:
    """An unacknowledged DOCK_CONFLICT already stands for this trailer."""
    cur.execute(
        """SELECT 1 FROM alerts
           WHERE entity_type='trailer' AND entity_id=%s
             AND alert_type='DOCK_CONFLICT' AND NOT acknowledged
           LIMIT 1""",
        (trailer_id,),
    )
    return cur.fetchone() is not None


def _raise_alert(conn, cur, *, entity_type, entity_id, alert_type, message, severity):
    alert_id = next_id(cur, "ALT")
    cur.execute(
        """INSERT INTO alerts (id, entity_type, entity_id, alert_type, message, severity)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (alert_id, entity_type, entity_id, alert_type, message, severity),
    )
    return alert_id


def _apply(conn, cur, plan, requests, live, now):
    """
    Persist the difference between the plan and what is currently committed.

    Three cases per trailer, and the middle one matters as much as the others:
    a re-plan that confirms the existing door writes the refreshed window but
    emits nothing. Silence is the correct output for "nothing changed" -- an
    event per GPS tick would drown the live rail and teach an operator to
    ignore it.
    """
    by_id = {t.trailer_id: t for t in requests}
    assigned = reassigned = confirmed = 0

    for trailer_id, planned in sorted(plan.assignments.items()):
        trailer = by_id[trailer_id]
        current = live.get(trailer_id)
        reason = explain(plan, planned, trailer)
        detail = breakdown(plan, planned, trailer)
        detail["source"] = "dock-worker"

        if current and current["dock_id"] == planned.dock_id:
            cur.execute(
                """UPDATE dock_assignments
                   SET reason=%s, score_breakdown=%s, planned_start=%s, planned_end=%s
                   WHERE id=%s""",
                (reason, json.dumps(detail), planned.start, planned.end,
                 current["assignment_id"]),
            )
            confirmed += 1
            _maybe_flag_long_wait(conn, cur, trailer_id, current["assignment_id"], planned,
                                  previous_wait=current.get("prior_wait"))
            continue

        if current:
            cur.execute("UPDATE dock_assignments SET status='REASSIGNED' WHERE id=%s",
                        (current["assignment_id"],))

        new_id = next_id(cur, "DA")
        cur.execute(
            """INSERT INTO dock_assignments (id, trailer_id, dock_id, status, reason,
                                             score_breakdown, planned_start, planned_end)
               VALUES (%s,%s,%s,'ASSIGNED',%s,%s,%s,%s)""",
            (new_id, trailer_id, planned.dock_id, reason, json.dumps(detail),
             planned.start, planned.end),
        )

        if current:
            payload = {
                "summary": (f"{trailer_id} moved {current['dock_id']} -> {planned.dock_id}, "
                            f"saving {_saved(current, planned)}"),
                "old_assignment_id": current["assignment_id"],
                "old_dock_id": current["dock_id"],
                "new_dock_id": planned.dock_id,
                "trailer_id": trailer_id,
                "reason": reason,
                "planned_start": planned.start.isoformat(),
                "planned_end": planned.end.isoformat(),
                "wait_minutes": planned.wait_minutes,
                "cost": planned.cost,
                "direction": trailer.direction,
                "source": "dock-worker",
            }
            record_event(conn, "dock_assignment", new_id, "DOCK_REASSIGNED", payload)
            reassigned += 1
        else:
            payload = {
                "summary": f"{trailer_id} scheduled at {planned.dock_id}"
                           + (f", {planned.wait_minutes} min wait"
                              if planned.wait_minutes else ", no wait"),
                "trailer_id": trailer_id,
                "dock_id": planned.dock_id,
                "reason": reason,
                "planned_start": planned.start.isoformat(),
                "planned_end": planned.end.isoformat(),
                "wait_minutes": planned.wait_minutes,
                "cost": planned.cost,
                "priority": trailer.priority,
                "load_type": trailer.load_type,
                "direction": trailer.direction,
                "source": "dock-worker",
            }
            record_event(conn, "dock_assignment", new_id, "DOCK_ASSIGNED", payload)
            assigned += 1

        _maybe_flag_long_wait(conn, cur, trailer_id, new_id, planned, previous_wait=None)

    for stuck in plan.unplaceable:
        if live.get(stuck.trailer_id) or _open_conflict_alert(cur, stuck.trailer_id):
            continue
        alert_id = _raise_alert(conn, cur, entity_type="trailer", entity_id=stuck.trailer_id,
                                alert_type="DOCK_CONFLICT", message=stuck.reason,
                                severity="warning")
        record_event(conn, "alert", alert_id, "ALERT_CREATED",
                     {"summary": stuck.reason, "alert_type": "DOCK_CONFLICT",
                      "trailer_id": stuck.trailer_id})
        log.warning("no eligible dock for %s", stuck.trailer_id)

    if assigned or reassigned or confirmed or plan.unplaceable:
        log.info("re-plan [%s %s]: %d assigned, %d moved, %d confirmed, %d unplaceable "
                 "| cost %d vs %d greedy",
                 plan.engine, plan.status, assigned, reassigned, confirmed,
                 len(plan.unplaceable), plan.total_cost, plan.greedy_cost)
    return assigned + reassigned


def _saved(current, planned) -> str:
    """Human-readable justification for a move, used in the event summary."""
    prior = current.get("prior_wait")
    if prior is None:
        return "a better slot"
    delta = prior - planned.wait_minutes
    return f"{delta} min of waiting" if delta > 0 else "a better overall plan"


def _maybe_flag_long_wait(conn, cur, trailer_id, assignment_id, planned, previous_wait):
    """
    DOCK_DELAYED, finally emitted honestly.

    The v5 engine deliberately never emitted it: with no window data, "this
    trailer will miss its slot" was not computable and faking it would have
    contradicted the engine's own framing. v6 has planned windows, so a long
    planned wait is now a real, derived fact -- the trailer is here (or will
    be) and the earliest door in the optimal plan is this far out.

    Only fired on crossing the threshold, not on every re-plan that leaves the
    trailer still waiting.
    """
    if planned.wait_minutes < LONG_WAIT_ALERT_MINUTES:
        return
    if previous_wait is not None and previous_wait >= LONG_WAIT_ALERT_MINUTES:
        return  # already flagged; re-planning has not made it newly bad

    # Durations, not wall-clock times: the exact window is on the assignment
    # row and every client renders it in the viewer's own timezone.
    message = f"{trailer_id} waits {planned.wait_minutes} min for {planned.dock_id}"
    _raise_alert(conn, cur, entity_type="dock_assignment", entity_id=assignment_id,
                 alert_type="DELAY", message=message,
                 severity="critical" if planned.wait_minutes >= 120 else "warning")
    record_event(conn, "dock_assignment", assignment_id, "DOCK_DELAYED", {
        "summary": message,
        "trailer_id": trailer_id,
        "dock_id": planned.dock_id,
        "wait_minutes": planned.wait_minutes,
        "planned_start": planned.start.isoformat(),
        "alert_type": "DELAY",
    })
    log.info("%s: DOCK_DELAYED, %d min wait", trailer_id, planned.wait_minutes)


def replan(conn, cur, *, now=None) -> int:
    """Read the yard, plan it, write back the difference. Returns writes made."""
    now = now or datetime.now(timezone.utc)
    docks, requests, live = _load_state(cur, now)
    if not requests:
        return 0
    plan = plan_docks(docks=docks, trailers=requests, now=now)
    return _apply(conn, cur, plan, requests, live, now)


# ─────────────────────────────────────────────
# Event handling
# ─────────────────────────────────────────────

def _eta_change_is_material(cur, trailer_id, payload) -> bool:
    """
    Compare against the ETA THIS WORKER last planned with (stored in
    score_breakdown), not against the previous GPS tick.

    A large ETA slip is also a real trailer delay in its own right, regardless
    of which door it ends up at, so it raises a DELAY alert on the trailer --
    distinct from the DOCK_DELAYED above, which is about queueing for a door.
    """
    cur.execute(
        """SELECT score_breakdown->>'scored_with_eta' FROM dock_assignments
           WHERE trailer_id=%s AND status IN ('ASSIGNED','CONFIRMED')""",
        (trailer_id,),
    )
    row = cur.fetchone()
    new_eta_raw = payload.get("new_eta")
    delta = payload.get("delta_minutes") or 0

    if row and row[0] and new_eta_raw:
        try:
            planned_with = datetime.fromisoformat(row[0])
            new_eta = datetime.fromisoformat(new_eta_raw)
            delta = abs((new_eta - planned_with).total_seconds()) / 60
        except ValueError:
            pass

    if delta < RESCORE_THRESHOLD_MINUTES:
        log.info("%s: ETA moved %.1f min, below %d min threshold -- no re-plan",
                 trailer_id, delta, RESCORE_THRESHOLD_MINUTES)
        return False
    return True


def _flag_trailer_delay(conn, cur, trailer_id, payload):
    delta = payload.get("delta_minutes") or 0
    if delta < RESCORE_THRESHOLD_MINUTES or payload.get("direction") != "later":
        return
    message = f"{trailer_id} delayed {round(delta)} min (ETA now {payload.get('new_eta')})"
    alert_id = _raise_alert(conn, cur, entity_type="trailer", entity_id=trailer_id,
                            alert_type="DELAY", message=message,
                            severity="critical" if delta >= 60 else "warning")
    record_event(conn, "alert", alert_id, "ALERT_CREATED",
                 {"summary": message, "alert_type": "DELAY", "trailer_id": trailer_id,
                  "delta_minutes": round(delta, 1)})


def handler(conn, fields):
    event_type = fields["event_type"]
    entity_id = fields["entity_id"]
    payload = fields["payload"]

    with conn.cursor() as cur:
        # No trailer row exists yet to plan a door for; real action waits for
        # TRAILER_DEPARTED. Tracking ticks never re-plan by themselves, per
        # redis-contract.md §9. Both are still claimed via processed_events.
        if event_type in ("SHIPMENT_CREATED", "TRAILER_LOCATION_UPDATED"):
            return

        if event_type == "ETA_UPDATED":
            _flag_trailer_delay(conn, cur, entity_id, payload)
            if not _eta_change_is_material(cur, entity_id, payload):
                return

        # An override the worker itself just made is already reflected in the
        # database it is about to read -- re-planning off it is pure churn.
        if event_type == "DOCK_REASSIGNED" and payload.get("source") == "dock-worker":
            return

        # Everything else -- a trailer departing, arriving, taking a door,
        # releasing one, or an operator overriding an assignment -- changes the
        # state the plan is derived from, so the plan is re-derived.
        replan(conn, cur)


def main():
    log.info("starting; group=%s allowed=%s", GROUP, sorted(ALLOWED))

    threading.Thread(target=_reconciler_loop, daemon=True, name="reconciler").start()

    # A worker that has been down misses events it can never claim again, so it
    # starts by re-deriving the plan from committed state. This is the same
    # function every event triggers -- there is no separate recovery path to
    # keep correct.
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                replan(conn, cur)
            conn.commit()
    except Exception:
        log.exception("startup re-plan failed; continuing to the event loop")

    with get_conn() as conn:
        consume(conn, GROUP, "dock-worker-1", handler, allowed_event_types=ALLOWED)


if __name__ == "__main__":
    main()
