"""
Dashboard Gateway. Read-only across BOTH domains, plus the one WebSocket.

It owns no tables. It exists because the dashboard needs cross-domain reads
(a KPI spanning trailers and invoices, an exception queue spanning `exceptions`
and `alerts`) and those queries belong in neither Yard API nor Procurement API.

TWO PATHS, NOT ONE (README §5):
  - REST for initial load, so a client connecting five minutes into the demo
    sees correct current state instead of an empty screen.
  - WebSocket for live deltas.
The event stream is for CHANGES, never for reconstructing state from scratch.

Run:  uvicorn services.dashboard_gateway.main:app --port 8003 --reload  (from backend/)
"""

import asyncio
import json
import logging
import queue
import sys
import threading
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, WebSocket, WebSocketDisconnect

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

# .env must load BEFORE event_bus is imported -- it reads REDIS_URL at module
# import time, so a later load would be ignored.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT.parent / ".env")

from event_bus import consume, publish_to_redis, record_event  # noqa: E402
from shared.api import create_app  # noqa: E402
from shared.auth import PERM_ALERT_ACK, decode_token, require  # noqa: E402
from shared.auth_routes import router as auth_router  # noqa: E402
from shared.db import get_conn  # noqa: E402
from shared.telemetry import collapse_telemetry, telemetry_mode  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")

app = create_app(
    "CogniSupply P2P — Dashboard Gateway",
    description="Cross-domain reads, global search, traceability, the live "
                "event WebSocket, and authentication (the only token issuer).",
)

# The gateway is the sole issuer of tokens; the other two services verify them
# locally. See shared/auth_routes.py for why login lives here.
app.include_router(auth_router)

EVAL_RESULTS_PATH = BACKEND_ROOT / "eval" / "eval_results.json"


def _iso(v):
    return v.isoformat() if v else None


def _f(v):
    return float(v) if v is not None else None


# ─────────────────────────────────────────────
# KPIs  (README §8 -- measured, never claimed)
# ─────────────────────────────────────────────

@app.get("/dashboard/overview", tags=["dashboard"])
def overview():
    """
    Every number here is computed from this run's own data. None of them are
    presented as a Cognizant-given target -- see README §8.

    Each KPI below carries the definition it is measured against, because a
    rate is only as good as its denominator. `kpi_basis` in the response
    returns the sample size behind every rate and average, so a reader can
    see what the percentage is a percentage OF without querying the database.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  -- v6: 'active' means still in the yard or inbound. UNLOADED is
                  -- still here (door released, tractor not yet through the gate);
                  -- DEPARTED is what actually removes a trailer from the count.
                  (SELECT count(*) FROM trailers WHERE status <> 'DEPARTED'),
                  (SELECT count(*) FROM exceptions WHERE status='OPEN'),
                  (SELECT count(*) FROM invoices i
                     LEFT JOIN match_results mr ON mr.invoice_id=i.id
                     WHERE mr.id IS NULL),
                  (SELECT count(*) FROM docks d WHERE d.is_active AND EXISTS (
                     SELECT 1 FROM dock_assignments da WHERE da.dock_id=d.id
                       AND da.status IN ('ASSIGNED','CONFIRMED'))),
                  (SELECT count(*) FROM docks WHERE is_active),
                  (SELECT count(*) FROM match_results),
                  (SELECT count(*) FROM match_results WHERE status='APPROVED'),
                  (SELECT count(*) FROM alerts WHERE NOT acknowledged),
                  (SELECT count(*) FROM payments WHERE status IN ('APPROVED','PAID'))
            """)
            (active_trailers, open_exceptions, pending_invoices, docks_occupied,
             docks_total, total_matches, approved_matches, open_alerts,
             payments_made) = cur.fetchone()

            # ── First-pass match rate ──────────────────────────────────────
            # Of every 3-way match RUN, the share that cleared with no
            # exception raised. This is a match-engine quality measure, and
            # `match_results.status` is safe as its basis precisely because
            # nothing ever mutates it: /exceptions/{id}/resolve stamps
            # `resolved_at` and leaves status='EXCEPTION' forever, so an
            # invoice a person later waved through can never be counted here.
            # An invoice with no PO is in the denominator too -- it is an
            # invoice that failed to clear automatically, whatever the reason.
            first_pass = (approved_matches / total_matches) if total_matches else 0

            # ── Straight-through processing ────────────────────────────────
            # A different question from first-pass, over a different
            # population: of every invoice RECEIVED, the share that reached a
            # settled payment without a person touching it anywhere.
            #
            # Denominator is all invoices, not matches run, so invoices still
            # queued for a match count against it -- an invoice sitting
            # unmatched has not been processed straight through, it has not
            # been processed at all. The two rates therefore converge when the
            # pipeline is fully caught up and diverge whenever it is not,
            # which is the honest behaviour rather than a defect.
            #
            # count(DISTINCT i.id) not count(*): the exception-resolution path
            # can add a second payment row to an invoice, and an invoice is
            # touchless once or not at all.
            cur.execute("""
                SELECT count(DISTINCT i.id)
                FROM invoices i
                JOIN match_results mr ON mr.invoice_id = i.id
                JOIN payments p ON p.invoice_id = i.id
                                AND p.status IN ('APPROVED','PAID')
                WHERE NOT EXISTS (SELECT 1 FROM exceptions e
                                  WHERE e.match_result_id = mr.id)
            """)
            touchless = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM invoices")
            invoices_received = cur.fetchone()[0]
            touchless_rate = (touchless / invoices_received) if invoices_received else 0

            # ── Truck turnaround ───────────────────────────────────────────
            # Gate-in to gate-out, the standard definition: TRAILER_ARRIVED ->
            # TRAILER_EXITED.
            #
            # It is measured from event_log rather than from
            # dock_assignments.assigned_at, which is what this used to do and
            # was wrong: dock-worker plans trailers while they are still
            # EN_ROUTE, so assigned_at can precede arrival by hours and the
            # "turnaround" silently included highway driving time. On this
            # run's data that inflated the figure from 259 min to 522.
            #
            # TRAILER_DEPARTED is NOT the gate-out event -- it is departure
            # from the ORIGIN. TRAILER_EXITED is our gate. Both directions
            # emit both events identically, so one query serves each.
            cur.execute("""
                WITH gate AS (
                    SELECT t.id, t.direction,
                           min(el.created_at) FILTER (
                               WHERE el.event_type='TRAILER_ARRIVED') AS gate_in,
                           max(el.created_at) FILTER (
                               WHERE el.event_type='TRAILER_EXITED')  AS gate_out
                    FROM trailers t
                    JOIN event_log el ON el.entity_type='trailer' AND el.entity_id = t.id
                    WHERE el.event_type IN ('TRAILER_ARRIVED','TRAILER_EXITED')
                    GROUP BY t.id, t.direction
                )
                SELECT
                  avg(EXTRACT(EPOCH FROM (gate_out-gate_in))/60)
                    FILTER (WHERE direction='INBOUND'),
                  count(*) FILTER (WHERE direction='INBOUND'),
                  avg(EXTRACT(EPOCH FROM (gate_out-gate_in))/60)
                    FILTER (WHERE direction='OUTBOUND'),
                  count(*) FILTER (WHERE direction='OUTBOUND')
                FROM gate
                WHERE gate_in IS NOT NULL AND gate_out IS NOT NULL
                  AND gate_out >= gate_in
            """)
            (avg_turnaround, turnaround_n,
             ob_turnaround, ob_turnaround_n) = cur.fetchone()

            # ── P2P cycle time ─────────────────────────────────────────────
            # PO raised -> payment approved, in hours. Status-filtered so a
            # rejected payment that still carries an approved_at can never
            # enter the average.
            cur.execute("""
                SELECT avg(EXTRACT(EPOCH FROM (p.approved_at - po.created_at))/3600),
                       count(*)
                FROM payments p
                JOIN invoices i ON i.id = p.invoice_id
                JOIN purchase_orders po ON po.id = i.po_id
                WHERE p.approved_at IS NOT NULL
                  AND p.status IN ('APPROVED','PAID')
                  AND p.approved_at >= po.created_at
            """)
            avg_cycle_hours, cycle_n = cur.fetchone()

            # ── Exception handling ─────────────────────────────────────────
            # Detection (exceptions.created_at, written by match-worker in the
            # same transaction as the match) -> human resolution
            # (exceptions.resolved_at, stamped by /exceptions/{id}/resolve).
            # Nothing else writes either column, so this is the real clock a
            # person was on. NULL until the first exception is resolved.
            cur.execute("""
                SELECT avg(EXTRACT(EPOCH FROM (resolved_at - created_at))/60), count(*)
                FROM exceptions
                WHERE resolved_at IS NOT NULL AND resolved_at >= created_at
            """)
            avg_resolution_minutes, exceptions_resolved = cur.fetchone()

            cur.execute("SELECT count(*) FROM exceptions WHERE severity IN ('critical','high') "
                        "AND status='OPEN'")
            critical_open = cur.fetchone()[0]

            # v7 outbound. Reported separately rather than folded into the
            # numbers above, because an outbound truck is not an inbound one
            # with a flag: it consumes the same doors but contributes nothing to
            # match rate, touchless rate or P2P cycle time. Merging them would
            # dilute every PR2 KPI with volume that has no invoice behind it.
            cur.execute("""
                SELECT
                  (SELECT count(*) FROM outbound_orders
                     WHERE status IN ('CREATED','PLANNED','STAGED','LOADING')),
                  (SELECT count(*) FROM outbound_orders WHERE status='DELIVERED'),
                  (SELECT count(*) FROM trailers
                     WHERE direction='OUTBOUND' AND status NOT IN ('DELIVERED')),
                  (SELECT count(*) FROM goods_issues)
            """)
            (ob_open, ob_delivered, ob_trailers, ob_issues) = cur.fetchone()
            # ob_turnaround comes from the same gate-in/gate-out CTE above, not
            # from a mirrored goods_issues query -- a door is one resource and
            # a truck is one truck, so both directions must be measured the
            # same way or the two figures cannot be compared.
        conn.rollback()

    return {
        "active_trailers": active_trailers,
        "outbound_open_orders": ob_open,
        "outbound_delivered": ob_delivered,
        "outbound_trailers": ob_trailers,
        "goods_issues": ob_issues,
        "open_exceptions": open_exceptions,
        "critical_exceptions": critical_open,
        "pending_invoices": pending_invoices,
        "docks_occupied": docks_occupied,
        "docks_total": docks_total,
        "open_alerts": open_alerts,
        "kpis": {
            "first_pass_match_rate": round(first_pass, 4),
            "touchless_rate": round(touchless_rate, 4),
            "dock_utilisation": round(docks_occupied / docks_total, 4) if docks_total else 0,
            # EXTRACT(EPOCH ...) is numeric on PG16, so avg() comes back as
            # Decimal -- floated here so the JSON is plain numbers.
            "avg_turnaround_minutes": round(_f(avg_turnaround), 1) if avg_turnaround else None,
            "avg_p2p_cycle_hours": round(_f(avg_cycle_hours), 1) if avg_cycle_hours else None,
            # Interventions that actually HAPPENED -- exceptions a person
            # closed. `open_exceptions` above is the separate, forward-looking
            # figure: how many are still waiting for one. This used to be a
            # duplicate of open_exceptions under a name that promised the
            # other number.
            "human_interventions": exceptions_resolved,
            "avg_exception_resolution_minutes": (round(_f(avg_resolution_minutes), 1)
                                                 if avg_resolution_minutes else None),
            "avg_outbound_turnaround_minutes": (round(_f(ob_turnaround), 1)
                                                if ob_turnaround else None),
        },
        # Sample sizes for every averaged/rated KPI above. A rate over three
        # invoices and a rate over three hundred are not the same claim, and a
        # dashboard that shows only the percentage hides which one it is.
        "kpi_basis": {
            "matches_run": total_matches,
            "matches_first_pass": approved_matches,
            "invoices_received": invoices_received,
            "invoices_touchless": touchless,
            "trailers_turned": turnaround_n,
            "outbound_trailers_turned": ob_turnaround_n,
            "payments_in_cycle_time": cycle_n,
            "exceptions_resolved": exceptions_resolved,
        },
        "measured_from": "this run's seeded + live data; not a vendor-supplied target",
    }


@app.get("/dashboard/pipeline", tags=["dashboard"])
def pipeline():
    """
    Funnel counts per stage, for the Control Tower's Pipeline Volume panel.

    v7 returns TWO funnels, not one longer one. The inbound funnel is the
    procure-to-pay chain and ends at a payment; the outbound funnel is an order
    fulfilment chain and ends at a delivery. They share the dock stage in the
    middle and nothing else -- no money flows through outbound, so appending its
    stages to the P2P funnel would produce a chart where the counts stop meaning
    the same kind of thing halfway along.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT count(*) FROM requisitions),
                  (SELECT count(*) FROM requisitions WHERE status='CONVERTED'),
                  (SELECT count(*) FROM purchase_orders),
                  (SELECT count(*) FROM trailers
                     WHERE direction='INBOUND' AND status IN ('EN_ROUTE','ARRIVED')),
                  (SELECT count(*) FROM trailers WHERE direction='INBOUND' AND status='DOCKED'),
                  (SELECT count(*) FROM goods_receipts),
                  (SELECT count(*) FROM invoices),
                  (SELECT count(*) FROM match_results),
                  (SELECT count(*) FROM payments),
                  (SELECT count(*) FROM alerts WHERE alert_type='DELAY' AND NOT acknowledged),
                  (SELECT count(*) FROM exceptions WHERE status='OPEN'),
                  (SELECT count(*) FROM purchase_orders WHERE status='CONFIRMED')
            """)
            (reqs, sourced, pos, transit, docked, received, invoices, matched, paid,
             delayed, exceptions, confirmed) = cur.fetchone()

            cur.execute("""
                SELECT
                  (SELECT count(*) FROM outbound_orders),
                  (SELECT count(*) FROM outbound_orders
                     WHERE status IN ('PLANNED','STAGED','LOADING','SHIPPED','DELIVERED')),
                  (SELECT count(*) FROM outbound_orders
                     WHERE status IN ('STAGED','LOADING','SHIPPED','DELIVERED')),
                  (SELECT count(*) FROM trailers
                     WHERE direction='OUTBOUND' AND status IN ('EN_ROUTE','ARRIVED')),
                  (SELECT count(*) FROM trailers WHERE direction='OUTBOUND' AND status='DOCKED'),
                  (SELECT count(*) FROM goods_issues),
                  (SELECT count(*) FROM outbound_orders WHERE status='DELIVERED')
            """)
            (ob_orders, ob_planned, ob_staged, ob_transit, ob_docked,
             ob_issued, ob_delivered) = cur.fetchone()
        conn.rollback()

    # `label` is presentation only -- consumers key off `key`, never the label,
    # so these read as the business documents an enterprise user expects.
    inbound = [
        {"key": "requisition", "label": "Requisition", "count": reqs},
        {"key": "sourcing", "label": "Sourcing", "count": sourced},
        {"key": "po", "label": "PO Issued", "count": pos, "confirmed": confirmed},
        {"key": "transit", "label": "In Transit", "count": transit, "delayed": delayed},
        {"key": "docking", "label": "Yard & Dock", "count": docked},
        {"key": "receiving", "label": "GRN Posted", "count": received},
        {"key": "invoice", "label": "Invoice Received", "count": invoices},
        {"key": "match", "label": "3-Way Match", "count": matched, "exceptions": exceptions},
        {"key": "payment", "label": "Settlement", "count": paid},
    ]
    outbound = [
        {"key": "order", "label": "Customer Order", "count": ob_orders},
        {"key": "planned", "label": "Load Planned", "count": ob_planned},
        {"key": "staged", "label": "Staged", "count": ob_staged},
        {"key": "collection", "label": "Collection", "count": ob_transit},
        {"key": "loading", "label": "Loading", "count": ob_docked},
        {"key": "issued", "label": "Goods Issued", "count": ob_issued},
        {"key": "delivered", "label": "Delivered", "count": ob_delivered},
    ]
    # `stages` keeps its exact pre-v7 meaning so nothing already reading it
    # breaks; the two named funnels are additive.
    return {"stages": inbound, "inbound": inbound, "outbound": outbound}


@app.get("/dashboard/at-risk", tags=["dashboard"])
def at_risk(limit: int = 20):
    """
    At-Risk Orders & Exceptions: open exceptions, unacknowledged delay alerts,
    and requisitions that have sat unconverted. Three sources, one ranked list.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT e.id, mr.po_id, e.exception_type, e.severity, e.impact_amount,
                       e.created_at, s.name, u.name
                FROM exceptions e
                LEFT JOIN match_results mr ON mr.id = e.match_result_id
                LEFT JOIN purchase_orders po ON po.id = mr.po_id
                LEFT JOIN suppliers s ON s.id = po.supplier_id
                LEFT JOIN users u ON u.id = e.assigned_to
                WHERE e.status='OPEN'
                ORDER BY CASE e.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                         WHEN 'medium' THEN 2 ELSE 3 END, e.created_at
                LIMIT %s
            """, (limit,))
            rows = [{
                "reference_id": r[1] or r[0], "entity_id": r[0], "kind": "exception",
                "issue_type": r[2], "severity": r[3], "value": _f(r[4]),
                "created_at": _iso(r[5]), "supplier": r[6], "owner": r[7] or "Unassigned",
            } for r in cur.fetchall()]

            cur.execute("""
                SELECT a.id, a.entity_id, a.alert_type, a.severity, a.message, a.created_at
                FROM alerts a WHERE NOT a.acknowledged
                ORDER BY a.created_at DESC LIMIT %s
            """, (limit,))
            rows += [{
                "reference_id": r[1], "entity_id": r[0], "kind": "alert",
                "issue_type": r[2], "severity": r[3], "value": None,
                "created_at": _iso(r[5]), "supplier": None, "owner": "System",
                "message": r[4],
            } for r in cur.fetchall()]

            cur.execute("""
                SELECT r.id, r.created_at FROM requisitions r
                WHERE r.status='PARSED' AND r.created_at < now() - interval '4 hours'
                ORDER BY r.created_at LIMIT %s
            """, (limit,))
            rows += [{
                "reference_id": r[0], "entity_id": r[0], "kind": "requisition",
                "issue_type": "APPROVAL_STALL", "severity": "medium", "value": None,
                "created_at": _iso(r[1]), "supplier": None, "owner": "Procurement",
            } for r in cur.fetchall()]
        conn.rollback()

    order = {"critical": 0, "high": 1, "medium": 2, "warning": 2, "low": 3, "info": 4}
    rows.sort(key=lambda x: (order.get(x["severity"], 5), x["created_at"] or ""))
    return {"at_risk": rows[:limit]}


# ─────────────────────────────────────────────
# Predictive invoice risk  (v8)
# ─────────────────────────────────────────────

# Pseudo-observations carried by the prior. A supplier with this many matched
# invoices is judged half on its own record and half on the prior; with many
# more, almost entirely on its own record.
#
# Five is the smallest value that stops the failure mode a raw rate has on a
# dataset this size: a supplier whose only two invoices both failed would
# otherwise be published as "100% risk" on the strength of two observations.
# Every supplier here has between 0 and 8 matched invoices, so unsmoothed rates
# would be almost pure noise.
RISK_PRIOR_STRENGTH = 5

# Display bands. They pick the badge colour and nothing else -- no decision,
# threshold or downstream consumer reads them.
RISK_BAND_HIGH = 0.30
RISK_BAND_MEDIUM = 0.15


def _risk_band(score: float) -> str:
    if score >= RISK_BAND_HIGH:
        return "high"
    if score >= RISK_BAND_MEDIUM:
        return "medium"
    return "low"


@app.get("/dashboard/supplier-risk", tags=["dashboard"])
def supplier_risk(limit: int = 10):
    """
    Predictive invoice risk: of the POs still in flight, which are most likely
    to produce a mismatched invoice, and why.

    This is a smoothed base rate, NOT a classifier and NOT a trained model, and
    it is reported as one. Every input to every score comes back with it --
    matches observed, exceptions observed, the raw rate, the master-data risk
    rating, the house average -- so any number on the dashboard can be
    re-derived by hand. README §8's rule ("measured, never claimed") applies to
    a forecast as much as to a KPI; the honest version of a prediction states
    what it is inferred from and how thin that evidence is.

    The score is a per-supplier posterior:

        prior = (house exception rate + supplier.risk_score) / 2
        score = (exceptions + k*prior) / (matched + k)      k = RISK_PRIOR_STRENGTH

    which is the observed rate pulled toward the prior in proportion to how
    little history backs it. A supplier with no matched invoice at all scores
    exactly the prior rather than a flattering zero, and `confidence` says how
    much of the score is its own record.

    The prior averages the house rate with `suppliers.risk_score` instead of
    picking one, because that column is a general risk rating that was never
    specifically about invoicing -- worth something for a supplier with no
    invoice history, not enough to stand alone once there is one.

    Risk is a property of the supplier, not of the individual PO: nothing
    knowable before the invoice arrives distinguishes two POs to the same
    supplier. What separates one PO from another is the money on it, so the
    list is ranked by what the forecast puts in doubt:

        expected_impact = score x typical_exception_severity x qty*unit_price

    where the severity term is the MEASURED median of
    `exceptions.impact_amount / PO value` over every priced exception in the
    database. Without it the only rupee figure available would be
    `score x whole PO value`, which asserts the entire order is at stake when
    the typical mismatch disputes a fraction of it.

    The one PO-level fact returned is `invoice_received` -- the invoice is
    already in and awaiting match, so the risk is imminent rather than larger.
    It is a flag, never a multiplier.

    Read-only across `suppliers`, `purchase_orders`, `match_results`,
    `exceptions`, `invoices`, `materials`. Writes nothing, emits nothing.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Per-supplier record. LEFT JOINs so a supplier that has never been
            # matched still appears -- it is precisely the supplier we know
            # least about, and dropping it would quietly exclude the riskiest
            # names from the ranking.
            cur.execute("""
                SELECT s.id, s.name, COALESCE(s.risk_score, 0),
                       count(mr.id),
                       count(*) FILTER (WHERE mr.status = 'EXCEPTION')
                FROM suppliers s
                LEFT JOIN purchase_orders po ON po.supplier_id = s.id
                LEFT JOIN match_results  mr ON mr.po_id = po.id
                GROUP BY s.id, s.name, s.risk_score
            """)
            history = cur.fetchall()

            # The house rate is taken over the WHOLE table, not by summing the
            # per-supplier counts above. An invoice that arrives with no PO
            # reference (MISSING_PO) produces a match_result with po_id NULL,
            # which joins to no supplier -- real evidence about how often
            # matching fails here, and excluding it would flatter the average
            # that every supplier's prior is built from.
            cur.execute("""
                SELECT count(*), count(*) FILTER (WHERE status = 'EXCEPTION')
                FROM match_results
            """)
            total_matched, total_exceptions = cur.fetchone()

            # How much money an exception actually puts in dispute, as a share
            # of the PO it landed on -- measured, not assumed. Without this the
            # only rupee figure available is `risk x whole PO value`, which
            # claims the entire order is at stake when a QTY_MISMATCH typically
            # disputes about 8% of it.
            #
            # MEDIAN, not mean: the observed ratios are long-tailed (a
            # DUPLICATE_INVOICE bills the order twice and so exceeds 100%,
            # while price slips sit near 6%). One duplicate would drag a mean
            # to roughly triple the typical case.
            cur.execute("""
                SELECT percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY e.impact_amount / (po.qty * po.unit_price)),
                       count(*)
                FROM exceptions e
                JOIN match_results   mr ON mr.id = e.match_result_id
                JOIN purchase_orders po ON po.id = mr.po_id
                WHERE e.impact_amount IS NOT NULL
                  AND po.qty * po.unit_price > 0
            """)
            severity_row = cur.fetchone()

            # Which way a supplier's invoices tend to fail. ORDER BY count DESC
            # then keeping the first row per supplier = the modal type.
            cur.execute("""
                SELECT po.supplier_id, e.exception_type, count(*) AS n
                FROM exceptions e
                JOIN match_results   mr ON mr.id = e.match_result_id
                JOIN purchase_orders po ON po.id = mr.po_id
                GROUP BY 1, 2
                ORDER BY 1, 3 DESC
            """)
            top_issue: dict = {}
            for supplier_id, exception_type, _n in cur.fetchall():
                top_issue.setdefault(supplier_id, exception_type)

            # POs where a mismatch is still possible. The status filter is the
            # intent; the NOT EXISTS is the guarantee -- a match_result is the
            # fact that matching happened, and a PO whose status lagged behind
            # it must not be forecast as if it were still open.
            cur.execute("""
                SELECT po.id, po.supplier_id, m.name, po.qty, po.unit_price,
                       po.status, po.expected_delivery,
                       EXISTS (SELECT 1 FROM invoices i WHERE i.po_id = po.id)
                FROM purchase_orders po
                LEFT JOIN materials m ON m.id = po.material_id
                WHERE po.status NOT IN ('MATCHED', 'CLOSED')
                  AND NOT EXISTS (
                      SELECT 1 FROM match_results mr WHERE mr.po_id = po.id)
            """)
            open_pos = cur.fetchall()
        conn.rollback()

    # The house average, and the fallback for a database with no match history
    # at all: 0.0, so a system that has never matched anything forecasts from
    # master data alone instead of dividing by zero.
    global_rate = total_exceptions / total_matched if total_matched else 0.0
    attributed = sum(r[3] for r in history)

    # None until at least one exception has been priced. Every rupee figure
    # below then reports "—" rather than falling back to a guessed severity,
    # for the same reason /kpi/model-performance 404s before an eval run.
    #
    # Rounded HERE, not on the way out, so the severity this response publishes
    # is the same one it computed with. Rounding only at the edge leaves a
    # response whose stated inputs do not quite reproduce its stated output --
    # fatal for a number whose defence is that it can be checked by hand.
    typical_severity = round(_f(severity_row[0]), 4) if severity_row[0] is not None else None
    severity_samples = severity_row[1]

    suppliers = {}
    for sid, name, static_risk, matched, exceptions in history:
        static_risk = float(static_risk)
        prior = (global_rate + static_risk) / 2
        score = (exceptions + RISK_PRIOR_STRENGTH * prior) / (matched + RISK_PRIOR_STRENGTH)
        suppliers[sid] = {
            "supplier_id": sid,
            "supplier": name,
            "matched_invoices": matched,
            "exceptions": exceptions,
            # None, not 0.0, when nothing has been matched. There is no observed
            # rate over zero observations, and 0% would read as a clean record.
            "observed_rate": round(exceptions / matched, 4) if matched else None,
            "static_risk_score": round(static_risk, 4),
            "risk_score": round(score, 4),
            "confidence": round(matched / (matched + RISK_PRIOR_STRENGTH), 4),
            "band": _risk_band(score),
            "likely_issue": top_issue.get(sid),
        }

    ranked = []
    for po_id, sid, material, qty, unit_price, status, expected, invoiced in open_pos:
        supplier = suppliers.get(sid)
        if supplier is None:      # PO with no supplier_id; nothing to forecast from
            continue
        exposure = _f(qty) * _f(unit_price) if qty is not None and unit_price is not None else None
        ranked.append({
            "po_id": po_id,
            "supplier_id": sid,
            "supplier": supplier["supplier"],
            "material": material,
            "qty": _f(qty),
            "value": exposure,
            "status": status,
            "expected_delivery": _iso(expected),
            "invoice_received": invoiced,
            "risk_score": supplier["risk_score"],
            "band": supplier["band"],
            "likely_issue": supplier["likely_issue"],
            "confidence": supplier["confidence"],
            # risk x typical severity x order value: the money this forecast
            # actually puts in doubt. All three factors are returned, so the
            # figure can be recomputed -- and disagreed with -- by hand.
            "expected_impact": (
                round(supplier["risk_score"] * typical_severity * exposure, 2)
                if exposure and typical_severity is not None else None
            ),
        })

    # Ranked by money at risk, not by probability. A 32% chance on a ₹34k order
    # is a smaller problem than a 25% chance on a ₹2.2 Cr one, and this is a
    # panel about where to look first. Probability breaks ties, and rows with no
    # priced severity yet fall to the bottom rather than sorting as zero-risk.
    ranked.sort(key=lambda r: (-(r["expected_impact"] or -1), -r["risk_score"]))

    return {
        "baseline": {
            "global_exception_rate": round(global_rate, 4),
            "matched_invoices": total_matched,
            "exceptions": total_exceptions,
            # Matches the per-supplier rows below are built from. It is lower
            # than matched_invoices by exactly the invoices that named no PO,
            # so the two sets of numbers reconcile on screen instead of looking
            # like one of them is wrong.
            "attributed_to_a_supplier": attributed,
            "prior_strength": RISK_PRIOR_STRENGTH,
            "typical_exception_severity": typical_severity,
            "severity_samples": severity_samples,
        },
        "suppliers": sorted(suppliers.values(), key=lambda s: -s["risk_score"]),
        "at_risk_pos": ranked[:limit],
        "open_pos_evaluated": len(ranked),
    }


# ─────────────────────────────────────────────
# Exceptions queue -- exceptions UNION alerts
# ─────────────────────────────────────────────

@app.get("/exceptions/queue", tags=["dashboard"])
def exceptions_queue(status: str = "OPEN", limit: int = 100):
    """
    The Exceptions Command Center feed.

    The design shows "Dock Delay" sitting next to "Price Mismatch", but those
    live in different tables: `exceptions` is match-only (FK to match_result),
    while dock delays and conflicts are `alerts`. This unions them read-only in
    the gateway -- neither table changes, and neither service grows a
    dependency on the other.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT e.id, e.exception_type, e.severity, e.status, e.impact_amount,
                       e.created_at, mr.po_id, mr.reason, mr.invoice_id, u.name, e.assigned_to
                FROM exceptions e
                LEFT JOIN match_results mr ON mr.id = e.match_result_id
                LEFT JOIN users u ON u.id = e.assigned_to
                WHERE (%s='ALL' OR e.status=%s)
                ORDER BY e.created_at DESC LIMIT %s
            """, (status, status, limit))
            items = [{
                "id": r[0], "source": "exception", "type": r[1], "severity": r[2],
                "status": r[3], "impact_amount": _f(r[4]), "created_at": _iso(r[5]),
                "entity_id": r[6] or r[8], "detail": r[7],
                "owner": r[9] or "Unassigned", "owner_id": r[10],
                "resolvable": True,
            } for r in cur.fetchall()]

            ack_filter = False if status == "OPEN" else None
            cur.execute("""
                SELECT a.id, a.alert_type, a.severity, a.acknowledged, a.message,
                       a.created_at, a.entity_id, a.entity_type
                FROM alerts a
                WHERE (%s::bool IS NULL OR a.acknowledged = %s)
                ORDER BY a.created_at DESC LIMIT %s
            """, (ack_filter, ack_filter, limit))
            items += [{
                "id": r[0], "source": "alert", "type": r[1],
                "severity": {"critical": "critical", "warning": "high"}.get(r[2], "medium"),
                "status": "ACKNOWLEDGED" if r[3] else "OPEN",
                "impact_amount": None, "created_at": _iso(r[5]),
                "entity_id": r[6], "detail": r[4], "owner": "System", "owner_id": None,
                "resolvable": False,
            } for r in cur.fetchall()]
        conn.rollback()

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    items.sort(key=lambda x: (order.get(x["severity"], 4), x["created_at"] or ""),
               reverse=False)
    return {"queue": items[:limit],
            "counts": {
                "total": len(items),
                "critical": sum(1 for i in items if i["severity"] == "critical"),
            }}


@app.get("/alerts", tags=["dashboard"])
def list_alerts(acknowledged: Optional[bool] = False, limit: int = 50):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, entity_type, entity_id, alert_type, message, severity,
                       acknowledged, created_at
                FROM alerts
                WHERE (%s::bool IS NULL OR acknowledged = %s)
                ORDER BY created_at DESC LIMIT %s
            """, (acknowledged, acknowledged, limit))
            rows = cur.fetchall()
        conn.rollback()
    return {"alerts": [{
        "id": r[0], "entity_type": r[1], "entity_id": r[2], "alert_type": r[3],
        "message": r[4], "severity": r[5], "acknowledged": r[6], "created_at": _iso(r[7]),
    } for r in rows]}


@app.post("/alerts/{alert_id}/acknowledge", tags=["dashboard"],
          dependencies=[Depends(require(PERM_ALERT_ACK))])
def acknowledge_alert(alert_id: str):
    """alerts.acknowledged exists in the schema and nothing previously wrote it."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT acknowledged, message FROM alerts WHERE id=%s", (alert_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"alert {alert_id} not found")
            if row[0]:
                raise HTTPException(409, f"alert {alert_id} is already acknowledged")
            cur.execute("UPDATE alerts SET acknowledged=TRUE WHERE id=%s", (alert_id,))

        payload = {"summary": f"{alert_id} acknowledged", "alert_id": alert_id}
        ev = record_event(conn, "alert", alert_id, "ALERT_ACKNOWLEDGED", payload)
        conn.commit()
        publish_to_redis(conn, ev[0], "alert", alert_id, "ALERT_ACKNOWLEDGED", payload, ev[1])
    return {"id": alert_id, "acknowledged": True}


# ─────────────────────────────────────────────
# Search + public tracker  (brief E2 requirement #1)
# ─────────────────────────────────────────────

def _resolve_reference(cur, ref: str):
    """
    Accepts a tracking number, trailer ID, shipment reference, PO, invoice or
    exception ID. The brief asks for the first three by name; supporting the
    rest costs nothing and makes Cmd+K useful.
    """
    ref = ref.strip()
    upper = ref.upper()

    cur.execute("SELECT id FROM trailers WHERE upper(id)=%s", (upper,))
    if (row := cur.fetchone()):
        return {"entity_type": "trailer", "entity_id": row[0]}

    cur.execute("SELECT id, po_id FROM shipments WHERE upper(id)=%s OR upper(tracking_number)=%s",
                (upper, upper))
    if (row := cur.fetchone()):
        cur.execute("SELECT id FROM trailers WHERE shipment_id=%s LIMIT 1", (row[0],))
        trailer = cur.fetchone()
        return {"entity_type": "shipment", "entity_id": row[0], "po_id": row[1],
                "trailer_id": trailer[0] if trailer else None}

    cur.execute("SELECT id FROM purchase_orders WHERE upper(id)=%s", (upper,))
    if (row := cur.fetchone()):
        return {"entity_type": "purchase_order", "entity_id": row[0]}

    cur.execute("SELECT id, po_id FROM invoices WHERE upper(id)=%s", (upper,))
    if (row := cur.fetchone()):
        return {"entity_type": "invoice", "entity_id": row[0], "po_id": row[1]}

    cur.execute("SELECT id FROM exceptions WHERE upper(id)=%s", (upper,))
    if (row := cur.fetchone()):
        return {"entity_type": "exception", "entity_id": row[0]}

    # v7: an outbound order is what a CUSTOMER quotes when they ring up asking
    # where their delivery is, so it has to resolve here or the public tracker
    # is inbound-only -- which would make "where's my truck" answerable for the
    # supplier's truck and not for the customer's.
    cur.execute("SELECT id, customer_name FROM outbound_orders WHERE upper(id)=%s", (upper,))
    if (row := cur.fetchone()):
        cur.execute("""SELECT t.id FROM trailers t
                       JOIN shipments s ON s.id = t.shipment_id
                       WHERE s.outbound_order_id=%s ORDER BY t.created_at DESC LIMIT 1""",
                    (row[0],))
        trailer = cur.fetchone()
        return {"entity_type": "outbound_order", "entity_id": row[0],
                "customer_name": row[1], "trailer_id": trailer[0] if trailer else None}

    return None


@app.get("/search", tags=["dashboard"])
def search(q: str, limit: int = 10):
    """Global Cmd+K. Exact ID resolution first, then a prefix sweep."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            exact = _resolve_reference(cur, q)
            like = f"%{q.upper()}%"
            results = []
            for table, kind in (("trailers", "trailer"), ("purchase_orders", "purchase_order"),
                                ("invoices", "invoice"), ("shipments", "shipment"),
                                ("exceptions", "exception"),
                                ("outbound_orders", "outbound_order")):   # v7
                cur.execute(
                    f"SELECT id FROM {table} WHERE upper(id) LIKE %s ORDER BY id LIMIT %s",
                    (like, limit),
                )
                results += [{"entity_type": kind, "entity_id": r[0]} for r in cur.fetchall()]
            cur.execute(
                "SELECT id, tracking_number FROM shipments WHERE upper(tracking_number) LIKE %s "
                "LIMIT %s", (like, limit))
            results += [{"entity_type": "shipment", "entity_id": r[0],
                         "tracking_number": r[1]} for r in cur.fetchall()]
        conn.rollback()
    return {"query": q, "exact": exact, "results": results[:limit]}


def _trailer_for_reference(cur, ref: str):
    """
    A customer reference -> (resolved, trailer_id). Raises the same 404s the
    tracker has always raised.

    Split out of track() in v8 so the tracker's WebSocket resolves a reference
    to a trailer by exactly the same rules the REST read does. Two copies would
    eventually disagree about, say, which trailer an outbound order maps to,
    and the socket would then be pushing updates about a different truck than
    the page is showing.
    """
    resolved = _resolve_reference(cur, ref)
    if resolved is None:
        raise HTTPException(404, f"nothing found for reference '{ref}'")

    trailer_id = resolved.get("trailer_id")
    if resolved["entity_type"] == "trailer":
        trailer_id = resolved["entity_id"]
    elif resolved["entity_type"] == "purchase_order":
        cur.execute("""SELECT t.id FROM trailers t JOIN shipments s ON s.id=t.shipment_id
                       WHERE s.po_id=%s LIMIT 1""", (resolved["entity_id"],))
        row = cur.fetchone()
        trailer_id = row[0] if row else None
    # outbound_order already carries trailer_id from the resolver.

    if trailer_id is None:
        raise HTTPException(
            404, f"'{ref}' resolved to {resolved['entity_type']} "
                 f"{resolved['entity_id']} but no trailer is attached yet")
    return resolved, trailer_id


@app.get("/track/{ref}", tags=["dashboard"])
def track(ref: str, telemetry: str = "collapsed"):
    """
    Customer-facing tracker: accepts a tracking number, trailer ID or shipment
    reference and returns location, ETA and delivery progress. No auth.

    `telemetry=collapsed` (default) folds runs of TRAILER_LOCATION_UPDATED in
    the timeline into one row each; `telemetry=full` returns every ping. The
    map is unaffected either way -- it draws `breadcrumbs`, which always comes
    back complete from tracking_events.
    """
    mode = telemetry_mode(telemetry)
    with get_conn() as conn:
        with conn.cursor() as cur:
            resolved, trailer_id = _trailer_for_reference(cur, ref)

            cur.execute("""
                SELECT t.id, t.status, t.eta, t.priority, t.load_type,
                       s.id, s.po_id, s.carrier, s.tracking_number, s.expected_arrival,
                       da.dock_id, da.status, da.docked_at,
                       o.name, o.latitude, o.longitude, d.name, d.latitude, d.longitude,
                       t.direction, s.outbound_order_id, ob.customer_name, ob.status
                FROM trailers t
                LEFT JOIN shipments s ON s.id = t.shipment_id
                LEFT JOIN outbound_orders ob ON ob.id = s.outbound_order_id
                LEFT JOIN dock_assignments da ON da.trailer_id=t.id
                     AND da.status IN ('ASSIGNED','CONFIRMED')
                LEFT JOIN locations o ON o.id = s.origin_location_id
                LEFT JOIN locations d ON d.id = s.destination_location_id
                WHERE t.id=%s
            """, (trailer_id,))
            r = cur.fetchone()

            cur.execute("""SELECT latitude, longitude, recorded_at, eta_estimate
                           FROM tracking_events WHERE trailer_id=%s
                           ORDER BY recorded_at""", (trailer_id,))
            breadcrumbs = [{"latitude": _f(x[0]), "longitude": _f(x[1]),
                            "recorded_at": _iso(x[2]), "eta_estimate": _iso(x[3])}
                           for x in cur.fetchall()]

            # The goods movement is emitted against the RECEIPT/ISSUE, not the
            # trailer (redis-contract.md §3: entity_type 'goods_receipt' /
            # 'goods_issue'), so filtering on entity_type='trailer' alone left
            # GOODS_RECEIVED out of the timeline entirely -- and the tracker's
            # final "Delivered" milestone keys off exactly that event, so it
            # could never light up however far the delivery actually got.
            cur.execute("""
                SELECT event_type, created_at, payload FROM event_log
                WHERE (entity_type='trailer' AND entity_id=%s)
                   OR (entity_type='goods_receipt' AND entity_id IN (
                          SELECT id FROM goods_receipts WHERE trailer_id=%s))
                   OR (entity_type='goods_issue' AND entity_id IN (
                          SELECT id FROM goods_issues WHERE trailer_id=%s))
                ORDER BY created_at
            """, (trailer_id, trailer_id, trailer_id))
            timeline = [{"event_type": x[0], "at": _iso(x[1]),
                         "summary": (x[2] or {}).get("summary")} for x in cur.fetchall()]
            if mode == "collapsed":
                timeline = collapse_telemetry(timeline)
        conn.rollback()

    direction = r[19] or "INBOUND"
    # v7: the two directions genuinely finish at different points, so they need
    # different scales. An inbound trailer's story ends when it clears our gate
    # -- that IS 100% of what we track. An outbound one is only ~85% done at
    # that moment, because the customer has not been delivered to yet. Sharing
    # one scale would either declare an outbound truck complete while it is
    # still driving, or leave every inbound truck permanently stuck at 85%.
    if direction == "OUTBOUND":
        progress = {"EN_ROUTE": 25, "ARRIVED": 45, "DOCKED": 60, "LOADED": 75,
                    "DEPARTED": 90, "DELIVERED": 100}.get(r[1], 10)
    else:
        progress = {"EN_ROUTE": 40, "ARRIVED": 70, "DOCKED": 85,
                    "UNLOADED": 95, "DEPARTED": 100}.get(r[1], 10)

    return {
        "reference": ref,
        "resolved_as": resolved,
        "direction": direction,
        "trailer": {"id": r[0], "status": r[1], "eta": _iso(r[2]), "priority": r[3],
                    "load_type": r[4], "direction": direction},
        "shipment": {"id": r[5], "po_id": r[6], "carrier": r[7], "tracking_number": r[8],
                     "expected_arrival": _iso(r[9]), "direction": direction},
        "outbound_order": ({"id": r[20], "customer_name": r[21], "status": r[22]}
                           if r[20] else None),
        "dock": {"dock_id": r[10], "status": r[11], "docked_at": _iso(r[12])} if r[10] else None,
        "origin": {"name": r[13], "latitude": _f(r[14]), "longitude": _f(r[15])},
        "destination": {"name": r[16], "latitude": _f(r[17]), "longitude": _f(r[18])},
        "current_position": breadcrumbs[-1] if breadcrumbs else None,
        "breadcrumbs": breadcrumbs,
        "timeline": timeline,
        "delivery_progress_pct": progress,
    }


@app.get("/map/trailers", tags=["dashboard"])
def map_trailers():
    """Live positions + trail for every trailer still in play."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.status, t.eta, t.priority, s.po_id, s.carrier,
                       da.dock_id,
                       te.latitude, te.longitude,
                       o.latitude, o.longitude, d.latitude, d.longitude,
                       t.direction, s.outbound_order_id, ob.customer_name
                FROM trailers t
                LEFT JOIN shipments s ON s.id=t.shipment_id
                LEFT JOIN outbound_orders ob ON ob.id = s.outbound_order_id
                LEFT JOIN dock_assignments da ON da.trailer_id=t.id
                     AND da.status IN ('ASSIGNED','CONFIRMED')
                LEFT JOIN locations o ON o.id=s.origin_location_id
                LEFT JOIN locations d ON d.id=s.destination_location_id
                LEFT JOIN LATERAL (
                    SELECT latitude, longitude FROM tracking_events
                    WHERE trailer_id=t.id ORDER BY recorded_at DESC LIMIT 1
                ) te ON TRUE
                -- v6: an unloaded trailer is still in the yard, so it stays on
                -- the map until it clears the gate.
                -- v7: an outbound trailer stays on the map PAST the gate, until
                -- it is DELIVERED -- the drive to the customer is the part of an
                -- outbound journey a customer actually wants to watch.
                WHERE t.status <> 'DELIVERED'
                  AND NOT (t.status = 'DEPARTED' AND t.direction = 'INBOUND')
            """)
            trailers = [{
                "id": r[0], "status": r[1], "eta": _iso(r[2]), "priority": r[3],
                "po_id": r[4], "carrier": r[5], "dock_id": r[6],
                "latitude": _f(r[7]), "longitude": _f(r[8]),
                # An outbound leg runs the other way down the same line, so the
                # map's arrow has to be drawn from the endpoints swapped.
                "origin": {"latitude": _f(r[11] if r[13] == "OUTBOUND" else r[9]),
                           "longitude": _f(r[12] if r[13] == "OUTBOUND" else r[10])},
                "destination": {"latitude": _f(r[9] if r[13] == "OUTBOUND" else r[11]),
                                "longitude": _f(r[10] if r[13] == "OUTBOUND" else r[12])},
                "direction": r[13], "outbound_order_id": r[14], "customer_name": r[15],
            } for r in cur.fetchall()]

            cur.execute("""SELECT id, name, location_type, latitude, longitude
                           FROM locations WHERE latitude IS NOT NULL""")
            locations = [{"id": r[0], "name": r[1], "type": r[2],
                          "latitude": _f(r[3]), "longitude": _f(r[4])}
                         for r in cur.fetchall()]
        conn.rollback()
    return {"trailers": trailers, "locations": locations}


# ─────────────────────────────────────────────
# Traceability -- cross-entity timeline for one PO
# ─────────────────────────────────────────────

@app.get("/traceability/{po_id}", tags=["dashboard"])
def traceability(po_id: str, telemetry: str = "collapsed"):
    """
    The full story of one PO, across every entity it touched.

    event_log stores events per entity, so a PO's history is scattered across
    its requisition, shipments, trailers, goods receipts, invoices, match
    results, exceptions and payments. This gathers all of those IDs first, then
    pulls their events in one pass -- which is what the Traceability screen and
    the exception root-cause chain both need, and what no per-domain endpoint
    can answer.

    `telemetry=collapsed` (default) folds runs of TRAILER_LOCATION_UPDATED into
    one row each. Uncollapsed, a single PO's audit trail is ~680 events of
    which ~660 are GPS pings, and it grows for as long as the truck drives --
    an unbounded response carrying a fixed amount of information. Pass
    `telemetry=full` for the raw trail.
    """
    mode = telemetry_mode(telemetry)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, requisition_id, status, supplier_id, material_id,
                                  qty, unit_price, created_at
                           FROM purchase_orders WHERE id=%s""", (po_id,))
            po = cur.fetchone()
            if po is None:
                raise HTTPException(404, f"purchase_order {po_id} not found")

            related: list[tuple[str, str]] = [("purchase_order", po_id)]
            if po[1]:
                related.append(("requisition", po[1]))

            cur.execute("SELECT id FROM shipments WHERE po_id=%s", (po_id,))
            shipment_ids = [r[0] for r in cur.fetchall()]
            related += [("shipment", s) for s in shipment_ids]

            if shipment_ids:
                cur.execute("SELECT id FROM trailers WHERE shipment_id = ANY(%s)",
                            (shipment_ids,))
                trailer_ids = [r[0] for r in cur.fetchall()]
                related += [("trailer", t) for t in trailer_ids]
                if trailer_ids:
                    cur.execute("SELECT id FROM dock_assignments WHERE trailer_id = ANY(%s)",
                                (trailer_ids,))
                    related += [("dock_assignment", r[0]) for r in cur.fetchall()]

            cur.execute("SELECT id FROM goods_receipts WHERE po_id=%s", (po_id,))
            related += [("goods_receipt", r[0]) for r in cur.fetchall()]

            cur.execute("SELECT id FROM invoices WHERE po_id=%s", (po_id,))
            invoice_ids = [r[0] for r in cur.fetchall()]
            related += [("invoice", i) for i in invoice_ids]

            cur.execute("SELECT id FROM match_results WHERE po_id=%s", (po_id,))
            match_ids = [r[0] for r in cur.fetchall()]
            related += [("match_result", m) for m in match_ids]

            if match_ids:
                cur.execute("SELECT id FROM exceptions WHERE match_result_id = ANY(%s)",
                            (match_ids,))
                related += [("exception", r[0]) for r in cur.fetchall()]
            if invoice_ids:
                cur.execute("SELECT id FROM payments WHERE invoice_id = ANY(%s)",
                            (invoice_ids,))
                related += [("payment", r[0]) for r in cur.fetchall()]

            entity_types = [t for t, _ in related]
            entity_ids = [i for _, i in related]
            cur.execute("""
                SELECT entity_type, entity_id, event_type, payload, created_at
                FROM event_log
                WHERE (entity_type, entity_id) IN (
                    SELECT unnest(%s::text[]), unnest(%s::text[])
                )
                ORDER BY created_at, id
            """, (entity_types, entity_ids))
            timeline = [{
                "entity_type": r[0], "entity_id": r[1], "event_type": r[2],
                "summary": (r[3] or {}).get("summary"), "payload": r[3],
                "at": _iso(r[4]),
            } for r in cur.fetchall()]
            if mode == "collapsed":
                timeline = collapse_telemetry(timeline)
        conn.rollback()

    return {
        "purchase_order": {"id": po[0], "requisition_id": po[1], "status": po[2],
                           "supplier_id": po[3], "material_id": po[4],
                           "qty": _f(po[5]), "unit_price": _f(po[6]),
                           "created_at": _iso(po[7])},
        "related_entities": [{"entity_type": t, "entity_id": i} for t, i in related],
        "timeline": timeline,
    }


# ─────────────────────────────────────────────
# Model performance (eval harness output)
# ─────────────────────────────────────────────

@app.get("/kpi/model-performance", tags=["dashboard"])
def model_performance():
    """
    Serves the latest eval run. Judged criterion: accuracy/precision/recall/F1.
    Returns 404 until backend/eval/run_eval.py has been run at least once --
    better an honest "not measured yet" than a fabricated number.
    """
    if not EVAL_RESULTS_PATH.exists():
        raise HTTPException(
            404,
            "no eval run found. Run: ./.venv/bin/python backend/eval/run_eval.py",
        )
    return json.loads(EVAL_RESULTS_PATH.read_text())


# ─────────────────────────────────────────────
# WebSocket -- live deltas
# ─────────────────────────────────────────────

class Hub:
    """Fan-out to every connected dashboard client."""

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.inbox: queue.Queue = queue.Queue(maxsize=1000)

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)
        log.info("client connected (%d total)", len(self.clients))

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)
        log.info("client disconnected (%d left)", len(self.clients))

    async def broadcast(self, message: dict):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


hub = Hub()


# ── v8: the public tracker's own rail ──────────────────────────────────────
#
# The customer portal cannot ride /ws/dashboard. Two reasons, either fatal:
#
#   1. AUTH. /track/{ref} is deliberately public (auth.PUBLIC_PREFIXES), and
#      the customer it is built for has no account. /ws/dashboard closes 1008
#      without a token, so the portal would have live updates only for staff --
#      i.e. for the one audience that has a control tower already.
#   2. SCOPE. /ws/dashboard is the everything-firehose: purchase orders,
#      invoices, supplier scores, payments, exceptions. Pushing that into a
#      browser opened by someone outside the company is a leak even if the
#      screen never renders it -- it is in the tab, readable from the console.
#
# So this rail is filtered twice over: to ONE trailer, and to the events a
# consignment's own timeline already shows. What goes down the wire is a bare
# "something changed" tick with no ids and no payload -- the client re-reads
# GET /track/{ref}, which stays the single authority on what a customer may
# see. Nothing here can therefore disclose more than the REST endpoint does.

TRACK_EVENT_TYPES = {
    "TRAILER_DEPARTED", "TRAILER_LOCATION_UPDATED", "ETA_UPDATED",
    "TRAILER_ARRIVED", "DOCK_ASSIGNED", "DOCK_REASSIGNED", "DOCK_DELAYED",
    "TRAILER_DOCKED", "GOODS_RECEIVED", "GOODS_ISSUED", "TRAILER_EXITED",
}


class TrackHub:
    """Fan-out to public tracker clients, each pinned to one trailer."""

    def __init__(self):
        self.clients: dict[WebSocket, str] = {}

    async def connect(self, ws: WebSocket, trailer_id: str):
        await ws.accept()
        self.clients[ws] = trailer_id
        log.info("tracker connected for %s (%d total)", trailer_id, len(self.clients))

    def disconnect(self, ws: WebSocket):
        if self.clients.pop(ws, None) is not None:
            log.info("tracker disconnected (%d left)", len(self.clients))

    async def broadcast(self, message: dict):
        if message.get("event_type") not in TRACK_EVENT_TYPES or not self.clients:
            return

        # Two ways an event names its trailer. Trailer-lifecycle events carry it
        # as the entity itself; the rest (GOODS_RECEIVED is a goods_receipt,
        # DOCK_ASSIGNED is a dock_assignment -- redis-contract.md §4) carry it
        # in payload.trailer_id. Without the second case the customer never
        # hears about the two milestones they care most about: a bay allocated
        # and the delivery booked in.
        subject = message.get("entity_id") if message.get("entity_type") == "trailer" else None
        if subject is None:
            payload = message.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = None
            if isinstance(payload, dict):
                subject = payload.get("trailer_id")
        if subject is None:
            return

        tick = {"type": "update", "event_type": message.get("event_type"),
                "timestamp": message.get("timestamp")}
        dead = []
        for ws, trailer_id in list(self.clients.items()):
            if trailer_id != subject:
                continue
            try:
                await ws.send_json(tick)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


track_hub = TrackHub()


def _consumer_thread():
    """
    Runs the dashboard-ws consumer group (allowed_event_types=None -- the one
    group that gets everything, per redis-contract.md §5) on its own thread and
    connection, and hands each event to the asyncio side through a queue.

    The handler does no domain writes; consume() still records the
    processed_events claim, which is what makes the group's position durable.
    """
    def handler(conn, fields):
        try:
            hub.inbox.put_nowait({
                "event_id": fields.get("event_id"),
                "event_type": fields.get("event_type"),
                "entity_type": fields.get("entity_type"),
                "entity_id": fields.get("entity_id"),
                "timestamp": fields.get("timestamp"),
                "payload": fields.get("payload"),
            })
        except queue.Full:
            # A slow or absent UI must never stall the event pipeline. Clients
            # re-read state over REST on connect, so dropping a delta here is
            # recoverable by design.
            log.warning("websocket inbox full; dropping event %s", fields.get("event_id"))

    while True:
        try:
            with get_conn() as conn:
                consume(conn, "dashboard-ws", "dashboard-ws-1", handler,
                        allowed_event_types=None)
        except Exception:
            log.exception("dashboard-ws consumer crashed; restarting in 2s")
            import time as _t
            _t.sleep(2)


async def _pump():
    """Drain the thread-safe queue onto the event loop and broadcast."""
    loop = asyncio.get_running_loop()
    while True:
        message = await loop.run_in_executor(None, hub.inbox.get)
        await hub.broadcast(message)
        # One consumer group feeds both rails. A second group reading the same
        # stream for the tracker would be a second processed_events claim on
        # every event for no gain -- the filtering is per-client anyway.
        await track_hub.broadcast(message)


@app.on_event("startup")
async def _startup():
    threading.Thread(target=_consumer_thread, daemon=True, name="dashboard-ws").start()
    asyncio.create_task(_pump())
    log.info("dashboard-ws consumer + pump started")


@app.websocket("/ws/dashboard")
async def ws_dashboard(ws: WebSocket, token: str = ""):
    """
    Forwards every event envelope verbatim.

    Client contract (README §5): call GET /dashboard/overview, /yard-status and
    /purchase-orders ONCE on connect for current state, then apply these
    messages as deltas. Never reconstruct state from this stream alone.

    AUTH: the token arrives as a `?token=` query parameter, not a header --
    the browser WebSocket API cannot set request headers, so this is the only
    way a browser client can present one. The HTTP auth middleware in
    shared/api.py does not see WebSocket scopes, so the check happens here.
    Rejected before accept(), with close code 1008 (policy violation), so an
    unauthenticated client never joins the broadcast hub at all.
    """
    try:
        user = decode_token(token)
    except HTTPException as exc:
        await ws.close(code=1008, reason=exc.detail)
        return

    await hub.connect(ws)
    try:
        await ws.send_json({"type": "hello",
                            "user": {"id": user.id, "name": user.name, "role": user.role},
                            "note": "load current state over REST, then apply these as deltas"})
        while True:
            # Keeps the connection open; inbound client messages are ignored.
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:
        hub.disconnect(ws)


@app.websocket("/ws/track/{ref}")
async def ws_track(ws: WebSocket, ref: str):
    """
    v8 -- live deltas for ONE consignment, for the public customer tracker.

    Public, exactly as GET /track/{ref} is: someone tracking a delivery has no
    account to authenticate with. See TrackHub above for why that is safe here
    and would not be on /ws/dashboard.

    Client contract, same as the dashboard's: this is a "re-read now" tick, not
    state. Messages are {"type":"update","event_type":...,"timestamp":...} with
    no ids and no payload, so a client that ignored GET /track/{ref} could not
    build a picture from the stream even if it tried.

    An unknown reference is closed with 1008 and the reason text, rather than
    left open forever consuming a slot on a truck that does not exist.
    """
    loop = asyncio.get_running_loop()

    def resolve():
        with get_conn() as conn:
            with conn.cursor() as cur:
                _, trailer_id = _trailer_for_reference(cur, ref)
            conn.rollback()
        return trailer_id

    try:
        # get_conn/psycopg are blocking; the pool must not be touched from the
        # event loop thread or one slow lookup stalls every other client's
        # deltas. This is the only blocking call on the path -- after it, the
        # connection does nothing but receive keepalives.
        trailer_id = await loop.run_in_executor(None, resolve)
    except HTTPException as exc:
        # close() before accept() is a 403 handshake rejection, which is the
        # honest answer for a reference that resolves to nothing.
        await ws.close(code=1008, reason=exc.detail)
        return
    except Exception:
        log.exception("tracker socket could not resolve '%s'", ref)
        await ws.close(code=1011, reason="lookup failed")
        return

    await track_hub.connect(ws, trailer_id)
    try:
        await ws.send_json({"type": "hello", "trailer_id": trailer_id,
                            "note": "load current state from GET /track/{ref}, "
                                    "then re-read on each update"})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        track_hub.disconnect(ws)
    except Exception:
        track_hub.disconnect(ws)
