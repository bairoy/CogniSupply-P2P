"""
3-way match worker. Background consume() loop, no HTTP surface.

Consumer group: match-worker
allowed_event_types: GOODS_RECEIVED, INVOICE_RECEIVED, SHIPMENT_CREATED

SHIPMENT_CREATED is the v4 addition (BUILD_PLAN.md §2.3). purchase_orders is
PR2-owned and Yard API must never write it, but purchase_orders.status
otherwise never advanced past CREATED, which made the pipeline funnel
uncomputable. This worker is therefore the PR2-side status reconciler:

    SHIPMENT_CREATED -> SHIPPED
    GOODS_RECEIVED   -> RECEIVED (or PARTIALLY_RECEIVED)
    match approved   -> MATCHED
    payment paid     -> CLOSED   (Procurement API, on /payments/{id}/pay)

Ownership rule intact: PR2 writes PR2 tables, E2 writes E2 tables.

The match DECISION lives in shared/match_policy.py and contains no model call.
This file is the I/O shell: find the pairing, run the policy, persist.

Run:  ./.venv/bin/python backend/services/match_worker/main.py
"""

import logging
import sys
import threading
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

# .env must load BEFORE event_bus is imported -- it reads REDIS_URL at module
# import time, so a later load would be ignored.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT.parent / ".env")

from event_bus import consume, reconcile_unpublished, record_event  # noqa: E402
from shared.db import get_conn  # noqa: E402
from shared.ids import next_id  # noqa: E402
from shared.match_policy import SEVERITY_BY_TYPE, evaluate  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  match-worker  %(levelname)-7s %(message)s",
)
log = logging.getLogger("match-worker")

# Service account for touchless approvals (FK target in users).
SYSTEM_USER = "USR-000"

GROUP = "match-worker"
ALLOWED = {"GOODS_RECEIVED", "INVOICE_RECEIVED", "SHIPMENT_CREATED"}

RECONCILE_INTERVAL_SECONDS = 1.0


def _reconciler_loop():
    """
    Handlers only record_event(); consume() owns the commit. This thread does
    the publishing afterwards using event_bus's own documented recovery path,
    so the worker introduces no second publishing mechanism.
    """
    while True:
        try:
            with get_conn() as conn:
                reconcile_unpublished(conn, limit=200)
                conn.commit()
        except Exception:
            log.exception("reconciler pass failed; retrying")
        time.sleep(RECONCILE_INTERVAL_SECONDS)


def _set_po_status(cur, po_id, status):
    cur.execute(
        "UPDATE purchase_orders SET status=%s, updated_at=now() WHERE id=%s AND status <> %s",
        (status, po_id, status),
    )
    return cur.rowcount > 0


def _emit_po_status(conn, po_id, status, note):
    payload = {"summary": f"{po_id} -> {status} ({note})", "status": status, "po_id": po_id}
    record_event(conn, "purchase_order", po_id, "PO_STATUS_CHANGED", payload)


def _try_match(conn, cur, po_id, trigger):
    """
    Run the 3-way match if BOTH a goods receipt and an unmatched invoice exist
    for this PO. If the pairing is incomplete, do nothing and wait for the other
    trigger -- a missing goods receipt is NOT a quantity mismatch, and treating
    it as one would be the single most damaging bug in this system.
    """
    cur.execute(
        """SELECT id, qty_received FROM goods_receipts
           WHERE po_id=%s ORDER BY received_at LIMIT 1""",
        (po_id,),
    )
    receipt = cur.fetchone()
    if receipt is None:
        log.info("%s: no goods receipt yet (trigger=%s) -- waiting", po_id, trigger)
        return
    gr_id, qty_received = receipt

    # The unique index on match_results(invoice_id) guarantees an invoice can
    # never be matched twice; this finds invoices not yet matched at all.
    cur.execute(
        """SELECT i.id, i.qty_invoiced, i.unit_price_invoiced, i.total
           FROM invoices i
           LEFT JOIN match_results mr ON mr.invoice_id = i.id
           WHERE i.po_id=%s AND mr.id IS NULL
           ORDER BY i.received_at""",
        (po_id,),
    )
    invoices = cur.fetchall()
    if not invoices:
        log.info("%s: no unmatched invoice yet (trigger=%s) -- waiting", po_id, trigger)
        return

    cur.execute("SELECT qty, unit_price, status FROM purchase_orders WHERE id=%s", (po_id,))
    po = cur.fetchone()
    if po is None:
        log.warning("%s: purchase order vanished", po_id)
        return
    po_qty, po_unit_price, po_status = po

    for inv_id, qty_invoiced, unit_price_invoiced, total in invoices:
        decision = evaluate(
            po_id=po_id, po_status=po_status, po_qty=po_qty, po_unit_price=po_unit_price,
            qty_received=qty_received, qty_invoiced=qty_invoiced,
            unit_price_invoiced=unit_price_invoiced,
        )

        mr_id = next_id(cur, "MR")
        cur.execute(
            """INSERT INTO match_results (id, po_id, goods_receipt_id, invoice_id, status, reason)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (mr_id, po_id, gr_id, inv_id, decision.status, decision.reason),
        )
        record_event(conn, "match_result", mr_id, "MATCH_COMPLETED", {
            "summary": f"{mr_id}: {decision.status} -- {decision.reason}",
            "po_id": po_id, "invoice_id": inv_id, "status": decision.status,
            "reason": decision.reason,
            "qty_variance_pct": float(decision.qty_variance_pct or 0),
            "price_variance_pct": float(decision.price_variance_pct or 0),
        })

        if decision.approved:
            pay_id = next_id(cur, "PAY")
            cur.execute(
                """INSERT INTO payments (id, invoice_id, amount, status, approved_by, approved_at)
                   VALUES (%s,%s,%s,'APPROVED',%s,now())""",
                (pay_id, inv_id, total, SYSTEM_USER),
            )
            _set_po_status(cur, po_id, "MATCHED")
            po_status = "MATCHED"  # so a second invoice in this batch sees it
            record_event(conn, "payment", pay_id, "PAYMENT_APPROVED", {
                "summary": f"{pay_id} approved for {total} against {inv_id}",
                "invoice_id": inv_id, "amount": float(total or 0), "po_id": po_id,
            })
            _emit_po_status(conn, po_id, "MATCHED", "3-way match approved")
            log.info("%s: APPROVED -> %s", po_id, pay_id)
        else:
            exc_id = next_id(cur, "EXC")
            cur.execute(
                """INSERT INTO exceptions (id, match_result_id, exception_type, status,
                                           severity, impact_amount)
                   VALUES (%s,%s,%s,'OPEN',%s,%s)""",
                (exc_id, mr_id, decision.exception_type,
                 decision.severity or SEVERITY_BY_TYPE.get(decision.exception_type, "medium"),
                 decision.impact_amount),
            )
            record_event(conn, "exception", exc_id, "EXCEPTION_CREATED", {
                "summary": f"{exc_id}: {decision.exception_type} on {po_id} -- {decision.reason}",
                "exception_type": decision.exception_type,
                "severity": decision.severity,
                "impact_amount": float(decision.impact_amount or 0),
                "po_id": po_id, "invoice_id": inv_id, "match_result_id": mr_id,
            })
            log.info("%s: EXCEPTION %s -> %s", po_id, decision.exception_type, exc_id)


def _match_invoice_without_po(conn, cur, invoice_id):
    """
    An invoice with no PO reference can never pair with a goods receipt, so it
    would otherwise sit unmatched forever. Run the policy immediately -- it
    returns MISSING_PO at step 1, before any tolerance math.
    """
    cur.execute(
        """SELECT i.id FROM invoices i LEFT JOIN match_results mr ON mr.invoice_id = i.id
           WHERE i.id=%s AND mr.id IS NULL""",
        (invoice_id,),
    )
    if cur.fetchone() is None:
        return

    decision = evaluate(
        po_id=None, po_status="", po_qty=0, po_unit_price=0,
        qty_received=0, qty_invoiced=0, unit_price_invoiced=0,
    )
    mr_id = next_id(cur, "MR")
    cur.execute(
        """INSERT INTO match_results (id, po_id, goods_receipt_id, invoice_id, status, reason)
           VALUES (%s,NULL,NULL,%s,%s,%s)""",
        (mr_id, invoice_id, decision.status, decision.reason),
    )
    exc_id = next_id(cur, "EXC")
    cur.execute(
        """INSERT INTO exceptions (id, match_result_id, exception_type, status, severity)
           VALUES (%s,%s,%s,'OPEN',%s)""",
        (exc_id, mr_id, decision.exception_type, decision.severity),
    )
    record_event(conn, "match_result", mr_id, "MATCH_COMPLETED", {
        "summary": f"{mr_id}: EXCEPTION -- {decision.reason}",
        "invoice_id": invoice_id, "status": decision.status,
    })
    record_event(conn, "exception", exc_id, "EXCEPTION_CREATED", {
        "summary": f"{exc_id}: MISSING_PO on {invoice_id} -- {decision.reason}",
        "exception_type": decision.exception_type, "severity": decision.severity,
        "invoice_id": invoice_id,
    })
    log.info("%s: MISSING_PO -> %s", invoice_id, exc_id)


def handler(conn, fields):
    event_type = fields["event_type"]
    entity_id = fields["entity_id"]
    payload = fields["payload"]

    with conn.cursor() as cur:
        if event_type == "SHIPMENT_CREATED":
            # v7: outbound shipments emit SHIPMENT_CREATED too, with po_id
            # explicitly null -- an outbound move has no purchase order behind
            # it. The `if po_id` guard is what makes this worker ignore them,
            # so it is load-bearing now rather than merely defensive: without
            # it, an outbound despatch would try to advance a PO that does not
            # exist. Advancing from CREATED or CONFIRMED both work, since the
            # UPDATE keys on "not already SHIPPED" rather than a specific
            # predecessor.
            po_id = payload.get("po_id")
            if po_id and _set_po_status(cur, po_id, "SHIPPED"):
                _emit_po_status(conn, po_id, "SHIPPED", "shipment despatched")
            return

        if event_type == "GOODS_RECEIVED":
            po_id = payload.get("po_id")
            if not po_id:
                return
            # Partial delivery is representable in the schema, so report it
            # accurately rather than calling a short receipt "RECEIVED".
            cur.execute(
                """SELECT po.qty, COALESCE(SUM(gr.qty_received), 0)
                   FROM purchase_orders po
                   LEFT JOIN goods_receipts gr ON gr.po_id = po.id
                   WHERE po.id=%s GROUP BY po.qty""",
                (po_id,),
            )
            row = cur.fetchone()
            if row:
                ordered, received = row
                status = "RECEIVED" if received >= ordered else "PARTIALLY_RECEIVED"
                if _set_po_status(cur, po_id, status):
                    _emit_po_status(conn, po_id, status, f"{received} of {ordered} received")
            _try_match(conn, cur, po_id, trigger="GOODS_RECEIVED")
            return

        if event_type == "INVOICE_RECEIVED":
            po_id = payload.get("po_id")
            if po_id is None:
                _match_invoice_without_po(conn, cur, entity_id)
                return
            _try_match(conn, cur, po_id, trigger="INVOICE_RECEIVED")
            return


def main():
    log.info("starting; group=%s allowed=%s", GROUP, sorted(ALLOWED))
    threading.Thread(target=_reconciler_loop, daemon=True, name="reconciler").start()
    with get_conn() as conn:
        consume(conn, GROUP, "match-worker-1", handler, allowed_event_types=ALLOWED)


if __name__ == "__main__":
    main()
