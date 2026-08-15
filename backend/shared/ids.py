"""
Human-readable ID generation, shared by every service.

IDs are TEXT with a prefix (PO-10245, TRL-3391) so they are directly
presentable in a live demo -- "show me what happened to PO-10245" is
answerable at a glance.

The numeric suffix comes from a real Postgres sequence, never COUNT(*)+1:
nextval() is atomic at the database level, COUNT(*)+1 is not and generates
duplicate IDs under concurrent requests. The sequences are declared at the
bottom of schema.sql for every entity that generates its own ID.
"""

# entity prefix -> sequence name, matching schema.sql exactly.
SEQUENCES = {
    "REQ": "requisition_id_seq",
    "SR": "supplier_recommendation_id_seq",
    "PO": "purchase_order_id_seq",
    "SHP": "shipment_id_seq",
    "TRL": "trailer_id_seq",
    "DA": "dock_assignment_id_seq",
    "GR": "goods_receipt_id_seq",
    "INV": "invoice_id_seq",
    "MR": "match_result_id_seq",
    "EXC": "exception_id_seq",
    "PAY": "payment_id_seq",
    "ALT": "alert_id_seq",
    # v7 -- outbound
    "OBO": "outbound_order_id_seq",
    "LP": "load_plan_id_seq",
    "GI": "goods_issue_id_seq",
}


def next_id(cur, prefix: str) -> str:
    """
    Concurrency-safe human-readable ID. `cur` is a cursor inside the caller's
    existing transaction -- nextval() is exempt from rollback by design, so an
    aborted transaction burns an ID rather than reusing one. That is correct:
    gaps are harmless, duplicates are not.
    """
    seq = SEQUENCES[prefix]
    cur.execute(f"SELECT nextval('{seq}')")
    return f"{prefix}-{cur.fetchone()[0]}"
