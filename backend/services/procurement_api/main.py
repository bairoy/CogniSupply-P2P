"""
Procurement API (PR2). Owns: requisitions, supplier_recommendations,
purchase_orders, invoices, exceptions, payments.

Reads goods_receipts; never writes it. That table belongs to Yard API.

AI boundary, restated because it is the point of the whole design:
  - requisition parsing and invoice OCR are model calls (extraction),
  - supplier scoring is arithmetic and the model only narrates it,
  - the 3-way match decision happens in match-worker with no model call at all.

Run:  uvicorn services.procurement_api.main:app --port 8002 --reload  (from backend/)
"""

import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

# .env must load BEFORE event_bus is imported -- it reads REDIS_URL at module
# import time, so a later load would be ignored.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT.parent / ".env")

from event_bus import publish_to_redis, record_event  # noqa: E402
from shared import llm  # noqa: E402
from shared.api import create_app  # noqa: E402
from shared.auth import (  # noqa: E402
    PERM_EXCEPTION_ASSIGN,
    PERM_EXCEPTION_RESOLVE,
    PERM_INVOICE_WRITE,
    PERM_PAYMENT_WRITE,
    PERM_PROCUREMENT_WRITE,
    AuthUser,
    current_user,
    require,
)
from shared.db import get_conn  # noqa: E402
from shared.ids import next_id  # noqa: E402
from shared.procurement_scoring import score_suppliers  # noqa: E402

app = create_app(
    "CogniSupply P2P — Procurement API (PR2)",
    description=(
        "End-to-end autonomous procure-to-pay. Conversational requisition "
        "intake, AI supplier selection, OCR invoice capture, exception "
        "resolution and payment."
    ),
)

INVOICE_DIR = Path(os.environ.get("INVOICE_DIR", BACKEND_ROOT.parent / "data" / "invoices"))
INVOICE_DIR.mkdir(parents=True, exist_ok=True)


def _iso(v):
    return v.isoformat() if v else None


def _f(v):
    return float(v) if v is not None else None


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class RequisitionRequest(BaseModel):
    raw_text: str = Field(examples=["We need 500 meters of industrial aluminium tubing "
                                    "delivered to the Bhiwandi plant by next Friday"])
    # v5: DEPRECATED and ignored -- the requester is the authenticated caller.
    # Kept in the model so pre-auth callers do not break on an extra field.
    requested_by: Optional[str] = Field(
        default=None, deprecated=True,
        description="Ignored since v5 -- the authenticated user is the requester.",
    )


class ChatTurn(BaseModel):
    role: str = Field(examples=["user"])
    content: str


class ChatRequest(BaseModel):
    message: str = Field(examples=["I need 500 units of aluminium tubing"])
    history: list[ChatTurn] = Field(default_factory=list)
    requested_by: Optional[str] = Field(
        default=None, deprecated=True,
        description="Ignored since v5 -- the authenticated user is the requester.",
    )


class InvoiceJSONRequest(BaseModel):
    po_id: Optional[str] = Field(default=None, examples=["PO-1001"])
    qty_invoiced: float = Field(examples=[500])
    unit_price_invoiced: float = Field(examples=[1000.00])
    tax: float = 0
    ocr_raw: dict = Field(default_factory=dict)


class ResolveRequest(BaseModel):
    resolution: str = Field(examples=["APPROVE"], description="APPROVE | REJECT")
    # v5: DEPRECATED and ignored. The person taking responsibility for an
    # override is now the authenticated caller, taken from the bearer token --
    # a client-supplied actor id would be trivially spoofable, which defeats
    # the point of recording who approved the money. Still accepted so
    # pre-auth callers do not break on an unexpected field.
    resolved_by: Optional[str] = Field(
        default=None, deprecated=True,
        description="Ignored since v5 -- the authenticated user is the resolver.",
    )
    notes: Optional[str] = Field(default=None)


class AssignRequest(BaseModel):
    assigned_to: str = Field(examples=["USR-003"])


def _catalogue(cur):
    cur.execute("SELECT id, name, uom, metadata FROM materials ORDER BY id")
    materials = [{"id": r[0], "name": r[1], "uom": r[2], "metadata": r[3] or {}}
                 for r in cur.fetchall()]
    cur.execute("SELECT id, name FROM locations WHERE location_type='WAREHOUSE' ORDER BY id")
    locations = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
    return materials, locations


# ─────────────────────────────────────────────
# Requisition intake
# ─────────────────────────────────────────────

def _write_requisition(conn, cur, parsed, raw_text, requested_by, used_ai):
    req_id = next_id(cur, "REQ")
    parsed_json = parsed.model_dump()
    parsed_json["ai_available"] = used_ai
    cur.execute(
        """INSERT INTO requisitions (id, requested_by, raw_text, parsed, status)
           VALUES (%s,%s,%s,%s,'PARSED')""",
        (req_id, requested_by, raw_text, json.dumps(parsed_json)),
    )
    payload = {
        "summary": f"{req_id} raised: {parsed.qty} {parsed.uom} {parsed.material_name}",
        "material_id": parsed.material_id, "qty": parsed.qty,
        "confidence": parsed.confidence, "ai_available": used_ai,
    }
    event_id, created_at = record_event(conn, "requisition", req_id,
                                        "REQUISITION_CREATED", payload)
    conn.commit()
    publish_to_redis(conn, event_id, "requisition", req_id,
                     "REQUISITION_CREATED", payload, created_at)
    return req_id, parsed_json


@app.post("/requisitions", status_code=201, tags=["procurement"],
          dependencies=[Depends(require(PERM_PROCUREMENT_WRITE))])
def create_requisition(body: RequisitionRequest, actor: AuthUser = Depends(current_user)):
    """Single-shot NLP intake. The LLM call happens inside this handler."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            materials, locations = _catalogue(cur)
        parsed, used_ai = llm.parse_requisition(
            body.raw_text, materials=materials, locations=locations,
            today=date.today().isoformat(),
        )
        with conn.cursor() as cur:
            req_id, parsed_json = _write_requisition(
                conn, cur, parsed, body.raw_text, actor.id, used_ai
            )
    return {"id": req_id, "parsed": parsed_json, "status": "PARSED",
            "ai_available": used_ai}


@app.post("/requisitions/chat", tags=["procurement"],
          dependencies=[Depends(require(PERM_PROCUREMENT_WRITE))])
def requisition_chat(body: ChatRequest, actor: AuthUser = Depends(current_user)):
    """
    Conversational intake -- the brief's "conversational NLP chatbot".

    Nothing is written until the parse is unambiguous. While the model still
    has open questions it returns them and the caller answers, so a half-
    understood request never becomes a requisition row.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            materials, locations = _catalogue(cur)

        history = [{"role": t.role, "content": t.content} for t in body.history]
        parsed, used_ai = llm.parse_requisition(
            body.message, materials=materials, locations=locations,
            today=date.today().isoformat(), history=history,
        )

        if parsed.ambiguities:
            return {
                "status": "clarifying",
                "questions": parsed.ambiguities,
                "draft": parsed.model_dump(),
                "ai_available": used_ai,
                "history": history + [
                    {"role": "user", "content": body.message},
                    {"role": "assistant", "content": " ".join(parsed.ambiguities)},
                ],
            }

        with conn.cursor() as cur:
            req_id, parsed_json = _write_requisition(
                conn, cur, parsed, body.message, actor.id, used_ai
            )
    return {"status": "parsed", "id": req_id, "parsed": parsed_json,
            "ai_available": used_ai}


@app.get("/requisitions/{req_id}", tags=["procurement"])
def get_requisition(req_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT r.id, r.requested_by, u.name, r.raw_text, r.parsed, r.status,
                          r.created_at
                   FROM requisitions r LEFT JOIN users u ON u.id = r.requested_by
                   WHERE r.id=%s""",
                (req_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"requisition {req_id} not found")
            requisition = {
                "id": row[0], "requested_by": row[1], "requested_by_name": row[2],
                "raw_text": row[3], "parsed": row[4], "status": row[5],
                "created_at": _iso(row[6]),
            }

            cur.execute(
                """SELECT sr.supplier_id, s.name, sr.price_score, sr.quality_score,
                          sr.lead_time_score, sr.reliability_score, sr.risk_score,
                          sr.overall_score, sr.rank, sr.recommended,
                          sr.quoted_unit_price, sr.quoted_lead_time_days, sr.reasoning
                   FROM supplier_recommendations sr
                   LEFT JOIN suppliers s ON s.id = sr.supplier_id
                   WHERE sr.requisition_id=%s ORDER BY sr.rank""",
                (req_id,),
            )
            recs = [{
                "supplier_id": r[0], "supplier_name": r[1], "price_score": _f(r[2]),
                "quality_score": _f(r[3]), "lead_time_score": _f(r[4]),
                "reliability_score": _f(r[5]), "risk_score": _f(r[6]),
                "overall_score": _f(r[7]), "rank": r[8], "recommended": r[9],
                "quoted_unit_price": _f(r[10]), "quoted_lead_time_days": _f(r[11]),
                "reasoning": r[12],
            } for r in cur.fetchall()]

            cur.execute("SELECT id FROM purchase_orders WHERE requisition_id=%s", (req_id,))
            po = cur.fetchone()
        conn.rollback()

    return {"requisition": requisition, "recommendations": recs,
            "purchase_order_id": po[0] if po else None}


# ─────────────────────────────────────────────
# Supplier selection -> PO
# ─────────────────────────────────────────────

@app.post("/requisitions/{req_id}/select-supplier", status_code=201, tags=["procurement"],
          dependencies=[Depends(require(PERM_PROCUREMENT_WRITE))])
def select_supplier(req_id: str):
    """
    AI scores every candidate supplier and auto-creates the PO.

    All candidates are persisted, not just the winner -- that is what lets the
    demo answer "why this supplier" with the actual numbers. Two events, one
    transaction.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT raw_text, parsed, status FROM requisitions WHERE id=%s", (req_id,)
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"requisition {req_id} not found")
            raw_text, parsed, status = row
            if status == "CONVERTED":
                raise HTTPException(409, f"requisition {req_id} is already converted")

            material_id = (parsed or {}).get("material_id")
            if not material_id:
                raise HTTPException(
                    422, f"requisition {req_id} has no resolved material_id; "
                         "clarify it before selecting a supplier"
                )

            cur.execute("SELECT name, uom, metadata FROM materials WHERE id=%s", (material_id,))
            mat = cur.fetchone()
            if mat is None:
                raise HTTPException(404, f"material {material_id} not found")
            mat_name, uom, mat_meta = mat
            # Fallback list price in INR, for a material seeded without one.
            base_price = float((mat_meta or {}).get("base_price", 8000.0))

            # Candidates: every supplier that can plausibly serve this material.
            cur.execute(
                """SELECT id, name, reliability_score, avg_lead_time_days, quality_score,
                          risk_score, metadata
                   FROM suppliers ORDER BY id"""
            )
            suppliers = [{
                "id": r[0], "name": r[1], "reliability_score": _f(r[2]),
                "avg_lead_time_days": _f(r[3]), "quality_score": _f(r[4]),
                "risk_score": _f(r[5]),
                "price_multiplier": float((r[6] or {}).get("price_multiplier", 1.0)),
            } for r in cur.fetchall()]

        scored = score_suppliers(candidates=suppliers, base_price=base_price)[:4]

        reasonings, used_ai = llm.write_supplier_reasoning(
            requisition_text=raw_text, candidates=[s.as_dict() for s in scored]
        )

        with conn.cursor() as cur:
            for s, reasoning in zip(scored, reasonings):
                sr_id = next_id(cur, "SR")
                cur.execute(
                    """INSERT INTO supplier_recommendations
                       (id, requisition_id, supplier_id, price_score, quality_score,
                        lead_time_score, reliability_score, risk_score, overall_score,
                        rank, recommended, quoted_unit_price, quoted_lead_time_days, reasoning)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (sr_id, req_id, s.supplier_id, s.price_score, s.quality_score,
                     s.lead_time_score, s.reliability_score, s.risk_score, s.overall_score,
                     s.rank, s.recommended, s.quoted_unit_price, s.quoted_lead_time_days,
                     reasoning),
                )

            winner = scored[0]
            qty = Decimal(str((parsed or {}).get("qty") or 0))
            po_id = next_id(cur, "PO")
            cur.execute(
                """INSERT INTO purchase_orders
                   (id, requisition_id, supplier_id, material_id, qty, unit_price,
                    delivery_location_id, expected_delivery, terms, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s, now() + (%s || ' days')::interval, %s,'CREATED')""",
                (po_id, req_id, winner.supplier_id, material_id, qty,
                 winner.quoted_unit_price,
                 (parsed or {}).get("delivery_location_id") or "LOC-001",
                 str(int(winner.quoted_lead_time_days)),
                 json.dumps({"payment_terms": "NET30", "currency": "INR", "incoterm": "DAP"})),
            )
            cur.execute("UPDATE requisitions SET status='CONVERTED' WHERE id=%s", (req_id,))

        rec_payload = {
            "summary": f"{len(scored)} suppliers scored for {req_id}; "
                       f"{winner.supplier_name} recommended",
            "recommended_supplier_id": winner.supplier_id,
            "recommended_supplier_name": winner.supplier_name,
            "overall_score": winner.overall_score, "ai_available": used_ai,
        }
        po_payload = {
            "summary": f"{po_id} raised with {winner.supplier_name} for "
                       f"{qty} {uom} {mat_name}",
            "supplier_id": winner.supplier_id, "supplier_name": winner.supplier_name,
            "material_id": material_id, "qty": float(qty),
            "unit_price": float(winner.quoted_unit_price),
        }
        ev1 = record_event(conn, "requisition", req_id, "SUPPLIER_RECOMMENDED", rec_payload)
        ev2 = record_event(conn, "purchase_order", po_id, "PO_CREATED", po_payload)
        conn.commit()
        publish_to_redis(conn, ev1[0], "requisition", req_id,
                         "SUPPLIER_RECOMMENDED", rec_payload, ev1[1])
        publish_to_redis(conn, ev2[0], "purchase_order", po_id,
                         "PO_CREATED", po_payload, ev2[1])

    return {
        "purchase_order_id": po_id,
        "ai_available": used_ai,
        "recommendations": [
            {**s.as_dict(), "reasoning": r} for s, r in zip(scored, reasonings)
        ],
    }


# ─────────────────────────────────────────────
# Invoices
# ─────────────────────────────────────────────

def _insert_invoice(conn, cur, *, po_id, qty, unit_price, tax, ocr_raw,
                    confidence, document_path):
    total = (Decimal(str(qty)) * Decimal(str(unit_price)) + Decimal(str(tax))
             ).quantize(Decimal("0.01"))
    inv_id = next_id(cur, "INV")
    cur.execute(
        """INSERT INTO invoices (id, po_id, qty_invoiced, unit_price_invoiced, tax, total,
                                 ocr_confidence, ocr_raw, document_path)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (inv_id, po_id, qty, unit_price, tax, total, confidence,
         json.dumps(ocr_raw), document_path),
    )
    return inv_id, total


def _publish_invoice_events(conn, inv_id, po_id, total, confidence, supplier_hint):
    inv_payload = {
        "summary": f"{inv_id} received from {supplier_hint} (total {total})",
        "po_id": po_id, "total": float(total), "ocr_confidence": confidence,
    }
    ocr_payload = {
        "summary": f"OCR extracted {inv_id} at "
                   f"{(confidence or 0):.0%} confidence",
        "ocr_confidence": confidence, "po_id": po_id,
    }
    ev1 = record_event(conn, "invoice", inv_id, "INVOICE_RECEIVED", inv_payload)
    ev2 = record_event(conn, "invoice", inv_id, "OCR_COMPLETED", ocr_payload)
    conn.commit()
    publish_to_redis(conn, ev1[0], "invoice", inv_id, "INVOICE_RECEIVED", inv_payload, ev1[1])
    publish_to_redis(conn, ev2[0], "invoice", inv_id, "OCR_COMPLETED", ocr_payload, ev2[1])


@app.post("/invoices", status_code=201, tags=["procurement"],
          dependencies=[Depends(require(PERM_INVOICE_WRITE))])
def create_invoice_json(body: InvoiceJSONRequest):
    """
    Structured invoice intake (Tier 1). For the real OCR path -- an actual
    invoice image -- use POST /invoices/ocr.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if body.po_id:
                cur.execute("SELECT 1 FROM purchase_orders WHERE id=%s", (body.po_id,))
                if cur.fetchone() is None:
                    raise HTTPException(404, f"purchase_order {body.po_id} not found")
            confidence = body.ocr_raw.get("confidence", 1.0)
            inv_id, total = _insert_invoice(
                conn, cur, po_id=body.po_id, qty=body.qty_invoiced,
                unit_price=body.unit_price_invoiced, tax=body.tax,
                ocr_raw={**body.ocr_raw, "engine": "structured_json"},
                confidence=confidence, document_path=None,
            )
        _publish_invoice_events(conn, inv_id, body.po_id, total, confidence,
                                body.ocr_raw.get("vendor", "supplier"))
    return {"id": inv_id, "total": float(total), "ocr_confidence": confidence}


@app.post("/invoices/ocr", status_code=201, tags=["procurement"],
          dependencies=[Depends(require(PERM_INVOICE_WRITE))])
async def create_invoice_ocr(
    file: UploadFile = File(..., description="Invoice image (PNG/JPEG)"),
    po_id_hint: Optional[str] = Form(default=None),
):
    """
    Real OCR intake: the invoice image goes to the configured vision model
    (Anthropic or OpenAI, see shared/llm.py) and the extracted fields are what
    get billed against.

    The PO reference comes from the DOCUMENT, not from the caller -- an invoice
    that shows no PO number produces po_id = NULL, which is a genuine
    MISSING_PO scenario the match worker will raise. po_id_hint is only used
    to record what the sender claimed, for comparison.
    """
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(422, "empty file")

    extracted, used_ai = llm.extract_invoice(
        image_bytes, media_type=file.content_type or "image/png"
    )
    if extracted is None:
        raise HTTPException(
            503,
            "OCR unavailable (no ANTHROPIC_API_KEY / OPENAI_API_KEY, or "
            "extraction failed). "
            "Use POST /invoices with structured JSON instead.",
        )

    stored = INVOICE_DIR / f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{file.filename}"
    stored.write_bytes(image_bytes)

    with get_conn() as conn:
        with conn.cursor() as cur:
            po_id = extracted.po_reference
            if po_id:
                cur.execute("SELECT 1 FROM purchase_orders WHERE id=%s", (po_id,))
                if cur.fetchone() is None:
                    # The document referenced a PO we do not have. That is a
                    # real anomaly -- record it and let the matcher handle it
                    # rather than silently dropping the reference.
                    po_id = None

            confidences = list(extracted.field_confidence.values())
            overall = round(sum(confidences) / len(confidences), 3) if confidences else None

            inv_id, total = _insert_invoice(
                conn, cur, po_id=po_id, qty=extracted.qty_invoiced,
                unit_price=extracted.unit_price_invoiced, tax=extracted.tax,
                ocr_raw={**extracted.model_dump(),
                         "engine": f"{llm.ai_provider() or 'none'}-vision",
                         "po_id_hint": po_id_hint,
                         "po_reference_resolved": po_id is not None},
                confidence=overall, document_path=str(stored),
            )
        _publish_invoice_events(conn, inv_id, po_id, total, overall, extracted.supplier_name)

    return {"id": inv_id, "po_id": po_id, "total": float(total),
            "ocr_confidence": overall, "extracted": extracted.model_dump(),
            "ai_available": used_ai}


@app.get("/invoices/{invoice_id}", tags=["procurement"])
def get_invoice(invoice_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT i.id, i.po_id, i.qty_invoiced, i.unit_price_invoiced, i.tax,
                          i.total, i.ocr_confidence, i.ocr_raw, i.document_path, i.received_at,
                          po.qty, po.unit_price, po.supplier_id, s.name,
                          gr.qty_received, gr.id,
                          mr.id, mr.status, mr.reason,
                          e.id, e.exception_type, e.status, e.severity, e.impact_amount,
                          p.id, p.status, p.amount
                   FROM invoices i
                   LEFT JOIN purchase_orders po ON po.id = i.po_id
                   LEFT JOIN suppliers s ON s.id = po.supplier_id
                   LEFT JOIN goods_receipts gr ON gr.po_id = i.po_id
                   LEFT JOIN match_results mr ON mr.invoice_id = i.id
                   LEFT JOIN exceptions e ON e.match_result_id = mr.id
                   LEFT JOIN payments p ON p.invoice_id = i.id
                   WHERE i.id=%s""",
                (invoice_id,),
            )
            r = cur.fetchone()
            if r is None:
                raise HTTPException(404, f"invoice {invoice_id} not found")
        conn.rollback()

    po_total = (_f(r[10]) or 0) * (_f(r[11]) or 0)
    inv_total = _f(r[5]) or 0
    return {
        "invoice": {
            "id": r[0], "po_id": r[1], "qty_invoiced": _f(r[2]),
            "unit_price_invoiced": _f(r[3]), "tax": _f(r[4]), "total": inv_total,
            "ocr_confidence": _f(r[6]), "ocr_raw": r[7],
            "has_document": r[8] is not None, "received_at": _iso(r[9]),
        },
        "purchase_order": {"id": r[1], "qty": _f(r[10]), "unit_price": _f(r[11]),
                           "supplier_id": r[12], "supplier_name": r[13],
                           "expected_total": round(po_total, 2)} if r[1] else None,
        "goods_receipt": {"id": r[15], "qty_received": _f(r[14])} if r[15] else None,
        "match_result": {"id": r[16], "status": r[17], "reason": r[18]} if r[16] else None,
        "exception": {"id": r[19], "exception_type": r[20], "status": r[21],
                      "severity": r[22], "impact_amount": _f(r[23])} if r[19] else None,
        "payment": {"id": r[24], "status": r[25], "amount": _f(r[26])} if r[24] else None,
        "variance": round(inv_total - po_total, 2) if r[1] else None,
    }


@app.get("/invoices/{invoice_id}/document", tags=["procurement"])
def get_invoice_document(invoice_id: str):
    """Serves the stored invoice image -- the "View Original Scan" action."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT document_path FROM invoices WHERE id=%s", (invoice_id,))
            row = cur.fetchone()
        conn.rollback()
    if row is None:
        raise HTTPException(404, f"invoice {invoice_id} not found")
    if not row[0]:
        raise HTTPException(404, f"invoice {invoice_id} has no stored document "
                                 "(it arrived as structured JSON, not a scan)")
    path = Path(row[0])
    if not path.exists():
        raise HTTPException(410, "document file is no longer on disk")
    return FileResponse(path)


# ─────────────────────────────────────────────
# Purchase orders
# ─────────────────────────────────────────────

class ConfirmPORequest(BaseModel):
    confirmed_delivery_date: Optional[datetime] = None
    notes: Optional[str] = None
    confirmed_by: Optional[str] = Field(
        default=None,
        description="Who at the supplier accepted. Free text -- suppliers are "
                    "not users of this system, so this is a name on a document, "
                    "not a users.id.",
    )


@app.post("/purchase-orders/{po_id}/confirm", tags=["procurement"], status_code=200,
          dependencies=[Depends(require(PERM_PROCUREMENT_WRITE))])
def confirm_purchase_order(po_id: str, body: ConfirmPORequest = ConfirmPORequest()):
    """
    The supplier accepts the PO -- v7, and the step the end-to-end workflow
    always described but the system never recorded.

    Before this, a PO went from CREATED straight to a shipment materialising,
    with the supplier's acceptance existing nowhere: not as a state, not as an
    event, not on the timeline. That gap is why nothing could react to "the
    supplier committed" as distinct from "we raised an order" -- and reacting to
    exactly that is the supplier-agent worker's entire job.

    The acceptance detail goes into terms.supplier_confirmation rather than
    earning columns. Nothing queries or indexes it (README §10's rule), and
    `terms` is already the PO's JSONB home for contract-shaped facts.

    Does NOT touch expected_delivery. That column means "the supplier's
    contractual promised date, set once at PO creation" (README §4), and
    overwriting it with a confirmation date would destroy the promised-vs-actual
    variance the KPI query depends on. A supplier who confirms a different date
    has their date recorded inside terms, where it can be compared, not merged
    into the number it should be compared against.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT po.status, po.supplier_id, s.name, po.qty, po.unit_price,
                          po.expected_delivery, m.name
                   FROM purchase_orders po
                   LEFT JOIN suppliers s ON s.id = po.supplier_id
                   LEFT JOIN materials m ON m.id = po.material_id
                   WHERE po.id=%s""",
                (po_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"purchase_order {po_id} not found")
            status, supplier_id, supplier_name, qty, unit_price, expected_delivery, material = row
            if status != "CREATED":
                raise HTTPException(
                    409, f"purchase_order {po_id} is {status}, expected CREATED")

            confirmation = {
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "confirmed_by": body.confirmed_by or supplier_name,
                "confirmed_delivery_date": _iso(body.confirmed_delivery_date),
                "notes": body.notes,
            }
            cur.execute(
                """UPDATE purchase_orders
                   SET status='CONFIRMED',
                       terms = COALESCE(terms,'{}'::jsonb)
                               || jsonb_build_object('supplier_confirmation', %s::jsonb),
                       updated_at=now()
                   WHERE id=%s""",
                (json.dumps(confirmation), po_id),
            )

        # A confirmed date that slips past the promised one is the first honest
        # signal of a late delivery, available before a truck has even moved.
        slipped = bool(
            body.confirmed_delivery_date and expected_delivery
            and body.confirmed_delivery_date > expected_delivery
        )
        payload = {
            "summary": f"{supplier_name or supplier_id} confirmed {po_id}"
                       + (f" ({qty:g} × {material})" if qty and material else ""),
            "po_id": po_id,
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "status": "CONFIRMED",
            "qty": _f(qty),
            "unit_price": _f(unit_price),
            "expected_delivery": _iso(expected_delivery),
            "confirmed_delivery_date": _iso(body.confirmed_delivery_date),
            "delivery_date_slipped": slipped,
        }
        event_id, created_at = record_event(conn, "purchase_order", po_id,
                                            "PO_CONFIRMED", payload)
        conn.commit()
        publish_to_redis(conn, event_id, "purchase_order", po_id,
                         "PO_CONFIRMED", payload, created_at)

    return {"id": po_id, "status": "CONFIRMED", "supplier_name": supplier_name,
            "confirmation": confirmation}


@app.get("/purchase-orders", tags=["procurement"])
def list_purchase_orders(status: Optional[str] = None, limit: int = 100):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT po.id, po.status, po.qty, po.unit_price, po.expected_delivery,
                          po.created_at, s.name, m.name, po.supplier_id, po.material_id
                   FROM purchase_orders po
                   LEFT JOIN suppliers s ON s.id = po.supplier_id
                   LEFT JOIN materials m ON m.id = po.material_id
                   WHERE (%s IS NULL OR po.status = %s)
                   ORDER BY po.created_at DESC LIMIT %s""",
                (status, status, limit),
            )
            rows = cur.fetchall()
        conn.rollback()
    return {"purchase_orders": [{
        "id": r[0], "status": r[1], "qty": _f(r[2]), "unit_price": _f(r[3]),
        "value": round((_f(r[2]) or 0) * (_f(r[3]) or 0), 2),
        "expected_delivery": _iso(r[4]), "created_at": _iso(r[5]),
        "supplier_name": r[6], "material_name": r[7],
        "supplier_id": r[8], "material_id": r[9],
    } for r in rows]}


@app.get("/purchase-orders/{po_id}", tags=["procurement"])
def get_purchase_order(po_id: str):
    """README §9's key reference query, as an endpoint."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT po.id, loc.name, po.expected_delivery, po.status, po.qty,
                          po.unit_price, s.name, m.name, po.terms, po.created_at,
                          shp.id, shp.expected_arrival, shp.status, shp.carrier,
                          gr.id, gr.qty_received, gr.received_at,
                          inv.id, inv.qty_invoiced, inv.total,
                          mr.id, mr.status, mr.reason,
                          ex.id, ex.exception_type, ex.status, ex.severity
                   FROM purchase_orders po
                   LEFT JOIN locations loc ON loc.id = po.delivery_location_id
                   LEFT JOIN suppliers s ON s.id = po.supplier_id
                   LEFT JOIN materials m ON m.id = po.material_id
                   LEFT JOIN shipments shp ON shp.po_id = po.id
                   LEFT JOIN goods_receipts gr ON gr.po_id = po.id
                   LEFT JOIN invoices inv ON inv.po_id = po.id
                   LEFT JOIN match_results mr ON mr.po_id = po.id
                   LEFT JOIN exceptions ex ON ex.match_result_id = mr.id
                   WHERE po.id=%s""",
                (po_id,),
            )
            rows = cur.fetchall()
            if not rows:
                raise HTTPException(404, f"purchase_order {po_id} not found")
        conn.rollback()

    r = rows[0]
    return {
        "purchase_order": {
            "id": r[0], "delivery_point": r[1], "expected_delivery": _iso(r[2]),
            "status": r[3], "qty": _f(r[4]), "unit_price": _f(r[5]),
            "supplier_name": r[6], "material_name": r[7], "terms": r[8],
            "created_at": _iso(r[9]),
        },
        "shipment": {"id": r[10], "expected_arrival": _iso(r[11]), "status": r[12],
                     "carrier": r[13]} if r[10] else None,
        "goods_receipt": {"id": r[14], "qty_received": _f(r[15]),
                          "received_at": _iso(r[16])} if r[14] else None,
        "invoices": [{"id": x[17], "qty_invoiced": _f(x[18]), "total": _f(x[19])}
                     for x in rows if x[17]],
        "match_results": [{"id": x[20], "status": x[21], "reason": x[22]}
                          for x in rows if x[20]],
        "exceptions": [{"id": x[23], "exception_type": x[24], "status": x[25],
                        "severity": x[26]} for x in rows if x[23]],
    }


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

@app.get("/exceptions", tags=["procurement"])
def list_exceptions(status: str = "OPEN", limit: int = 100):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT e.id, e.exception_type, e.status, e.severity, e.impact_amount,
                          e.assigned_to, u.name, e.created_at, e.resolution_notes,
                          mr.id, mr.po_id, mr.reason, mr.invoice_id
                   FROM exceptions e
                   LEFT JOIN users u ON u.id = e.assigned_to
                   LEFT JOIN match_results mr ON mr.id = e.match_result_id
                   WHERE (%s = 'ALL' OR e.status = %s)
                   ORDER BY CASE e.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                            WHEN 'medium' THEN 2 ELSE 3 END,
                            e.created_at DESC
                   LIMIT %s""",
                (status, status, limit),
            )
            rows = cur.fetchall()
        conn.rollback()
    return {"exceptions": [{
        "id": r[0], "exception_type": r[1], "status": r[2], "severity": r[3],
        "impact_amount": _f(r[4]), "assigned_to": r[5], "assigned_to_name": r[6],
        "created_at": _iso(r[7]), "resolution_notes": r[8],
        "match_result_id": r[9], "po_id": r[10], "reason": r[11], "invoice_id": r[12],
    } for r in rows]}


@app.post("/exceptions/{exception_id}/assign", tags=["procurement"],
          dependencies=[Depends(require(PERM_EXCEPTION_ASSIGN))])
def assign_exception(exception_id: str, body: AssignRequest):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM exceptions WHERE id=%s", (exception_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"exception {exception_id} not found")
            cur.execute("SELECT name FROM users WHERE id=%s", (body.assigned_to,))
            user = cur.fetchone()
            if user is None:
                raise HTTPException(404, f"user {body.assigned_to} not found")
            cur.execute("UPDATE exceptions SET assigned_to=%s WHERE id=%s",
                        (body.assigned_to, exception_id))

        payload = {"summary": f"{exception_id} assigned to {user[0]}",
                   "assigned_to": body.assigned_to, "assigned_to_name": user[0]}
        ev = record_event(conn, "exception", exception_id, "EXCEPTION_ASSIGNED", payload)
        conn.commit()
        publish_to_redis(conn, ev[0], "exception", exception_id,
                         "EXCEPTION_ASSIGNED", payload, ev[1])
    return {"id": exception_id, "assigned_to": body.assigned_to}


@app.post("/exceptions/{exception_id}/resolve", tags=["procurement"],
          dependencies=[Depends(require(PERM_EXCEPTION_RESOLVE))])
def resolve_exception(exception_id: str, body: ResolveRequest,
                      actor: AuthUser = Depends(current_user)):
    """
    Human review closes the loop. APPROVE also creates the payment and moves
    the PO to MATCHED -- the deterministic engine refused to auto-approve, so
    a person takes responsibility for the override and it is recorded.

    v5: "a person" is now the authenticated caller, not a body field. The
    override is the single most consequential manual action in PR2, so the
    name attached to it has to be one the caller proved, not one they typed.
    """
    resolved_by = actor.id
    resolution = body.resolution.upper()
    if resolution not in ("APPROVE", "REJECT"):
        raise HTTPException(422, "resolution must be APPROVE or REJECT")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT e.status, mr.po_id, mr.invoice_id, i.total
                   FROM exceptions e
                   LEFT JOIN match_results mr ON mr.id = e.match_result_id
                   LEFT JOIN invoices i ON i.id = mr.invoice_id
                   WHERE e.id=%s""",
                (exception_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"exception {exception_id} not found")
            status, po_id, invoice_id, total = row
            if status != "OPEN":
                raise HTTPException(409, f"exception {exception_id} is already {status}")

            new_status = "APPROVED" if resolution == "APPROVE" else "REJECTED"
            cur.execute(
                """UPDATE exceptions SET status=%s, resolution_notes=%s, resolved_at=now()
                   WHERE id=%s""",
                (new_status, body.notes, exception_id),
            )
            cur.execute(
                """UPDATE match_results SET resolved_at=now()
                   WHERE id = (SELECT match_result_id FROM exceptions WHERE id=%s)""",
                (exception_id,),
            )

            pay_id = None
            if resolution == "APPROVE" and invoice_id:
                pay_id = next_id(cur, "PAY")
                cur.execute(
                    """INSERT INTO payments (id, invoice_id, amount, status, approved_by,
                                             approved_at)
                       VALUES (%s,%s,%s,'APPROVED',%s,now())""",
                    (pay_id, invoice_id, total, resolved_by),
                )
                if po_id:
                    cur.execute(
                        "UPDATE purchase_orders SET status='MATCHED', updated_at=now() "
                        "WHERE id=%s", (po_id,),
                    )

        res_payload = {
            "summary": f"{exception_id} {new_status.lower()} by {actor.name}",
            "status": new_status, "resolved_by": resolved_by,
            "resolved_by_name": actor.name,
            "notes": body.notes, "po_id": po_id,
        }
        ev = record_event(conn, "exception", exception_id, "EXCEPTION_RESOLVED", res_payload)
        pay_ev = None
        pay_payload = None
        if pay_id:
            pay_payload = {
                "summary": f"{pay_id} approved for {total} after manual review",
                "invoice_id": invoice_id, "amount": _f(total), "po_id": po_id,
            }
            pay_ev = record_event(conn, "payment", pay_id, "PAYMENT_APPROVED", pay_payload)
        conn.commit()
        publish_to_redis(conn, ev[0], "exception", exception_id,
                         "EXCEPTION_RESOLVED", res_payload, ev[1])
        if pay_ev:
            publish_to_redis(conn, pay_ev[0], "payment", pay_id,
                             "PAYMENT_APPROVED", pay_payload, pay_ev[1])

    return {"id": exception_id, "status": new_status, "payment_id": pay_id}


# ─────────────────────────────────────────────
# Payments
# ─────────────────────────────────────────────

@app.get("/payments", tags=["procurement"])
def list_payments(status: Optional[str] = None, limit: int = 100):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.id, p.invoice_id, p.amount, p.status, p.approved_by,
                          p.approved_at, p.paid_at, p.created_at, i.po_id, s.name
                   FROM payments p
                   LEFT JOIN invoices i ON i.id = p.invoice_id
                   LEFT JOIN purchase_orders po ON po.id = i.po_id
                   LEFT JOIN suppliers s ON s.id = po.supplier_id
                   WHERE (%s IS NULL OR p.status = %s)
                   ORDER BY p.created_at DESC LIMIT %s""",
                (status, status, limit),
            )
            rows = cur.fetchall()
        conn.rollback()
    return {"payments": [{
        "id": r[0], "invoice_id": r[1], "amount": _f(r[2]), "status": r[3],
        "approved_by": r[4], "approved_at": _iso(r[5]), "paid_at": _iso(r[6]),
        "created_at": _iso(r[7]), "po_id": r[8], "supplier_name": r[9],
    } for r in rows]}


@app.post("/payments/{payment_id}/pay", tags=["procurement"],
          dependencies=[Depends(require(PERM_PAYMENT_WRITE))])
def pay(payment_id: str):
    """APPROVED -> PAID, and the PO reaches CLOSED. Closes the P2P loop."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.status, p.amount, i.po_id FROM payments p
                   LEFT JOIN invoices i ON i.id = p.invoice_id WHERE p.id=%s""",
                (payment_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"payment {payment_id} not found")
            status, amount, po_id = row
            if status != "APPROVED":
                raise HTTPException(409, f"payment {payment_id} is {status}, expected APPROVED")

            cur.execute("UPDATE payments SET status='PAID', paid_at=now() WHERE id=%s",
                        (payment_id,))
            if po_id:
                cur.execute(
                    "UPDATE purchase_orders SET status='CLOSED', updated_at=now() WHERE id=%s",
                    (po_id,),
                )

        payload = {"summary": f"{payment_id} paid ({amount})", "amount": _f(amount),
                   "po_id": po_id}
        ev = record_event(conn, "payment", payment_id, "PAYMENT_PAID", payload)
        po_ev = None
        po_payload = None
        if po_id:
            po_payload = {"summary": f"{po_id} -> CLOSED (payment settled)",
                          "status": "CLOSED", "po_id": po_id}
            po_ev = record_event(conn, "purchase_order", po_id, "PO_STATUS_CHANGED", po_payload)
        conn.commit()
        publish_to_redis(conn, ev[0], "payment", payment_id, "PAYMENT_PAID", payload, ev[1])
        if po_ev:
            publish_to_redis(conn, po_ev[0], "purchase_order", po_id,
                             "PO_STATUS_CHANGED", po_payload, po_ev[1])

    return {"id": payment_id, "status": "PAID", "po_id": po_id}
