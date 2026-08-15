"""
Supplier agent (v7). Background consume() loop, no HTTP surface.

Consumer group: supplier-agent
allowed_event_types: PO_CREATED   (exactly redis-contract.md §5)

WHAT THIS CLOSES

The end-to-end workflow has always described a step the code did not have:

    PO created -> SUPPLIER CONFIRMS -> shipment exists -> truck rolls

The middle two were fiction. A PO sat at CREATED forever, and shipments came
into being because a human called POST /shipments or because the seed script
fabricated one. This worker is the missing supplier: it reacts to a new PO,
decides whether the supplier accepts it, and if so brings the shipment and its
trailer into existence -- at which point the yard half of the system takes over
on its own, because dock-worker is already listening for TRAILER_DEPARTED.

That makes the whole chain from a typed sentence to a payment genuinely
event-driven, with no human in the middle of the happy path.

IT DRIVES HTTP, IT DOES NOT WRITE TABLES

This is the design rule that matters most here, and it is deliberate.

The obvious implementation is to INSERT into shipments and trailers directly --
this worker already holds a database connection, and it would be five lines.
It is also exactly the back door the ownership rules exist to prevent:
`shipments` and `trailers` are Yard API's, and "PR2 never writes E2 tables"
stops being true the moment a PR2-side worker does it because it was
convenient. So the agent calls the same public POST /shipments and
POST /shipments/{id}/trailers endpoints an operator would, with a bearer token,
over HTTP. Every validation, every guard, every event those endpoints emit
happens identically whether a human or this process triggered it.

The cost is a network hop and a service token. The benefit is that automation
cannot drift away from the contract the rest of the system is tested against --
if the agent can drive it, an operator can, and vice versa.

DETERMINISM

Whether a supplier accepts is decided by hashing the PO id against that
supplier's seeded reliability_score -- not random(). A demo that replays the
same seed must make the same decisions, or "why did it decline THAT one" has no
answer. Same reasoning as the dock engine's fixed solver seed.

Run:  ./.venv/bin/python backend/services/supplier_agent/main.py
"""

import hashlib
import logging
import os
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

import httpx  # noqa: E402

from event_bus import consume, reconcile_unpublished, record_event  # noqa: E402
from shared.auth import ROLE_ADMIN, issue_token  # noqa: E402
from shared.db import get_conn  # noqa: E402
from shared.ids import next_id  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  supplier-agent  %(levelname)-7s %(message)s",
)
log = logging.getLogger("supplier-agent")

GROUP = "supplier-agent"
ALLOWED = {"PO_CREATED"}

YARD_API = os.environ.get("YARD_API_URL", "http://127.0.0.1:8001")
PROCUREMENT_API = os.environ.get("PROCUREMENT_API_URL", "http://127.0.0.1:8002")
HTTP_TIMEOUT = 10.0

# The service identity the agent acts as. USR-000 is the schema's service
# account, which is precisely what "a touchless action" should be attributed to.
# It is minted a token here rather than given a password because the system
# account cannot sign in by design (shared/auth.py) -- it has no credentials to
# present, only an identity to act under.
SERVICE_USER_ID = "USR-000"
SERVICE_USER_NAME = "supplier-agent"

# How long after confirmation the supplier's truck is expected at our gate, when
# the PO carries no expected_delivery to work from.
DEFAULT_LEAD_HOURS = 6

RECONCILE_INTERVAL_SECONDS = 1.0

# Same publishing rule as the other workers: handlers only record_event(), and a
# background thread runs event_bus's own reconcile_unpublished(). A handler must
# not publish inline, because at handler time its event_log row is not committed
# and publishing something that might roll back is what the write-ordering
# contract forbids.


def _reconciler_loop():
    while True:
        try:
            with get_conn() as conn:
                reconcile_unpublished(conn, limit=200)
                conn.commit()
        except Exception:
            log.exception("reconciler pass failed; retrying")
        time.sleep(RECONCILE_INTERVAL_SECONDS)


def _service_token() -> str:
    """
    A short-lived admin-role token for USR-000.

    Admin because the agent has to call across BOTH domains -- procurement:write
    to confirm the PO and yard:write to raise the shipment -- and no single
    human role spans them, correctly: the segregation of duties in the matrix is
    about people. Re-minted per call rather than cached so a long-running worker
    never fails on an expired token.
    """
    token, _ = issue_token(SERVICE_USER_ID, SERVICE_USER_NAME, ROLE_ADMIN)
    return token


def _accepts(po_id: str, reliability: float) -> bool:
    """
    Deterministic accept/decline.

    A stable hash of the PO id maps to [0,1) and is compared against the
    supplier's seeded reliability. Same PO, same supplier, same answer, on every
    machine and every replay -- which is what makes a scripted demo honest and
    the eval harness meaningful. random() would give a nicer-looking spread and
    a system nobody can reason about twice.
    """
    digest = hashlib.sha256(po_id.encode()).digest()
    roll = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return roll < float(reliability)


def _post(path: str, base: str, json_body: dict | None = None) -> dict:
    resp = httpx.post(
        f"{base}{path}",
        json=json_body or {},
        headers={"Authorization": f"Bearer {_service_token()}"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def handler(conn, fields):
    """
    A new PO exists. Decide, then act.

    Note what is and is not inside consume()'s transaction. The DECLINE path
    writes an alerts row through `conn`, so it commits atomically with the
    processed_events claim, exactly like any other worker write. The ACCEPT path
    makes HTTP calls, which cannot be transactional -- so it is written to be
    safely retryable instead: if the agent crashes after confirming the PO but
    before creating the shipment, the retry finds the PO already CONFIRMED, the
    confirm call 409s, and the handler carries on to the shipment rather than
    aborting. Idempotence by resumption, not by rollback.
    """
    po_id = fields["entity_id"]
    payload = fields["payload"]

    with conn.cursor() as cur:
        cur.execute(
            """SELECT po.status, po.supplier_id, s.name, s.reliability_score,
                      s.avg_lead_time_days, s.location_id, po.expected_delivery,
                      po.delivery_location_id, po.qty, m.name
               FROM purchase_orders po
               LEFT JOIN suppliers s ON s.id = po.supplier_id
               LEFT JOIN materials m ON m.id = po.material_id
               WHERE po.id=%s""",
            (po_id,),
        )
        row = cur.fetchone()
        if row is None:
            log.warning("%s: purchase order not found -- nothing to confirm", po_id)
            return
        (status, supplier_id, supplier_name, reliability, lead_days,
         origin_loc, expected_delivery, dest_loc, qty, material) = row

        # Already past confirmation: a duplicate delivery, or a human got there
        # first. Either way there is nothing to do, and re-raising a shipment
        # would double-ship the order.
        if status not in ("CREATED", "CONFIRMED"):
            log.info("%s: already %s -- skipping", po_id, status)
            return

        cur.execute(
            "SELECT id FROM shipments WHERE po_id=%s LIMIT 1", (po_id,))
        if cur.fetchone() is not None:
            log.info("%s: shipment already exists -- skipping", po_id)
            return

        reliability = float(reliability if reliability is not None else 0.8)

        if not _accepts(po_id, reliability):
            # A declined PO is a real procurement outcome, not an error. It stays
            # CREATED and a human picks it up -- which is the correct place for a
            # judgement call the system is not entitled to make on its own.
            alert_id = next_id(cur, "ALT")
            message = (f"{supplier_name or supplier_id} declined {po_id} "
                       f"(reliability {reliability:.2f}) -- needs re-sourcing")
            cur.execute(
                """INSERT INTO alerts (id, entity_type, entity_id, alert_type, message, severity)
                   VALUES (%s,'purchase_order',%s,'SUPPLIER_DECLINED',%s,'warning')""",
                (alert_id, po_id, message),
            )
            record_event(conn, "alert", alert_id, "ALERT_CREATED", {
                "summary": message,
                "entity_type": "purchase_order",
                "entity_id": po_id,
                "alert_type": "SUPPLIER_DECLINED",
                "severity": "warning",
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
            })
            log.info("%s: DECLINED by %s -> %s", po_id, supplier_name, alert_id)
            return

    # ---- accepted: drive the public APIs, outside the cursor block ----
    now = datetime.now(timezone.utc)
    lead = timedelta(days=float(lead_days)) if lead_days else timedelta(hours=DEFAULT_LEAD_HOURS)
    eta = expected_delivery or (now + lead)

    if status == "CREATED":
        try:
            _post(f"/purchase-orders/{po_id}/confirm", PROCUREMENT_API, {
                "confirmed_delivery_date": eta.isoformat(),
                "confirmed_by": supplier_name,
                "notes": f"accepted automatically on supplier reliability {reliability:.2f}",
            })
        except httpx.HTTPStatusError as exc:
            # 409 means someone confirmed it between our read and our write.
            # That is the outcome we wanted, so carry on to the shipment.
            if exc.response.status_code != 409:
                raise
            log.info("%s: already confirmed by another actor -- continuing", po_id)

    shipment = _post("/shipments", YARD_API, {
        "po_id": po_id,
        "tracking_number": f"TRK-{po_id.split('-')[-1]}",
        "carrier": (payload.get("carrier")
                    or f"{(supplier_name or 'Supplier').split()[0]} Freight"),
        "origin_location_id": origin_loc,
        "destination_location_id": dest_loc,
        "expected_arrival": eta.isoformat(),
    })
    shipment_id = shipment["id"]

    trailer = _post(f"/shipments/{shipment_id}/trailers", YARD_API, {
        "load_type": payload.get("load_type") or "dry_van",
        "priority": payload.get("priority") or "normal",
    })

    log.info("%s: CONFIRMED by %s -> %s / %s (%s x %s, eta %s)",
             po_id, supplier_name, shipment_id, trailer["id"],
             qty, material, eta.isoformat(timespec="minutes"))


def main():
    log.info("starting; group=%s allowed=%s yard=%s procurement=%s",
             GROUP, sorted(ALLOWED), YARD_API, PROCUREMENT_API)
    threading.Thread(target=_reconciler_loop, daemon=True).start()
    with get_conn() as conn:
        consume(conn, GROUP, "supplier-agent-1", handler, allowed_event_types=ALLOWED)


if __name__ == "__main__":
    main()
