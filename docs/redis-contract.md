# Redis Contract — LOCKED (v1)

This is the Redis-side counterpart to `schema.sql`. Same rule applies:
fields/values only ever get ADDED after this is written, never renamed
or removed.

Redis is used for exactly one thing in this system: **live event
delivery on top of the Postgres `event_log` table.** Nothing else lives
in Redis unless explicitly added to this document first.

---

## 1. Stream

| Property | Value |
|---|---|
| Key | `events:supply-chain` |
| Type | Redis Stream (`XADD` / `XREADGROUP`) |
| Count | Exactly one stream. No per-domain streams. |

## 2. Event field contract

Every entry in the stream has exactly these fields, every time. No entry
may omit a field or add an undeclared one without updating this table.

| Field | Type | Example |
|---|---|---|
| `event_id` | string form of `event_log.id` (the canonical Postgres serial ID — not a separately generated UUID, so the same event has one identity in both stores, enabling retry/idempotency) | `10291` |
| `event_type` | string, from vocabulary in §3 | `DOCK_ASSIGNED` |
| `entity_type` | string, from vocabulary in §4 | `trailer` |
| `entity_id` | string, matches a Postgres primary key | `TRL-3391` |
| `timestamp` | ISO-8601 UTC string | `2026-08-12T14:20:05Z` |
| `payload` | JSON string (object) | `{"dock_id":"DOCK-04"}` |

`event_id`, `entity_type`, `entity_id`, `event_type` are duplicated
exactly as written to the `event_log` Postgres row for the same event —
the two must always agree, since the Postgres row is the source of truth
and the stream entry is just its live-delivery copy.

## 2a. Payload convention (v4, additive)

The envelope in §2 is unchanged and still fixed. In addition, every payload
SHOULD carry:

- `summary` — a one-line human-readable string (e.g. `"TRL-3391 assigned to
  DOCK-04"`). This is what the dashboard's live event rail renders.
- the display fields a client needs to apply the event as a delta without
  refetching (ids, status, amounts).

This is a floor, not a schema: §6's "no locked payload schema per event type"
still holds, and adding a field to one event's payload still requires no
change to this document. It exists because before v4 most events published
`payload = {}` — `SHIPMENT_CREATED` did not even carry `po_id` — which made
the WebSocket useless as a delta channel.

## 3. `event_type` vocabulary (append-only)

Fixed list. A service may only ever emit a value from this list. Adding
a new event type = adding a new line here, never renaming an existing one.

```
REQUISITION_CREATED
SUPPLIER_RECOMMENDED
PO_CREATED
SHIPMENT_CREATED
TRAILER_DEPARTED
TRAILER_LOCATION_UPDATED
ETA_UPDATED
DOCK_ASSIGNED
DOCK_REASSIGNED
DOCK_DELAYED
TRAILER_ARRIVED
GOODS_RECEIVED
INVOICE_RECEIVED
OCR_COMPLETED
MATCH_COMPLETED
EXCEPTION_CREATED
EXCEPTION_RESOLVED
PAYMENT_APPROVED
ALERT_CREATED
TRAILER_DOCKED
PO_STATUS_CHANGED
ALERT_ACKNOWLEDGED
EXCEPTION_ASSIGNED
PAYMENT_PAID
TRAILER_EXITED
PO_CONFIRMED
OUTBOUND_ORDER_CREATED
LOAD_PLAN_CREATED
LOAD_STAGED
GOODS_ISSUED
OUTBOUND_DELIVERED
```

**v7 appended the last six.** Five are the outbound half of the yard; one
(`PO_CONFIRMED`) closes a gap on the inbound side. Nothing was renamed.

| Event | entity_type | Why it exists |
|---|---|---|
| `PO_CONFIRMED` | `purchase_order` | The supplier accepting the PO was a step the workflow described and the system never recorded — a PO went from `CREATED` straight to a shipment appearing, with the supplier's acceptance nowhere in the timeline. It is also the trigger the `supplier-agent` group needs a *name* for: without it, "PO exists" and "supplier committed to it" are the same event, and nothing can react to the second. |
| `OUTBOUND_ORDER_CREATED` | `outbound_order` | A customer order enters the yard's world. The inbound equivalent is `PO_CREATED`. |
| `LOAD_PLAN_CREATED` | `outbound_order` | Pick lines written. Deliberately carries the **order's** id, not a `load_plans` row id: a load plan is many rows and the event is about the set, so an event per line would be noise the dashboard has to re-aggregate. Hence there is no `load_plan` entity_type — see §4. |
| `LOAD_STAGED` | `outbound_order` | Goods picked to the staging lane. This is the outbound readiness signal — a truck should not be given a door for a load that is not picked yet, which is a constraint the inbound side simply does not have. |
| `GOODS_ISSUED` | `goods_issue` | Loading complete and the door released. The exact mirror of `GOODS_RECEIVED`, and like it, it is a **dock-release signal** — which is why `dock-worker` subscribes to it (§5). |
| `OUTBOUND_DELIVERED` | `outbound_order` | Confirmed at the customer. The outbound story's terminal state; there is no match/pay leg behind it, because nobody invoices us for goods we shipped out. |

Two things v7 deliberately did **not** add:

- **No outbound-specific trailer events.** An outbound truck emits
  `TRAILER_DEPARTED`, `TRAILER_ARRIVED`, `TRAILER_DOCKED`, `DOCK_ASSIGNED` and
  `TRAILER_EXITED` — the same five an inbound truck emits, with
  `payload.direction` telling them apart. Driving to a gate, queueing, and
  taking a door is one movement whichever way the pallets travel; forking the
  vocabulary would have forked every consumer that watches trailers.
- **No outbound match/exception events.** See `GOODS_ISSUED` above.

`TRAILER_EXITED` was appended in v6, for the outbound leg (`trailers.status =
'DEPARTED'`). `TRAILER_DEPARTED` already means "left the supplier" and could
not be reused for "left our yard" without making both ambiguous — the two are
opposite ends of the same journey. Emitted by Yard API's
`POST /trailers/{id}/depart`, entity_type `trailer`.

The five before it were appended in v4 (approved in `BUILD_PLAN.md` §2.2).
Nothing was renamed or removed. Why each exists:

| Event | Why it was needed |
|---|---|
| `TRAILER_DOCKED` | Nothing previously wrote `trailers.status='DOCKED'` or moved an assignment to `CONFIRMED`, so those states were unreachable despite being in the schema vocabulary. |
| `PO_STATUS_CHANGED` | `purchase_orders.status` never advanced past `CREATED`, which made the pipeline funnel uncomputable. Emitted by match-worker, the PR2-side status reconciler. |
| `ALERT_ACKNOWLEDGED` | `alerts.acknowledged` existed and nothing ever wrote it. |
| `EXCEPTION_ASSIGNED` | `exceptions.assigned_to` existed and nothing ever wrote it. |
| `PAYMENT_PAID` | `payments.paid_at` / the `PAID` status existed with no transition into them, so the P2P loop never closed. |

`TRAILER_DEPARTED` fires once, at `trailers` row creation (a trailer's
default status is `EN_ROUTE` — there is no separate `CREATED` state to
depart *from*, so trailer creation *is* the departure moment). No new
`trailers.status` value needed for this — it's a pure event, not a
status transition.

## 4. `entity_type` vocabulary (append-only)

```
requisition
purchase_order
shipment
trailer
dock_assignment
goods_receipt
invoice
match_result
exception
payment
alert
outbound_order
goods_issue
```

v7 appended the last two. `outbound_order` is the outbound counterpart of
`purchase_order`; `goods_issue` of `goods_receipt`.

Note there is **no `load_plan` entity_type**, for the same reason there is no
`dock` one: a load plan is a *set* of pick lines belonging to an outbound
order, not an independently addressable thing anyone tracks. `LOAD_PLAN_CREATED`
and `LOAD_STAGED` therefore both carry `entity_type = outbound_order` with the
per-line detail in `payload`. Do not add `load_plan` later — an event per pick
line is traffic no consumer wants, and the moment the vocabulary allows it
something will emit it.

Note: there is no `dock` entity_type, deliberately. Any event about a
dock — including `DOCK_ASSIGNED`, `DOCK_REASSIGNED`, `DOCK_DELAYED` —
uses `entity_type = dock_assignment` and `entity_id` = the
`dock_assignments.id` row, with the specific `dock_id` inside `payload`
if needed. Do not invent `entity_type = dock` later; a dock (the
physical door) and a dock assignment (the event-worthy relationship
between a trailer and a door) are different things, and only the
latter is a first-class entity in this contract.

## 5. Consumer groups (fixed set)

| Group name | Owned by | `allowed_event_types` passed to `consume()` |
|---|---|---|
| `dock-worker` | Dock scheduling worker | `{"SHIPMENT_CREATED", "TRAILER_DEPARTED", "ETA_UPDATED", "TRAILER_LOCATION_UPDATED", "TRAILER_ARRIVED", "TRAILER_DOCKED", "GOODS_RECEIVED", "DOCK_REASSIGNED"}` — `TRAILER_LOCATION_UPDATED` updates tracking only and never re-plans by itself; `ETA_UPDATED` re-plans only per the threshold in §9; `GOODS_RECEIVED` (v4) is the dock-release signal. **v6 added `TRAILER_ARRIVED`** (the trailer is ready *now*, not at its ETA), **`TRAILER_DOCKED`** (that window is now immovable) and **`DOCK_REASSIGNED`** (an operator override, which everything else must be planned around). The worker emits `DOCK_REASSIGNED` too, so the payload carries `source` and the worker ignores its own — see its module docstring on why that cannot loop |
| `match-worker` | 3-way match worker | `{"GOODS_RECEIVED", "INVOICE_RECEIVED", "SHIPMENT_CREATED"}` — **`SHIPMENT_CREATED` (v4)** carries no match work; it exists so PR2 (which owns `purchase_orders`) can advance the PO to `SHIPPED` without E2 writing a PR2 table |
| `supplier-agent` | Supplier agent worker (v7) | `{"PO_CREATED"}` — the autonomous PR2→E2 bridge. On a new PO it decides whether the supplier accepts, emits `PO_CONFIRMED`, and calls Yard API to create the shipment and trailer. It is a **consumer, never a direct writer** of E2 tables: it drives the same public `POST /shipments` and `POST /shipments/{id}/trailers` endpoints an operator would, so the ownership rule (only Yard API writes E2 tables) survives contact with automation |
| `dashboard-ws` | Dashboard WebSocket layer | `None` (no filter — every event type is forwarded) |

**v8 — `dashboard-ws` feeds TWO WebSocket rails, and remains ONE group.**
The gateway now serves `/ws/dashboard` (token-gated, every event, for signed-in
staff) and `/ws/track/{ref}` (public, one consignment, for the customer
tracker). Both are fed from the same `dashboard-ws` consumer group: the pump
hands each message to both fan-outs, and filtering is per-client. A second
consumer group for the tracker would be a second `processed_events` claim on
every event for no gain, so **no group was added** — the fixed set above is
unchanged. The public rail additionally filters to one trailer (matched on
`entity_id` for `entity_type = trailer`, and on `payload.trailer_id` otherwise,
since `GOODS_RECEIVED` is a `goods_receipt` and `DOCK_ASSIGNED` a
`dock_assignment` — §4) and to an 11-type customer vocabulary. No event type
was added to §3 for any of this.

**v7 — `dock-worker` gains `GOODS_ISSUED`.** It is the outbound dock-release
signal, exactly as `GOODS_RECEIVED` is the inbound one: a door frees the moment
the load is on the truck, and trailers queued behind it — in *either* direction
— should move up immediately rather than at the next unrelated event. Its full
set is therefore `{"SHIPMENT_CREATED", "TRAILER_DEPARTED", "ETA_UPDATED",
"TRAILER_LOCATION_UPDATED", "TRAILER_ARRIVED", "TRAILER_DOCKED",
"GOODS_RECEIVED", "GOODS_ISSUED", "DOCK_REASSIGNED"}`. No other outbound event
enters the set, because outbound trailers reuse the inbound trailer events
(§3) and the worker already subscribes to all of them.

`match-worker` is **unchanged by v7**. Outbound has no invoice, no 3-way match
and no payment, so subscribing it to any outbound event would be adding a
consumer with nothing to do.

No service creates an ad hoc consumer group outside this list. If a new
service needs its own group, it's added here first — with its filter
set defined at the same time, not left implicit.

Filtering happens inside `consume()`, before the `processed_events`
claim — an event type outside a group's set is acked and dropped
immediately, without a Postgres round-trip. This makes the table above
an enforced contract, not documentation a handler has to remember to
honor on its own.

## 6. What is explicitly NOT in Redis (Tier 1)

- No `trailer:{id}:state` / `dock:{id}:state` key-value cache. Current
  state is read from Postgres directly (`trailers`, `dock_assignments`,
  etc.) — the tables are small enough that this is fast without a cache.
- No session storage.
- No Redis Pub/Sub (Streams only — see prior discussion on why: Streams
  give consumer groups and replay, Pub/Sub doesn't).
- No TTLs/expiry on stream entries in Tier 1 — the stream is trimmed
  manually or left to grow for the hackathon's short lifetime, not
  auto-expired. `XTRIM` with a max length is the only Tier 2 addition
  worth considering if the stream gets large during testing.
- No locked payload *schema* per event type. The envelope (§2) is fixed;
  `payload` contents stay a flexible JSON object by design, so adding a
  field to one event's payload never requires touching this contract.

If a state cache turns out to be genuinely needed (dashboard feels slow
under real load), it gets added here as `trailer:{id}:state` /
`dock:{id}:state` — JSON blobs, no TTL, overwritten on every update —
before it gets written into any service.

## 7. Write ordering contract (non-negotiable)

For every event, in every service, without exception:

1. Write the row to Postgres `event_log` (and whatever domain table the
   event describes) — commit. `redis_published` defaults to `FALSE`.
2. `XADD` the same event to `events:supply-chain`, using `event_log.id`
   as `event_id`.
3. On successful `XADD`, flip `event_log.redis_published` to `TRUE`.

Step 2/3 failing must never roll back step 1, and step 1 must never wait
on step 2 succeeding.

There are exactly three sanctioned ways an event enters this system —
no service calls `XADD` directly, ever:

- **Domain event** (the normal case — this event accompanies a
  domain-table write): `record_event()` inside the caller's existing
  transaction, commit both together, then `publish_to_redis()`.
- **Pure event** (rare — no accompanying domain-table write):
  `publish_event()`, which wraps the same record-then-commit-then-publish
  sequence for the single-write case.
- **Recovery**: `reconcile_unpublished()`, which finds rows still
  `redis_published = FALSE` and calls `publish_to_redis()` on each —
  this is the concrete mechanism, not just a claim, for "Redis going
  down never loses data."

## 8. Consumer idempotency (mandatory)

`reconcile_unpublished()` can cause the same event to be `XADD`'d twice
as two separate stream entries — e.g. the app crashes after a successful
`XADD` but before `redis_published` flips to `TRUE`, and the reconciler
resends it on the next pass. This is expected and correct under an
at-least-once delivery model — it is not something to eliminate, only
something every consumer must handle.

**Idempotency state is owned by PostgreSQL, not Redis.** A `processed_events`
table (`consumer_group`, `event_id`) with that pair as primary key is
the source of truth for "has this consumer group already handled this
event" — a Redis-side check-then-act (e.g. `SISMEMBER` then `SADD`) is
not atomic and can race under concurrent consumers; `INSERT ... ON
CONFLICT DO NOTHING` on the Postgres primary key is. Redis stays purely
a live-delivery mechanism; it holds no authoritative consumer state.

The claim insert and the handler's own domain writes commit **together,
in the same transaction** — so a handler that fails partway through
never leaves a "processed" marker for work that didn't actually happen.
A Redis message is ACKed only after that Postgres transaction commits
successfully. See `consume()` in `event_bus.py` for the implementation;
no worker handler manages its own idempotency or its own commit.

## 9. Dock re-optimization threshold

`dock-worker` does not re-score on every `TRAILER_LOCATION_UPDATED` —
that would thrash the dock assignment on every GPS tick. The rule:

| Trigger | Action |
|---|---|
| `SHIPMENT_CREATED` | No-op — no trailer row exists yet |
| `TRAILER_DEPARTED` | Plan the yard; the new trailer gets a door and a window |
| `TRAILER_LOCATION_UPDATED` | Update tracking only. Never re-plans by itself. |
| `ETA_UPDATED` | Re-plan only if the new ETA differs from the ETA last used for planning by ≥10 minutes |
| `TRAILER_ARRIVED` / `TRAILER_DOCKED` / `GOODS_RECEIVED` / `DOCK_REASSIGNED` | Re-plan (v6) — each changes which doors are free when |
| `GOODS_ISSUED` | Re-plan (v7) — an outbound load is on the truck and that door is free |

This threshold is application logic, not schema — documented here so
it isn't decided differently by whoever happens to write the worker.

v6 note: "re-plan" means re-deriving the schedule for **every** pending
trailer from committed state, not re-scoring the one trailer the event names.
A re-plan that changes nothing writes nothing and emits nothing, so the extra
triggers above do not add event traffic — they only make the recommendation
correct sooner. See `docs/DOCK_DECISION_ENGINE.md` §6.
