"""
Shared event bus helper — used by every service (Yard API, Procurement API,
dock worker, match worker) so the dual-write pattern is never done ad hoc.
NO service inserts into event_log directly, issues its own XADD/XREADGROUP
calls, or manages its own consumer idempotency — everything here is the
only sanctioned path.

WRITE PATH — THE RULE THIS FILE ENFORCES:

    BEGIN (caller's transaction)
      domain table INSERT/UPDATE   <- caller does this
      record_event(...)            <- writes event_log, does NOT commit
    COMMIT                         <- caller commits BOTH together
    publish_to_redis(...)          <- only after commit succeeds

record_event() deliberately does not commit. If it did, and the caller's
domain-table write failed afterward, you could end up with an event_log
row for something that never actually happened in the domain tables.

READ PATH — THE RULE THIS FILE ENFORCES:

Idempotency state lives in PostgreSQL (`processed_events`), not Redis.
A Redis-side check-then-act (SISMEMBER then SADD) is not atomic and can
race under concurrent consumers; `INSERT ... ON CONFLICT DO NOTHING` on
(consumer_group, event_id) is. The claim insert and the handler's own
domain writes commit together, in one transaction — so a handler that
fails partway through never leaves a "processed" marker for work that
didn't actually happen. A Redis message is only ACKed after that
Postgres transaction commits.

Delivery to Redis is at-least-once, not exactly-once (see
reconcile_unpublished) — this is expected and handled by the above, not
something to eliminate.
"""

import json
import logging
import os

import redis

logger = logging.getLogger("event_bus")

STREAM_KEY = "events:supply-chain"
STUCK_MESSAGE_IDLE_MS = 30_000  # claim messages pending longer than this

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_pool = redis.ConnectionPool.from_url(REDIS_URL, decode_responses=True)
r = redis.Redis(connection_pool=_pool)


# ─────────────────────────────────────────────
# WRITE PATH
# ─────────────────────────────────────────────

def record_event(pg_conn, entity_type: str, entity_id: str, event_type: str, payload: dict):
    """
    Insert into event_log WITHIN THE CALLER'S EXISTING TRANSACTION.
    Does NOT commit — the caller commits this together with whatever
    domain-table change this event describes, as one atomic unit.

    Returns (event_id, created_at), meaningful only after the caller's
    commit succeeds — pass both to publish_to_redis() after that commit.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO event_log (entity_type, entity_id, event_type, payload)
            VALUES (%s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (entity_type, entity_id, event_type, json.dumps(payload)),
        )
        event_id, created_at = cur.fetchone()
    return event_id, created_at


def publish_to_redis(pg_conn, event_id: int, entity_type: str, entity_id: str,
                      event_type: str, payload: dict, created_at):
    """
    XADD to the stream, using the real event_log.created_at as the
    timestamp (one source of truth for when the event happened, not a
    separately-generated app-side clock). Call only after the caller has
    committed the transaction record_event() ran inside.

    Redis delivery failures are swallowed here and left for
    reconcile_unpublished() to retry later — that part of this function
    never raises past the caller. PostgreSQL failures (the UPDATE/commit
    below) are NOT swallowed and propagate normally; a failure to record
    redis_published = TRUE is a real problem the caller should see, not
    something to silently ignore.
    """
    try:
        r.xadd(
            STREAM_KEY,
            {
                "event_id": str(event_id),
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "timestamp": created_at.isoformat(),
                "payload": json.dumps(payload),
            },
        )
    except redis.RedisError:
        logger.warning("Redis publish failed for event_id=%s, will be picked up by reconcile_unpublished()", event_id)
        return

    with pg_conn.cursor() as cur:
        cur.execute("UPDATE event_log SET redis_published = TRUE WHERE id = %s", (event_id,))
    pg_conn.commit()


def publish_event(pg_conn, entity_type: str, entity_id: str, event_type: str, payload: dict):
    """
    Convenience wrapper for the rare case where this event IS the only
    write happening (no separate domain-table change in the same
    transaction). If you're also writing a domain table row for this
    event (the usual case), use record_event() inside your own
    transaction, commit both together, then call publish_to_redis()
    yourself — see the module docstring.
    """
    event_id, created_at = record_event(pg_conn, entity_type, entity_id, event_type, payload)
    pg_conn.commit()
    publish_to_redis(pg_conn, event_id, entity_type, entity_id, event_type, payload, created_at)
    return event_id


def reconcile_unpublished(pg_conn, limit: int = 100):
    """
    Finds committed events that never reached Redis (redis_published =
    FALSE) and republishes them. Run on a timer or service startup.
    Can cause the same event_id to appear as two separate stream
    entries — expected under at-least-once delivery, handled by the
    processed_events claim in consume(), not by anything here.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, entity_type, entity_id, event_type, payload, created_at
            FROM event_log
            WHERE redis_published = FALSE
            ORDER BY id
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    for event_id, entity_type, entity_id_val, event_type, payload, created_at in rows:
        publish_to_redis(pg_conn, event_id, entity_type, entity_id_val, event_type, payload, created_at)


# ─────────────────────────────────────────────
# READ PATH
# ─────────────────────────────────────────────

def consume(pg_conn, group: str, consumer_name: str, handler,
            allowed_event_types: set | None = None, block_ms: int = 5000):
    """
    Generic consumer-group loop.

    allowed_event_types: the fixed set this group actually cares about
        (e.g. dock-worker's {"SHIPMENT_CREATED", "TRAILER_DEPARTED",
        "ETA_UPDATED", "TRAILER_LOCATION_UPDATED"} — see
        redis-contract.md §5). Events outside this set are acked and
        skipped BEFORE touching Postgres at all — no processed_events
        row, no transaction, no wasted round-trip. Pass None only for a
        group that genuinely wants everything (dashboard-ws).

        This makes the "Reacts to" column in redis-contract.md §5 an
        enforced contract, not a comment a worker's handler has to
        remember to honor internally.

    handler signature: handler(pg_conn, fields) -> None
        `fields` is the parsed event dict (payload already json.loads'd).
        handler may perform its own domain-table writes using pg_conn.
        handler must NOT commit or rollback pg_conn itself — consume()
        owns the transaction boundary, committing the processed_events
        claim and the handler's writes together as one atomic unit.

    Three reliability behaviors, all enforced HERE:

    1. IDEMPOTENCY (Postgres-owned): INSERT INTO processed_events
       (consumer_group, event_id) ... ON CONFLICT DO NOTHING is the
       atomic claim. If it claims nothing, this event_id was already
       handled by this group — skip the handler, ack, move on.

    2. STUCK MESSAGES: XAUTOCLAIM runs each loop iteration to reclaim
       anything idle longer than STUCK_MESSAGE_IDLE_MS (a consumer that
       read a message and crashed before acking) and reprocess it.

    3. HANDLER FAILURE ISOLATION: a raised exception rolls back the
       Postgres transaction (undoing BOTH the claim and any partial
       domain writes together — so a retry starts clean, not half-done),
       leaves the message unacked for XAUTOCLAIM to retry, and does not
       take down the rest of the loop.
    """
    try:
        r.xgroup_create(STREAM_KEY, group, id="0", mkstream=True)
    except redis.ResponseError:
        pass  # group already exists

    def _handle_one(msg_id, fields):
        event_id = fields.get("event_id")
        event_type = fields.get("event_type")

        if allowed_event_types is not None and event_type not in allowed_event_types:
            r.xack(STREAM_KEY, group, msg_id)  # not this group's concern — ack, never touch Postgres
            return

        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO processed_events (consumer_group, event_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING event_id
                    """,
                    (group, event_id),
                )
                claimed = cur.fetchone() is not None

            if not claimed:
                pg_conn.commit()  # release the no-op transaction cleanly
                r.xack(STREAM_KEY, group, msg_id)
                return  # duplicate delivery, already handled by this group

            parsed = dict(fields)
            parsed["payload"] = json.loads(fields.get("payload", "{}"))
            handler(pg_conn, parsed)

            pg_conn.commit()  # claim + handler's domain writes commit together
        except Exception:
            pg_conn.rollback()  # undo the claim AND any partial domain writes
            logger.exception("handler failed for event_id=%s (group=%s) — rolled back, left unacked for retry", event_id, group)
            return  # do NOT ack; XAUTOCLAIM will retry it later

        r.xack(STREAM_KEY, group, msg_id)

    while True:
        # 1. Reclaim anything stuck from a crashed consumer
        try:
            _, claimed, _ = r.xautoclaim(STREAM_KEY, group, consumer_name, STUCK_MESSAGE_IDLE_MS, start_id="0-0")
            for msg_id, fields in claimed:
                _handle_one(msg_id, fields)
        except redis.ResponseError:
            pass  # group/stream not ready yet on first run

        # 2. Read new messages
        resp = r.xreadgroup(group, consumer_name, {STREAM_KEY: ">"}, count=10, block=block_ms)
        if not resp:
            continue
        for _, messages in resp:
            for msg_id, fields in messages:
                _handle_one(msg_id, fields)
