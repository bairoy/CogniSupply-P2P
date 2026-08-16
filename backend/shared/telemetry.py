"""
Folding GPS telemetry out of event timelines.

`event_log` holds two different kinds of record under one shape:

* **Facts** -- discrete things a human audits: departed, docked, received,
  matched, paid. Each one earns a line on a timeline.
* **Telemetry** -- ``TRAILER_LOCATION_UPDATED``, a sensor sampling a continuous
  quantity every few seconds.

Measured on the seeded database, 7,851 of 10,019 event_log rows (78%) are
position pings, and a single PO's audit trail is ~692 events of which ~660 are
pings. Worse, that number GROWS for as long as the truck drives while the
information it carries stays constant -- an unbounded response for a fixed
amount of meaning, on endpoints including the public, unauthenticated tracker.

Position belongs on the map, where 660 points are a smooth line. It is not read
from here: the map draws `tracking_events` (served as `breadcrumbs`, still
complete and untouched), and nothing consumes TRAILER_LOCATION_UPDATED for data
-- dock-worker acks it without acting (redis-contract.md §9).

So the timelines fold it and every caller keeps the escape hatch. This module
is the single implementation, imported by both dashboard_gateway and
yard_api.outbound, so the two cannot drift into different ideas of what a
collapsed row looks like.

Nothing here writes, and nothing here decides anything: it is a rendering of
rows already fetched.
"""

from fastapi import HTTPException

# Deliberately just the one event type. ETA_UPDATED looks similar but is a
# fact -- Yard API only emits it when the arrival moved by 10 minutes or more
# (api-contract.md), which is a change somebody planning a door cares about.
TELEMETRY_EVENT_TYPES = {"TRAILER_LOCATION_UPDATED"}

TELEMETRY_MODES = ("collapsed", "full")


def telemetry_mode(value: str) -> str:
    """Validate the `?telemetry=` query parameter. 400 on anything else."""
    if value not in TELEMETRY_MODES:
        raise HTTPException(
            400, f"telemetry must be one of {', '.join(TELEMETRY_MODES)}"
        )
    return value


def collapse_telemetry(rows: list[dict], *, time_key: str = "at") -> list[dict]:
    """
    Fold each RUN of consecutive telemetry rows into a single summary row.

    Runs, not all-of-a-type: if a truck pings, gets delayed, then pings again,
    that is two separate stretches of driving either side of an event, and
    flattening them into one row would misrepresent the order things happened
    in. A run of exactly one is left alone -- "1 position update" collapsed is
    strictly worse than the row it replaces. Runs are grouped by `entity_id`
    where the rows carry one, so two trailers pinging into a single timeline
    never merge however they interleave.

    `rows` are dicts as the callers build them, already ordered oldest-first by
    SQL. `time_key` is whichever key holds the timestamp ("at" in the gateway,
    "timestamp" in outbound) -- the summary row reports its span under `from`
    and `to` regardless.

    The collapsed row keeps the first row's identity fields and adds
    `collapsed`, `count`, `from`, `to`. `payload` is nulled if present: the
    individual lat/lng samples live in `tracking_events`, and a caller who
    wants the raw rows asks for `telemetry=full`.
    """
    out: list[dict] = []
    run: list[dict] = []

    def flush():
        if not run:
            return
        if len(run) == 1:
            out.append(run[0])
        else:
            first, last = run[0], run[-1]
            collapsed = dict(first)
            collapsed["summary"] = f"{len(run)} position updates"
            collapsed["collapsed"] = True
            collapsed["count"] = len(run)
            collapsed["from"] = first.get(time_key)
            collapsed["to"] = last.get(time_key)
            if "payload" in collapsed:
                collapsed["payload"] = None
            out.append(collapsed)
        run.clear()

    for row in rows:
        telemetry = row.get("event_type") in TELEMETRY_EVENT_TYPES
        same_run = (
            run and telemetry and row.get("entity_id") == run[-1].get("entity_id")
        )
        if same_run:
            run.append(row)
            continue
        flush()
        (run if telemetry else out).append(row)
    flush()
    return out
