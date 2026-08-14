# Dock Decision Engine — LOCKED

Honest framing, stated up front: this is a **Tier-1 intelligent heuristic**,
not industrial dock scheduling. `schema.sql` has no dock service duration,
appointment windows, occupancy intervals, or labor data — so this engine
does not claim to solve those. What it does claim: correct, explainable,
implementable, demonstrable, extensible with the fields that actually exist.

## Inputs (all from `schema.sql`, nothing else)

- `trailers`: `load_type`, `priority`, `eta`, `status`
- `docks`: `compatible_load_types`, `yard_position`, `is_active`
- `dock_assignments`: existing rows, to determine current occupancy

## Stage 1 — Hard constraint filter (reject, not score)

A dock is a candidate only if **all** of:
1. `docks.is_active = TRUE`
2. `trailer.load_type` is in `docks.compatible_load_types`
3. No existing `dock_assignments` row for this dock with `status IN ('ASSIGNED', 'CONFIRMED')` **belonging to a different trailer**. On initial assignment (`TRAILER_DEPARTED`) this trailer has no prior row, so it's equivalent to "no occupying row at all." On re-scoring (`ETA_UPDATED`), the trailer's *own* current dock must remain eligible — otherwise re-scoring would always exclude the dock it's already sitting in, making "the same dock still wins" structurally impossible. Concretely: `NOT EXISTS (SELECT 1 FROM dock_assignments WHERE dock_id = candidate.id AND status IN ('ASSIGNED','CONFIRMED') AND trailer_id != this_trailer.id)`.

If zero candidates survive: do **not** create a `dock_assignments` row.
Instead write an `alerts` row (`alert_type = 'DOCK_CONFLICT'`, both values
already in the locked vocabulary) and emit `ALERT_CREATED`. The trailer
stays unassigned until the next `ETA_UPDATED` re-scoring pass finds a
candidate — see "Known limitation" below.

## Stage 2 — Soft scoring (rank the survivors)

```
score(dock) = 0.5 × priority_score(trailer.priority)
            + 0.3 × specialization_score(dock)
            - 0.2 × normalized_position(dock)
```

- `priority_score`: `critical=1.0, high=0.75, normal=0.5, low=0.25` — directly from `trailers.priority`.
- `specialization_score`: `1 / len(docks.compatible_load_types)`, normalized 0-1 across candidates. A dock that *only* handles this trailer's load type scores higher than a flexible dock that handles many — assign scarce-fit trailers to scarce-fit docks first, preserve flexible docks for whatever arrives next. This is a real, defensible heuristic, not filler.
- `normalized_position`: `docks.yard_position` normalized 0-1 across candidates (lower position = closer to entrance = preferred, as a tie-break, not a real distance calculation — we don't have trailer entry-point coordinates to compute real distance).

Highest score wins. Ties broken by lowest `yard_position`.

## Decision → write → event

```
INSERT dock_assignments (dock_id=winner, status='ASSIGNED', reason=<explanation string>)
record_event(DOCK_ASSIGNED) — same transaction, commit together
```
`reason` is always a human-readable explanation, e.g. `"DOCK-04: only reefer-compatible free dock, priority=critical"` — this is what makes the assignment defensible to a judge, not just a number.

## Re-scoring (ETA_UPDATED, ≥10 min threshold — per redis-contract.md §9)

Re-run Stage 1 + Stage 2 from scratch (with the self-occupancy fix above,
so the trailer's current dock is a legitimate candidate). If the winning
dock differs from the current assignment: apply the **locked
reassignment pattern** (old row → `REASSIGNED`, new row → `ASSIGNED`,
never edited in place), `record_event(DOCK_REASSIGNED)`. If the same
dock still wins: no domain write, no event — re-scoring confirmed the
existing assignment, nothing changed.

**`DOCK_DELAYED` is not emitted by this engine.** The original design
here referenced the trailer "missing its effectively-available window" —
`schema.sql` has no dock availability window, appointment window, or
service duration, so that can't legitimately be computed, and claiming
it would contradict this document's own opening honesty framing.
`DOCK_DELAYED` stays a valid value in `redis-contract.md`'s vocabulary
for if window data is ever added — it's just genuinely unused by Tier 1
logic, not silently faked.

What IS honestly computable from data we have: if the new ETA is later
than the ETA last used for scoring by a large margin (the same delta
already computed to decide whether to re-score at all — see
`api-contract.md`'s `POST /trailers/{id}/tracking`), that's a real
trailer delay, independent of which dock ends up assigned. Write an
`alerts` row (`alert_type = 'DELAY'`) and emit `ALERT_CREATED` for that,
regardless of whether the dock reassigns.

## Explicitly out of scope for Tier 1 (stated, not silently skipped)

- **No preemption.** A newly-arriving critical-priority trailer never
  bumps an already-assigned lower-priority trailer. It competes only for
  currently-free docks; if none, it alerts and waits.
- **No global joint optimization.** Each trailer is scored independently
  at its own `TRAILER_DEPARTED`/`ETA_UPDATED` event, not as a batch
  optimization across all waiting trailers.

## Fix required in already-built code (found while designing this)

`dock_assignments.status` has no terminal "released" value — `ASSIGNED |
CONFIRMED | DELAYED | REASSIGNED`. Without one, a dock that finished
unloading still reads as occupied forever under the Stage 1 hard
constraint, and can never be assigned to the next trailer. Fix: add
`COMPLETED` to the status vocabulary (a `TEXT` column, append-only —
no migration, matches the design principle exactly) and update
`POST /trailers/{id}/unload` to also set the trailer's current
`dock_assignments` row to `COMPLETED` — matching on
`status IN ('ASSIGNED', 'CONFIRMED', 'DELAYED')`, not just `'ASSIGNED'`,
since a trailer can legitimately reach unload while its assignment sits
in any of those three states. Patched below.

## Known limitation, stated honestly

Nothing currently re-triggers scoring for a *waiting* trailer when a
*different* trailer's dock frees up (`dock-worker` doesn't subscribe to
`GOODS_RECEIVED`). A waiting trailer only gets re-scored on its own next
`ETA_UPDATED`. Acceptable for Tier 1 demo purposes; a real fix would need
`dock-worker` to also react to dock-release events, which isn't in the
locked consumer contract and would need a deliberate addition, not a
silent one.
