# Dock Decision Engine — LOCKED (v6)

Honest framing, stated up front. v1–v5 of this document described a **Tier-1
intelligent heuristic** and said plainly that it could not do dock scheduling,
because `schema.sql` had no service duration, no appointment windows and no
occupancy intervals. That was true then. v6 adds the two columns that were
missing (`dock_assignments.planned_start` / `planned_end`, schema v6, applied
by `migrations/v6_dock_scheduling.sql`) and uses the service duration that
already existed (`docks.metadata.expected_unload_minutes`, v4).

So the honest framing now is different, and narrower: this **is** dock
scheduling over time, solved to optimality for the pending set on every state
change. What it still is not, and does not claim: labour/crew scheduling,
carrier appointment negotiation, or yard-jockey movement planning. There is no
data for any of those, and none is faked.

## 1. What changed from v5, and why

| | v5 | v6 |
|---|---|---|
| Availability | "is this door occupied *right now*" | "is this door free for the window this truck needs" |
| ETA | not an input | drives when the trailer is ready, and therefore the whole plan |
| Waiting time | not computed | the dominant term in the objective, and a stored, reportable number |
| Scope of a decision | one trailer, scored alone | every pending trailer, planned jointly |
| Method | weighted score, pick the max | CP-SAT minimisation over an integer cost model |
| Order-dependence | outcome depended on event arrival order | outcome depends only on state |

The two v5 problems this fixes, both of which were visible on the board:

* a door reserved for a truck arriving in six hours blocked a truck arriving in
  twenty minutes, indefinitely — nothing in the model knew those were different
  moments in time;
* priority was a free-floating bonus, so a critical load could "win" a door and
  then sit in front of it for an hour, which is the opposite of what priority
  is supposed to buy.

## 2. Hard constraints (feasibility — reject, do not score)

A dock is a candidate for a trailer only if all of:

1. `docks.is_active = TRUE`
2. `trailer.load_type ∈ docks.compatible_load_types`
3. the trailer's service window does not overlap a window already committed on
   that door

Constraints 1 and 2 are properties of a door and are checked per candidate
(`feasible_docks()`). Constraint 3 is a property of a door *at a time* and is
therefore enforced by the scheduler as a `NoOverlap` per dock, not by
pre-filtering: a door that is busy when the trailer arrives is still a
candidate, it just costs the wait. That distinction is the whole point of v6.

**Immovable windows.** Two kinds of assignment are never re-planned and enter
the model as fixed intervals instead:

* a trailer physically at a door (`trailers.status = 'DOCKED'`). Its window
  runs from `docked_at`, and is extended to at least `now + 5 min` if it has
  overrun — a door being used past its plan is still not free.
* an operator override (`score_breakdown.source = 'manual_override'`, written
  by `POST /dock-assignments/{id}/reassign`). If the optimiser could undo a
  human decision on the next GPS tick, the override button would be a lie.

Overlapping committed windows on one door are **merged** into their union
before the model is built. A door cannot conflict with itself; handing an
overlap to `NoOverlap` as two intervals would return INFEASIBLE for the entire
yard because of one bad row. See `busy_intervals()`.

If a trailer has no feasible door at all (the tanker door is out of service and
nothing else takes tankers), no `dock_assignments` row is created. An `alerts`
row (`alert_type = 'DOCK_CONFLICT'`) is written and `ALERT_CREATED` emitted —
once, not on every re-plan, guarded by the existence of an unacknowledged
conflict alert for that trailer. The trailer is re-planned automatically as
soon as any state changes.

## 3. Objective (soft — rank the feasible)

Minimise, over all pending trailers jointly:

```
cost = wait_weight(priority) × wait_minutes
     + W_FLEX     × (len(compatible_load_types) − 1)
     + W_POSITION × yard_position
     + W_UTIL     × (minutes already committed on that door ÷ 15)
     + W_CHURN    × (1 if this moves a trailer off a door it was promised)
```

| Weight | Value | Why it exists |
|---|---|---|
| `wait_weight` | critical 8, high 4, normal 2, low 1 — **per minute** | Turnaround is what the yard pays for. Priority enters *through* waiting, so a critical load does not get a nicer door, it gets to not queue. 15 min of critical wait costs the same as 2 hours of low-priority wait. |
| `W_FLEX` | 6 per extra load type | Keeps multi-purpose doors free for whatever arrives next, so a scarce-fit trailer is not locked out later by a load that had options. |
| `W_POSITION` | 1 per slot | Travel toward the entrance, and a tie-break. `yard_position` is unique per door, so per-trailer dock costs are distinct and the ranking is stable. |
| `W_UTIL` | 1 per 15 committed minutes | Spreads load instead of stacking a queue behind one door, keeping spare capacity for the trailer whose ETA nobody knows yet. |
| `W_CHURN` | 90 | Plan stability. A re-plan only moves an already-promised trailer when the move is worth more than the disruption — 11 minutes of critical waiting, 45 of normal. |

`ready_at` — when a trailer can first take a door — is `now` for a trailer
already at the gate, and `max(eta, now)` for one still inbound. That is the
single point where ETA enters, and everything else follows from it.

## 4. Solver

CP-SAT (`ortools`), one model per re-plan:

* one integer start variable per trailer, in minutes from a common origin;
* one optional interval per (trailer, door) sharing that start, sized by that
  door's `expected_unload_minutes`;
* `AddExactlyOne` over each trailer's door literals;
* `AddNoOverlap` per door, including the immovable fixed intervals;
* the objective above, entirely in integers.

**Determinism is a requirement, not a hope.** Inputs are sorted by id before
the model is built, `num_search_workers = 1`, `random_seed = 0`, and the budget
is `max_deterministic_time` rather than a wall-clock limit — a wall-clock limit
would make the plan depend on how busy the machine is. The same inputs produce
the same plan on any machine, which is what makes the tests meaningful.

**Greedy is not a second engine.** The same cost function drives a
deterministic first-fit pass (heaviest wait weight first, then earliest ready,
then trailer id, filling gaps between existing windows). It is used for three
things: a solution hint that gives CP-SAT an immediate upper bound; the
fallback if OR-Tools is unavailable or the solve returns no solution (the yard
still gets a schedule, and `Plan.status` says which path produced it); and the
**baseline every plan is reported against**. `improvement_vs_greedy` on the
Yard & Dock screen is therefore a measured number, not a claim that optimising
helps.

## 5. Explainability

`dock_assignments.reason` is one true sentence, e.g.
`"DOCK-11: earliest workable slot, 33 min wait vs 71 min at DOCK-02
(priority=high, cost 96)"`. It names the actual deciding factor rather than
"highest score", and it is generated from the same numbers that made the
decision.

It carries **no wall-clock time**, deliberately. The window is a structured
field every client renders in the viewer's own timezone, so a server-side "at
14:05" baked into the prose would sit next to a UI reading 19:35 for the same
instant and look like a bug. Durations are timezone-free; the same rule applies
to alert messages.

`dock_assignments.score_breakdown` carries the arithmetic: every cost term with
its inputs, the planned window, the wait, the rejected doors with the reason
each was rejected, and **alternatives** — for every other feasible door, what
this trailer would have cost there given where the rest of the plan landed.
That is the counterfactual a yard supervisor actually asks for ("why not
D-07"), answered with the same arithmetic, not a separate narrative.

## 6. Triggers — when recommendations update

Every trigger runs the *same* function: read the whole yard from Postgres, plan
it, write back the difference. There is no incremental bookkeeping to drift out
of sync, and a worker that has been down re-derives the plan on startup.

| Event | Why it re-plans |
|---|---|
| `TRAILER_DEPARTED` | a trailer now exists to give a door to |
| `ETA_UPDATED` | the truck will be here at a different time (≥10 min, per redis-contract.md §9) |
| `TRAILER_ARRIVED` | it is here now: `ready_at` becomes now, and it competes differently |
| `TRAILER_DOCKED` | a door is now genuinely occupied, and that window is fixed |
| `GOODS_RECEIVED` | a door was released — the trailers queued behind it move up |
| `DOCK_REASSIGNED` | an operator overrode an assignment; everything else re-plans around it |
| `TRAILER_LOCATION_UPDATED` | **never** re-plans by itself (§9) — tracking only |
| `SHIPMENT_CREATED` | no-op; no trailer row exists yet |

A re-plan that confirms the existing door refreshes the stored window and emits
**nothing**. Silence is the correct output for "nothing changed" — an event per
GPS tick would drown the live rail.

This closes the limitation v5 documented honestly at the bottom of its own
file: a waiting trailer used to be re-scored only on its own next ETA update,
so a door freeing up elsewhere never reached it.

## 7. `DOCK_DELAYED`, now honestly emitted

v5 deliberately never emitted `DOCK_DELAYED`: with no window data, "this
trailer will miss its slot" was not computable, and faking it would have
contradicted the engine's own framing. The value stayed in the locked
vocabulary for if window data were ever added.

It has been. A planned wait of ≥45 minutes is now a derived fact: the trailer
is here (or will be), and the earliest door in the *optimal* plan is that far
out. That emits `DOCK_DELAYED` on the assignment plus an `alerts` row
(`alert_type = 'DELAY'`, `critical` at ≥2 hours), once, on crossing the
threshold — not on every re-plan that leaves the trailer still queued.

Distinct from it, and unchanged: a large **ETA slip** raises a `DELAY` alert on
the *trailer*. That is the truck being late; `DOCK_DELAYED` is the truck
queueing for a door. Both can happen to the same trailer for different reasons.

## 8. Still out of scope for v6 (stated, not silently skipped)

* **No preemption.** A newly-arriving critical trailer never bumps a trailer
  already at a door. It competes for every *future* window, which is where the
  priority weight does its work, and where bumping would be theatre anyway.
* **No carrier appointment windows.** A trailer is ready when it arrives; there
  is no booked slot it can be early or late for, because no such data exists.
* **No labour or equipment constraints.** Service duration is a property of the
  door, not of a crew roster that is not modelled.
* **No multi-yard or trailer-repositioning moves.** One yard, one hop.

## 9. Implementation

| Concern | Where |
|---|---|
| Feasibility, cost model, CP-SAT, greedy, explanations | `backend/shared/dock_engine.py` (pure functions, no database) |
| Read state → plan → write the difference → emit | `backend/services/dock_worker/main.py` |
| Manual override (pinned, window computed) | Yard API `POST /dock-assignments/{id}/reassign` |
| Door timeline and utilisation read | Yard API `GET /dock-schedule` |
| Tests | `backend/tests/test_dock_engine.py` (planner), `backend/tests/test_dock_scheduling_live.py` (real Postgres + real HTTP) |
