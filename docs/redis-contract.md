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
```

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
```

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
| `dock-worker` | Dock scoring worker | `{"SHIPMENT_CREATED", "TRAILER_DEPARTED", "ETA_UPDATED", "TRAILER_LOCATION_UPDATED"}` — `TRAILER_LOCATION_UPDATED` updates tracking only, never triggers re-scoring by itself; `ETA_UPDATED` re-scores only per the threshold in §9 |
| `match-worker` | 3-way match worker | `{"GOODS_RECEIVED", "INVOICE_RECEIVED"}` |
| `dashboard-ws` | Dashboard WebSocket layer | `None` (no filter — every event type is forwarded) |

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
| `SHIPMENT_CREATED` / `TRAILER_DEPARTED` | Initial dock scoring and assignment |
| `TRAILER_LOCATION_UPDATED` | Update tracking only. Never re-scores by itself. |
| `ETA_UPDATED` | Re-score only if the new ETA differs from the ETA last used for scoring by ≥10 minutes |

This threshold is application logic, not schema — documented here so
it isn't decided differently by whoever happens to write the worker.
