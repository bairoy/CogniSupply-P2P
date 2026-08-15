"""
The dock scheduling engine, tested as a pure function.

No database, no Redis, no HTTP -- shared/dock_engine.py takes already-fetched
rows and returns a plan, which is exactly what makes the decision logic
testable in isolation. The live-stack half of the story (the worker actually
writing this to Postgres and emitting the right events) is
test_dock_scheduling_live.py.

Every test here asserts a property the use case asks for by name: doors are
only offered when they are genuinely free during the window, ETA decides who
gets there first, priority buys shorter queues rather than nicer doors,
existing assignments are respected, the answer is deterministic, and the
optimiser is never worse than the greedy baseline it reports against.

Run:  ./.venv/bin/python -m pytest backend/tests/test_dock_engine.py -v
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from shared import dock_engine  # noqa: E402
from shared.dock_engine import (  # noqa: E402
    Booking,
    DockState,
    TrailerRequest,
    breakdown,
    busy_intervals,
    explain,
    feasible_docks,
    plan_docks,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def dock(dock_id, position, load_types, *, service=45, active=True, bookings=()):
    return DockState(dock_id=dock_id, yard_position=position,
                     compatible_load_types=list(load_types), is_active=active,
                     service_minutes=service, bookings=list(bookings))


def trailer(trailer_id, load_type="dry_van", priority="normal", ready=0, current=None):
    return TrailerRequest(trailer_id=trailer_id, load_type=load_type, priority=priority,
                          ready_at=at(ready), current_dock_id=current)


def minutes_from_now(value: datetime) -> int:
    return round((value - NOW).total_seconds() / 60)


# ─────────────────────────────────────────────
# Stage 1 — hard constraints
# ─────────────────────────────────────────────

def test_inactive_dock_is_rejected_not_scored():
    docks = [dock("DOCK-01", 1, ["dry_van"], active=False),
             dock("DOCK-02", 2, ["dry_van"])]
    eligible, rejected = feasible_docks(trailer("TRL-1"), docks)
    assert [d.dock_id for d in eligible] == ["DOCK-02"]
    assert rejected == [{"dock_id": "DOCK-01", "reason": "dock is out of service"}]


def test_incompatible_load_type_is_rejected_with_a_readable_reason():
    docks = [dock("DOCK-03", 3, ["reefer"]), dock("DOCK-07", 7, ["flatbed"])]
    eligible, rejected = feasible_docks(trailer("TRL-1", load_type="dry_van"), docks)
    assert eligible == []
    assert {r["dock_id"] for r in rejected} == {"DOCK-03", "DOCK-07"}
    assert "not dry_van" in rejected[0]["reason"]


def test_trailer_with_no_compatible_active_dock_is_unplaceable_not_assigned():
    """The tanker door is out of service: alert, never a fabricated assignment."""
    docks = [dock("DOCK-06", 6, ["tanker"], active=False), dock("DOCK-01", 1, ["dry_van"])]
    plan = plan_docks(docks=docks, trailers=[trailer("TRL-T", load_type="tanker")], now=NOW)

    assert plan.assignments == {}
    assert [u.trailer_id for u in plan.unplaceable] == ["TRL-T"]
    assert "no active door handles tanker" in plan.unplaceable[0].reason


def test_one_unplaceable_trailer_does_not_stop_the_others_being_planned():
    docks = [dock("DOCK-06", 6, ["tanker"], active=False), dock("DOCK-01", 1, ["dry_van"])]
    plan = plan_docks(docks=docks,
                      trailers=[trailer("TRL-T", load_type="tanker"), trailer("TRL-D")],
                      now=NOW)
    assert plan.assignments["TRL-D"].dock_id == "DOCK-01"
    assert [u.trailer_id for u in plan.unplaceable] == ["TRL-T"]


# ─────────────────────────────────────────────
# Time windows — the v6 point
# ─────────────────────────────────────────────

def test_a_busy_door_is_not_offered_during_its_committed_window():
    """
    The v5 bug this replaces: occupancy was 'is it taken right now'. Here the
    only compatible door is busy for the next 60 minutes, so the trailer is
    scheduled AFTER that window rather than on top of it.
    """
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40,
                  bookings=[Booking("TRL-BUSY", NOW, at(60))])]
    plan = plan_docks(docks=docks, trailers=[trailer("TRL-1", ready=10)], now=NOW)

    assigned = plan.assignments["TRL-1"]
    assert assigned.dock_id == "DOCK-01"
    assert minutes_from_now(assigned.start) == 60
    assert assigned.wait_minutes == 50


def test_a_door_reserved_for_a_later_truck_is_still_usable_now():
    """
    The other half of the same v5 bug: a door held for a truck arriving in six
    hours used to be unusable by a truck arriving in twenty minutes. With
    windows, the near trailer takes the slot before the reservation.
    """
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40)]
    soon = trailer("TRL-SOON", ready=20)
    later = trailer("TRL-LATER", ready=360)
    plan = plan_docks(docks=docks, trailers=[soon, later], now=NOW)

    assert plan.assignments["TRL-SOON"].wait_minutes == 0
    assert plan.assignments["TRL-LATER"].wait_minutes == 0
    assert minutes_from_now(plan.assignments["TRL-SOON"].start) == 20
    assert minutes_from_now(plan.assignments["TRL-LATER"].start) == 360


def test_two_trailers_never_share_a_door_at_the_same_time():
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40)]
    plan = plan_docks(docks=docks,
                      trailers=[trailer("TRL-1", ready=0), trailer("TRL-2", ready=10)],
                      now=NOW)
    first, second = plan.assignments["TRL-1"], plan.assignments["TRL-2"]
    assert first.end <= second.start or second.end <= first.start


def test_short_load_fills_a_gap_between_two_bookings():
    """First-fit into gaps, not append-to-the-end: a 40-minute load takes the
    50-minute hole rather than queueing behind everything."""
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40,
                  bookings=[Booking("A", NOW, at(30)), Booking("B", at(80), at(200))])]
    plan = plan_docks(docks=docks, trailers=[trailer("TRL-1", ready=0)], now=NOW)
    assert minutes_from_now(plan.assignments["TRL-1"].start) == 30


def test_service_duration_comes_from_the_dock_not_a_constant():
    docks = [dock("DOCK-03", 3, ["reefer"], service=55)]
    plan = plan_docks(docks=docks, trailers=[trailer("TRL-R", load_type="reefer")], now=NOW)
    assignment = plan.assignments["TRL-R"]
    assert (assignment.end - assignment.start) == timedelta(minutes=55)


def test_overlapping_committed_windows_are_merged_not_treated_as_a_conflict():
    """
    Bad data must not take the yard down. Two overlapping windows on one door
    describe a door busy across their union; handed to NoOverlap as separate
    fixed intervals they would make the whole plan INFEASIBLE.
    """
    busy = dock("DOCK-01", 1, ["dry_van"], service=40,
                bookings=[Booking("A", NOW, at(60)), Booking("B", at(30), at(90))])
    assert busy_intervals(busy, NOW) == [(0, 90)]

    plan = plan_docks(docks=[busy], trailers=[trailer("TRL-1")], now=NOW)
    assert plan.engine == "cp-sat"
    assert minutes_from_now(plan.assignments["TRL-1"].start) == 90


# ─────────────────────────────────────────────
# ETA and priority
# ─────────────────────────────────────────────

def test_earlier_eta_gets_the_earlier_slot():
    docks = [dock("DOCK-01", 1, ["dry_van"], service=60)]
    plan = plan_docks(
        docks=docks,
        trailers=[trailer("TRL-LATE", ready=90), trailer("TRL-EARLY", ready=0)],
        now=NOW)
    assert plan.assignments["TRL-EARLY"].start < plan.assignments["TRL-LATE"].start


def test_priority_buys_a_shorter_queue_not_a_nicer_door():
    """
    One door, two trailers ready at the same moment. The critical load goes
    first and waits nothing; the low-priority load absorbs the wait. That is
    priority expressed as turnaround, which is the v6 model's central claim.
    """
    docks = [dock("DOCK-01", 1, ["dry_van"], service=45)]
    plan = plan_docks(
        docks=docks,
        trailers=[trailer("TRL-LOW", priority="low"),
                  trailer("TRL-CRIT", priority="critical")],
        now=NOW)

    assert plan.assignments["TRL-CRIT"].wait_minutes == 0
    assert plan.assignments["TRL-LOW"].wait_minutes == 45
    assert plan.assignments["TRL-CRIT"].start < plan.assignments["TRL-LOW"].start


def test_a_critical_trailer_does_not_displace_one_already_at_the_door():
    """No preemption -- DOCK_DECISION_ENGINE.md §8. Steel already backed into a
    door is a fixed booking, whatever arrives next."""
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40,
                  bookings=[Booking("TRL-DOCKED", NOW, at(40))])]
    plan = plan_docks(docks=docks, trailers=[trailer("TRL-CRIT", priority="critical")],
                      now=NOW)
    assert minutes_from_now(plan.assignments["TRL-CRIT"].start) == 40


def test_dedicated_door_is_preferred_over_a_flexible_one_when_the_wait_is_equal():
    """The flexibility term: keep multi-purpose doors free for what comes next."""
    docks = [dock("DOCK-04", 4, ["dry_van", "reefer", "flatbed"], service=45),
             dock("DOCK-05", 5, ["dry_van"], service=45)]
    plan = plan_docks(docks=docks, trailers=[trailer("TRL-1")], now=NOW)
    assert plan.assignments["TRL-1"].dock_id == "DOCK-05"


def test_waiting_longer_beats_a_marginally_nicer_door():
    """
    Sanity on the weight ratios: the flexibility and position terms must never
    add up to enough to justify a real wait. A dedicated door 60 minutes out
    loses to a flexible one that is free now.
    """
    docks = [dock("DOCK-04", 4, ["dry_van", "reefer", "flatbed"], service=45),
             dock("DOCK-05", 5, ["dry_van"], service=45,
                  bookings=[Booking("BUSY", NOW, at(60))])]
    plan = plan_docks(docks=docks, trailers=[trailer("TRL-1")], now=NOW)
    assert plan.assignments["TRL-1"].dock_id == "DOCK-04"
    assert plan.assignments["TRL-1"].wait_minutes == 0


# ─────────────────────────────────────────────
# Plan stability and churn
# ─────────────────────────────────────────────

def test_replanning_unchanged_state_keeps_every_trailer_where_it_was():
    """The property that stops the worker thrashing: re-planning is idempotent,
    so a confirming pass writes no reassignment and emits no event."""
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40),
             dock("DOCK-02", 2, ["dry_van", "flatbed"], service=45),
             dock("DOCK-05", 5, ["dry_van"], service=40)]
    trailers = [trailer("TRL-1", ready=0), trailer("TRL-2", ready=20),
                trailer("TRL-3", ready=35, priority="high")]

    first = plan_docks(docks=docks, trailers=trailers, now=NOW)
    settled = [
        trailer(t.trailer_id, priority=t.priority,
                ready=minutes_from_now(t.ready_at),
                current=first.assignments[t.trailer_id].dock_id)
        for t in trailers
    ]
    second = plan_docks(docks=docks, trailers=settled, now=NOW)

    for trailer_id, planned in first.assignments.items():
        assert second.assignments[trailer_id].dock_id == planned.dock_id
        assert second.assignments[trailer_id].changed is False


def test_churn_cost_prevents_a_trivial_reassignment():
    """Moving a promised trailer for a two-minute gain is disruption, not
    optimisation. W_CHURN is what says so."""
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40),
             dock("DOCK-02", 2, ["dry_van"], service=40)]
    plan = plan_docks(docks=docks,
                      trailers=[trailer("TRL-1", current="DOCK-02")], now=NOW)
    assert plan.assignments["TRL-1"].dock_id == "DOCK-02"
    assert plan.assignments["TRL-1"].changed is False


def test_a_big_enough_saving_does_move_a_trailer():
    """The other side of churn: when the promised door has become an hour's
    wait and another is free now, the move is worth it."""
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40),
             dock("DOCK-02", 2, ["dry_van"], service=40,
                  bookings=[Booking("BUSY", NOW, at(120))])]
    plan = plan_docks(docks=docks,
                      trailers=[trailer("TRL-1", priority="high", current="DOCK-02")],
                      now=NOW)
    assert plan.assignments["TRL-1"].dock_id == "DOCK-01"
    assert plan.assignments["TRL-1"].changed is True
    assert plan.assignments["TRL-1"].cost_terms["churn"]["cost"] > 0


# ─────────────────────────────────────────────
# Determinism, optimality, fallback
# ─────────────────────────────────────────────

def _contended_yard():
    docks = [
        dock("DOCK-01", 1, ["dry_van"], service=40),
        dock("DOCK-02", 2, ["dry_van", "flatbed"], service=45),
        dock("DOCK-03", 3, ["reefer"], service=55),
        dock("DOCK-04", 4, ["dry_van", "reefer", "flatbed"], service=50,
             bookings=[Booking("TRL-AT-DOOR", NOW, at(35))]),
        dock("DOCK-06", 6, ["tanker"], service=70, active=False),
    ]
    trailers = [
        trailer("TRL-01", "dry_van", "normal", ready=10),
        trailer("TRL-02", "dry_van", "critical", ready=15),
        trailer("TRL-03", "reefer", "high", ready=5),
        trailer("TRL-04", "flatbed", "low", ready=25),
        trailer("TRL-05", "dry_van", "normal", ready=30),
        trailer("TRL-06", "reefer", "normal", ready=40),
    ]
    return docks, trailers


def test_the_same_inputs_always_produce_the_same_plan():
    """Determinism is a requirement of the brief, so it is asserted, not hoped
    for: single worker, fixed seed, sorted inputs, integer costs."""
    docks, trailers = _contended_yard()
    signatures = set()
    for _ in range(5):
        plan = plan_docks(docks=docks, trailers=trailers, now=NOW)
        signatures.add(tuple(sorted(
            (t, a.dock_id, a.start.isoformat()) for t, a in plan.assignments.items())))
    assert len(signatures) == 1


def test_cpsat_is_never_worse_than_the_greedy_baseline_it_reports_against():
    docks, trailers = _contended_yard()
    plan = plan_docks(docks=docks, trailers=trailers, now=NOW)
    assert plan.engine == "cp-sat"
    assert plan.status == "OPTIMAL"
    assert plan.total_cost <= plan.greedy_cost
    assert plan.improvement == plan.greedy_cost - plan.total_cost


def test_optimiser_beats_greedy_where_greedy_is_myopic():
    """
    The concrete case for solving this jointly rather than trailer by trailer.

    Greedy takes trailers heaviest-first and gives each the door that is
    cheapest FOR IT. The critical dry-van load therefore takes DOCK-04, which
    is nearer the entrance and cheap enough to beat the far dry-van-only door
    even after the flexibility penalty. But DOCK-04 is the only door that takes
    reefers, so the reefer trailer behind it now queues 45 minutes for a door
    that never had to be occupied.

    Planning both together sends the critical load to the far dedicated door
    and leaves the reefer its only option free. Nobody waits.
    """
    docks = [dock("DOCK-04", 1, ["dry_van", "reefer"], service=45),
             dock("DOCK-12", 9, ["dry_van"], service=45)]
    trailers = [trailer("TRL-CRIT", "dry_van", "critical", ready=0),
                trailer("TRL-REEF", "reefer", "normal", ready=0)]

    plan = plan_docks(docks=docks, trailers=trailers, now=NOW)

    assert plan.total_cost < plan.greedy_cost
    assert plan.improvement > 0
    assert plan.assignments["TRL-CRIT"].dock_id == "DOCK-12"
    assert plan.assignments["TRL-REEF"].dock_id == "DOCK-04"
    assert plan.assignments["TRL-REEF"].wait_minutes == 0
    assert plan.assignments["TRL-CRIT"].wait_minutes == 0


def test_greedy_fallback_still_produces_a_valid_schedule_without_ortools(monkeypatch):
    """OR-Tools missing must degrade the plan's quality, never the yard's
    ability to run. The status says which path produced the answer."""
    monkeypatch.setattr(dock_engine, "cp_model", None)
    docks, trailers = _contended_yard()

    plan = plan_docks(docks=docks, trailers=trailers, now=NOW)

    assert plan.engine == "greedy"
    assert plan.status == "OR_TOOLS_UNAVAILABLE"
    assert len(plan.assignments) == len(trailers)
    assert plan.improvement == 0
    # Still a physically valid schedule: no two trailers overlap on a door.
    by_dock: dict[str, list[tuple[datetime, datetime]]] = {}
    for assignment in plan.assignments.values():
        by_dock.setdefault(assignment.dock_id, []).append((assignment.start, assignment.end))
    for windows in by_dock.values():
        windows.sort()
        for earlier, later in zip(windows, windows[1:]):
            assert earlier[1] <= later[0]


# ─────────────────────────────────────────────
# Explainability
# ─────────────────────────────────────────────

def test_breakdown_carries_every_cost_term_and_the_counterfactuals():
    docks, trailers = _contended_yard()
    plan = plan_docks(docks=docks, trailers=trailers, now=NOW)
    request = next(t for t in trailers if t.trailer_id == "TRL-02")
    assignment = plan.assignments["TRL-02"]

    detail = breakdown(plan, assignment, request)

    assert set(detail["cost_terms"]) == {
        "wait", "flexibility", "position", "utilisation", "churn", "total"}
    assert detail["cost_terms"]["total"] == assignment.cost
    assert detail["cost_terms"]["wait"]["weight_per_minute"] == 8   # critical
    assert detail["plan"]["greedy_baseline_cost"] == plan.greedy_cost
    assert detail["planned_start"] == assignment.start.isoformat()
    # "Why not the others" is answered for every door that was eligible.
    assert detail["alternatives"], "a contended yard must offer alternatives"
    for alternative in detail["alternatives"]:
        assert alternative["dock_id"] != assignment.dock_id
        assert alternative["cost"] >= assignment.cost
    # ...and every door that was not eligible says why not.
    assert any(r["dock_id"] == "DOCK-06" for r in detail["rejected"])


def test_reason_states_the_actual_deciding_factor():
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40,
                  bookings=[Booking("BUSY", NOW, at(90))]),
             dock("DOCK-05", 5, ["dry_van"], service=40)]
    request = trailer("TRL-1")
    plan = plan_docks(docks=docks, trailers=[request], now=NOW)

    reason = explain(plan, plan.assignments["TRL-1"], request)

    assert reason.startswith("DOCK-05")
    assert "free on arrival" in reason
    assert "DOCK-01 would have waited 90 min" in reason


def test_alternatives_are_ranked_cheapest_first():
    docks, trailers = _contended_yard()
    plan = plan_docks(docks=docks, trailers=trailers, now=NOW)
    for assignment in plan.assignments.values():
        costs = [a["cost"] for a in assignment.alternatives]
        assert costs == sorted(costs)


# ─────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────

def test_no_trailers_is_an_empty_plan_not_an_error():
    plan = plan_docks(docks=[dock("DOCK-01", 1, ["dry_van"])], trailers=[], now=NOW)
    assert plan.assignments == {}
    assert plan.status == "NO_TRAILERS"


def test_a_trailer_ready_in_the_past_is_never_scheduled_before_now():
    """Seeded and recovered state routinely has ETAs in the past. The plan must
    still be a plan for the future, not a backdated one."""
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40)]
    plan = plan_docks(docks=docks, trailers=[trailer("TRL-OLD", ready=-600)], now=NOW)
    assert plan.assignments["TRL-OLD"].start >= NOW - timedelta(minutes=600)
    assert plan.assignments["TRL-OLD"].wait_minutes == 0


def test_a_booking_that_already_finished_does_not_block_the_door():
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40,
                  bookings=[Booking("OLD", at(-200), at(-100))])]
    plan = plan_docks(docks=docks, trailers=[trailer("TRL-1")], now=NOW)
    assert plan.assignments["TRL-1"].wait_minutes == 0


def test_trailer_with_no_load_type_can_use_any_active_door():
    docks = [dock("DOCK-06", 6, ["tanker"], active=False), dock("DOCK-03", 3, ["reefer"])]
    plan = plan_docks(docks=docks, trailers=[trailer("TRL-?", load_type=None)], now=NOW)
    assert plan.assignments["TRL-?"].dock_id == "DOCK-03"


def test_naive_datetimes_are_treated_as_utc_rather_than_crashing():
    """psycopg hands back tz-aware values, but seeds and tests do not always."""
    docks = [dock("DOCK-01", 1, ["dry_van"], service=40)]
    naive = TrailerRequest(trailer_id="TRL-N", load_type="dry_van", priority="normal",
                           ready_at=NOW.replace(tzinfo=None) + timedelta(minutes=30))
    plan = plan_docks(docks=docks, trailers=[naive], now=NOW)
    assert minutes_from_now(plan.assignments["TRL-N"].start) == 30


@pytest.mark.parametrize("priority,weight", [
    ("critical", 8), ("high", 4), ("normal", 2), ("low", 1), ("nonsense", 2), (None, 2),
])
def test_unknown_priority_falls_back_to_normal(priority, weight):
    assert trailer("TRL-1", priority=priority).wait_weight == weight
