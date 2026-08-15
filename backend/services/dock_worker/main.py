"""
Dock scoring worker. Background consume() loop, no HTTP surface.

Consumer group: dock-worker
allowed_event_types: SHIPMENT_CREATED, TRAILER_DEPARTED, ETA_UPDATED,
                     TRAILER_LOCATION_UPDATED, GOODS_RECEIVED

GOODS_RECEIVED is the v4 addition recorded in BUILD_PLAN.md §2.2. It closes the
limitation DOCK_DECISION_ENGINE.md flags honestly at the bottom of the file:
nothing previously re-triggered scoring for a WAITING trailer when a DIFFERENT
trailer's dock freed up, so a trailer that found no dock stayed unassigned
until its own next ETA update. A goods receipt is exactly the dock-release
signal, so the worker now reacts to it.

The scoring itself lives in shared/dock_engine.py -- this file is the I/O
shell around it: read state, call the engine, write the result, emit the event.

Run:  ./.venv/bin/python backend/services/dock_worker/main.py
"""

import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

# .env must load BEFORE event_bus is imported -- it reads REDIS_URL at module
# import time, so a later load would be ignored.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT.parent / ".env")

from event_bus import consume, reconcile_unpublished, record_event  # noqa: E402
from shared.db import get_conn  # noqa: E402
from shared.dock_engine import decide  # noqa: E402
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
    "GOODS_RECEIVED",
}

# redis-contract.md §9: re-score only when the new ETA differs from the ETA
# last used for scoring by >= 10 minutes. The API applies the same threshold
# before emitting ETA_UPDATED at all; this is the worker's own defensive
# re-check against its own last-scored ETA, so two small emitted changes that
# each cleared 10 min from the previous tick cannot compound into a re-score
# the worker never evaluated. Belt and braces, cheap to keep both.
RESCORE_THRESHOLD_MINUTES = 10

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


def _fetch_docks(cur):
    cur.execute("SELECT id, compatible_load_types, yard_position, is_active FROM docks")
    return [
        {"id": r[0], "compatible_load_types": r[1], "yard_position": r[2], "is_active": r[3]}
        for r in cur.fetchall()
    ]


def _occupancy(cur):
    cur.execute(
        "SELECT dock_id, trailer_id FROM dock_assignments WHERE status IN ('ASSIGNED','CONFIRMED')"
    )
    return {r[0]: r[1] for r in cur.fetchall()}


def _raise_dock_conflict(conn, cur, trailer_id, reason):
    """No candidate survived Stage 1: alert instead of creating an assignment."""
    alert_id = next_id(cur, "ALT")
    cur.execute(
        """INSERT INTO alerts (id, entity_type, entity_id, alert_type, message, severity)
           VALUES (%s,'trailer',%s,'DOCK_CONFLICT',%s,'warning')""",
        (alert_id, trailer_id, reason),
    )
    payload = {"summary": reason, "alert_type": "DOCK_CONFLICT", "trailer_id": trailer_id}
    record_event(conn, "alert", alert_id, "ALERT_CREATED", payload)
    log.warning("no eligible dock for %s", trailer_id)


def _score_and_assign(conn, cur, trailer_id, *, rescore: bool):
    """
    Stage 1 + Stage 2 for one trailer, then persist the outcome.

    On re-score the locked reassignment pattern applies: never UPDATE dock_id
    in place -- mark the old row REASSIGNED and INSERT a new one, so
    dock_assignments stays a genuine history table.
    """
    cur.execute(
        "SELECT load_type, priority, eta, status FROM trailers WHERE id = %s",
        (trailer_id,),
    )
    row = cur.fetchone()
    if row is None:
        log.warning("trailer %s vanished", trailer_id)
        return
    load_type, priority, eta, status = row

    if status in ("UNLOADED",):
        return  # nothing to dock

    cur.execute(
        """SELECT id, dock_id, score_breakdown FROM dock_assignments
           WHERE trailer_id=%s AND status IN ('ASSIGNED','CONFIRMED')""",
        (trailer_id,),
    )
    current = cur.fetchone()

    if current and status == "DOCKED":
        # Already physically at the door -- re-scoring now would be theatre.
        return

    decision = decide(
        docks=_fetch_docks(cur),
        trailer_id=trailer_id,
        trailer_load_type=load_type,
        priority=priority or "normal",
        occupancy=_occupancy(cur),
    )

    if decision.winner is None:
        if not current:
            _raise_dock_conflict(conn, cur, trailer_id, decision.reason)
        return

    breakdown = dict(decision.breakdown)
    # Remember which ETA this scoring pass was based on, so the next
    # ETA_UPDATED can compare against it rather than against the previous tick.
    breakdown["scored_with_eta"] = eta.isoformat() if eta else None
    breakdown["scored_at"] = datetime.now(timezone.utc).isoformat()

    if current and current[1] == decision.winner:
        # Same dock still wins: no domain write, no event. Re-scoring confirmed
        # the existing assignment -- nothing changed.
        log.info("%s: re-score confirmed %s", trailer_id, decision.winner)
        cur.execute(
            "UPDATE dock_assignments SET score_breakdown=%s WHERE id=%s",
            (json.dumps(breakdown), current[0]),
        )
        return

    if current:
        cur.execute("UPDATE dock_assignments SET status='REASSIGNED' WHERE id=%s", (current[0],))

    new_id = next_id(cur, "DA")
    cur.execute(
        """INSERT INTO dock_assignments (id, trailer_id, dock_id, status, reason, score_breakdown)
           VALUES (%s,%s,%s,'ASSIGNED',%s,%s)""",
        (new_id, trailer_id, decision.winner, decision.reason, json.dumps(breakdown)),
    )

    if current:
        payload = {
            "summary": f"{trailer_id} reassigned {current[1]} -> {decision.winner} "
                       f"after ETA change",
            "old_assignment_id": current[0],
            "old_dock_id": current[1],
            "new_dock_id": decision.winner,
            "trailer_id": trailer_id,
            "reason": decision.reason,
            "final_score": breakdown.get("final_score"),
        }
        event_type = "DOCK_REASSIGNED"
    else:
        payload = {
            "summary": decision.reason,
            "trailer_id": trailer_id,
            "dock_id": decision.winner,
            "reason": decision.reason,
            "final_score": breakdown.get("final_score"),
            "priority": priority,
            "load_type": load_type,
        }
        event_type = "DOCK_ASSIGNED"

    record_event(conn, "dock_assignment", new_id, event_type, payload)
    log.info("%s: %s -> %s (%s)", trailer_id, event_type, decision.winner,
             breakdown.get("final_score"))


def _handle_eta_updated(conn, cur, trailer_id, payload):
    """
    Re-score only past the threshold, comparing against the ETA THIS WORKER
    last scored with (stored in score_breakdown), not the previous GPS tick.

    Separately: a large ETA slip is a real trailer delay regardless of which
    dock ends up assigned, so it raises a DELAY alert. DOCK_DELAYED is
    deliberately NOT emitted -- schema.sql has no dock availability window or
    service duration, so "will miss its window" is not honestly computable and
    the engine does not pretend otherwise.
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
            scored_with = datetime.fromisoformat(row[0])
            new_eta = datetime.fromisoformat(new_eta_raw)
            delta = abs((new_eta - scored_with).total_seconds()) / 60
        except ValueError:
            pass

    if delta >= RESCORE_THRESHOLD_MINUTES and payload.get("direction") == "later":
        alert_id = next_id(cur, "ALT")
        message = (f"{trailer_id} delayed {round(delta)} min "
                   f"(ETA now {payload.get('new_eta')})")
        cur.execute(
            """INSERT INTO alerts (id, entity_type, entity_id, alert_type, message, severity)
               VALUES (%s,'trailer',%s,'DELAY',%s,%s)""",
            (alert_id, trailer_id, message, "critical" if delta >= 60 else "warning"),
        )
        ap = {"summary": message, "alert_type": "DELAY", "trailer_id": trailer_id,
              "delta_minutes": round(delta, 1)}
        record_event(conn, "alert", alert_id, "ALERT_CREATED", ap)

    if delta < RESCORE_THRESHOLD_MINUTES:
        log.info("%s: ETA moved %.1f min, below %d min threshold -- no re-score",
                 trailer_id, delta, RESCORE_THRESHOLD_MINUTES)
        return

    _score_and_assign(conn, cur, trailer_id, rescore=True)


def _handle_dock_released(conn, cur, released_dock_id):
    """
    v4: a dock just freed up. Re-score trailers that are waiting WITHOUT any
    current assignment, oldest ETA first, so a trailer that previously found
    nothing eligible gets a door as soon as one exists.
    """
    cur.execute(
        """SELECT t.id FROM trailers t
           WHERE t.status IN ('EN_ROUTE','ARRIVED')
             AND NOT EXISTS (
                 SELECT 1 FROM dock_assignments da
                 WHERE da.trailer_id = t.id AND da.status IN ('ASSIGNED','CONFIRMED'))
           ORDER BY t.eta NULLS LAST
           LIMIT 5"""
    )
    waiting = [r[0] for r in cur.fetchall()]
    if not waiting:
        return
    log.info("dock %s released -- re-scoring %d waiting trailer(s)",
             released_dock_id, len(waiting))
    for trailer_id in waiting:
        _score_and_assign(conn, cur, trailer_id, rescore=False)


def handler(conn, fields):
    event_type = fields["event_type"]
    entity_id = fields["entity_id"]
    payload = fields["payload"]

    with conn.cursor() as cur:
        if event_type == "SHIPMENT_CREATED":
            # No-op: no trailer row exists yet to score a dock for. Real action
            # waits for TRAILER_DEPARTED. Still claimed via processed_events.
            return

        if event_type == "TRAILER_LOCATION_UPDATED":
            # Tracking only, per redis-contract.md §9. Never re-scores by itself.
            return

        if event_type == "TRAILER_DEPARTED":
            _score_and_assign(conn, cur, entity_id, rescore=False)
            return

        if event_type == "ETA_UPDATED":
            _handle_eta_updated(conn, cur, entity_id, payload)
            return

        if event_type == "GOODS_RECEIVED":
            _handle_dock_released(conn, cur, payload.get("released_dock_id"))
            return


def main():
    log.info("starting; group=%s allowed=%s", GROUP, sorted(ALLOWED))

    threading.Thread(target=_reconciler_loop, daemon=True, name="reconciler").start()

    with get_conn() as conn:
        consume(conn, GROUP, "dock-worker-1", handler, allowed_event_types=ALLOWED)


if __name__ == "__main__":
    main()
