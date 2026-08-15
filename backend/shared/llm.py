"""
The AI layer. One module, one Anthropic client, three tasks:

  1. parse_requisition()  -- conversational NLP intake  (PR2 brief item 1)
  2. extract_invoice()    -- vision OCR over an invoice image (PR2 brief item 4)
  3. write_supplier_reasoning() -- narrates an ALREADY-COMPUTED score

WHERE AI IS AND IS NOT USED
---------------------------
AI extracts, classifies and explains. It never decides.

  - Supplier scoring is arithmetic (shared/procurement_scoring.py); the model
    only writes prose about the result.
  - The 3-way match decision is deterministic (shared/match_policy.py) and
    contains no model call at all.

This is the same principle 3WAY_MATCH_POLICY.md states: a probabilistic
"looks fine, pay it" does not survive an audit. Keeping the model outside the
decision boundary is what makes the system defensible.

GRACEFUL DEGRADATION
--------------------
Every function here falls back to a deterministic stub when ANTHROPIC_API_KEY
is unset or a call fails, and reports which path ran via `ai_available`. A live
demo must never die because of a network blip -- it should visibly degrade
instead.
"""

import base64
import json
import logging
import os
import re
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("llm")

# Claude Opus 5. Thinking is ON by default on this model (adaptive), so no
# `thinking` parameter is passed. Do NOT add temperature/top_p/top_k or
# budget_tokens -- all are rejected with a 400 on this model.
MODEL = "claude-opus-5"

# max_tokens caps thinking PLUS the response on this model, so it needs
# headroom well above the size of the JSON we expect back.
MAX_TOKENS = 4096

_client = None
_client_checked = False


def ai_available() -> bool:
    return _get_client() is not None


def _get_client():
    """Lazily construct the client. Absent key is a normal state, not an error."""
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning(
            "ANTHROPIC_API_KEY not set -- NLP/OCR run in deterministic fallback mode"
        )
        return None
    try:
        import anthropic

        _client = anthropic.Anthropic()
    except Exception:
        logger.exception("could not construct Anthropic client; falling back")
        _client = None
    return _client


# ─────────────────────────────────────────────
# 1. Requisition parsing (conversational NLP)
# ─────────────────────────────────────────────

class ParsedRequisition(BaseModel):
    """Structured output contract for requisition intake (BUILD_PLAN §4.1)."""

    material_id: Optional[str] = Field(
        default=None, description="Material ID from the catalogue, e.g. MAT-001. "
                                  "null if no catalogue item clearly matches."
    )
    material_name: str = Field(description="Material as the user described it")
    qty: float = Field(description="Quantity requested. 0 if not stated.")
    uom: str = Field(default="unit", description="Unit of measure")
    required_date: Optional[str] = Field(
        default=None, description="ISO date (YYYY-MM-DD) if a deadline was stated"
    )
    delivery_location_id: Optional[str] = Field(
        default=None, description="Location ID, e.g. LOC-001"
    )
    confidence: float = Field(description="0-1 confidence in this parse")
    ambiguities: list[str] = Field(
        default_factory=list,
        description="Questions that must be answered before this becomes a PO. "
                    "Empty means ready to convert.",
    )


PARSE_SYSTEM = """You extract structured purchase requisitions from how people \
actually ask for things at work.

Resolve the material against this catalogue. Use the exact ID; never invent one.
{materials}

Delivery locations:
{locations}

Rules:
- If the request does not clearly match one catalogue material, set material_id \
to null and add an ambiguity explaining the choice you could not make.
- If quantity is missing or vague ("some", "a few"), set qty to 0 and add an \
ambiguity.
- Relative dates ("next Friday") resolve against today, {today}.
- ambiguities must be empty ONLY when the requisition could be turned into a \
purchase order with no further questions."""


def parse_requisition(raw_text: str, *, materials, locations, today,
                      history=None) -> tuple[ParsedRequisition, bool]:
    """
    Returns (parsed, used_ai). `history` is prior conversation turns for the
    chat endpoint, so a follow-up answer refines the previous parse instead of
    starting over.
    """
    client = _get_client()
    if client is None:
        return _fallback_parse(raw_text, materials), False

    catalogue = "\n".join(f"  {m['id']}: {m['name']} (uom: {m['uom']})" for m in materials)
    locs = "\n".join(f"  {loc['id']}: {loc['name']}" for loc in locations)

    messages = []
    for turn in (history or []):
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": raw_text})

    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=PARSE_SYSTEM.format(materials=catalogue, locations=locs, today=today),
            messages=messages,
            output_format=ParsedRequisition,
        )
        if response.stop_reason == "refusal":
            logger.warning("requisition parse refused; using fallback")
            return _fallback_parse(raw_text, materials), False
        parsed = response.parsed_output
        if parsed is None:
            return _fallback_parse(raw_text, materials), False
        return parsed, True
    except Exception:
        logger.exception("requisition parse failed; using fallback")
        return _fallback_parse(raw_text, materials), False


def _fallback_parse(raw_text: str, materials) -> ParsedRequisition:
    """
    Deterministic parse: first number is the quantity, best word-overlap match
    is the material. Crude on purpose -- it exists so the system keeps working
    without an API key, and it reports low confidence so the UI can say so.
    """
    qty_match = re.search(r"(\d[\d,]*\.?\d*)", raw_text.replace(",", ""))
    qty = float(qty_match.group(1)) if qty_match else 0.0

    words = set(re.findall(r"[a-z]+", raw_text.lower()))
    best, best_score = None, 0
    for m in materials:
        overlap = len(words & set(re.findall(r"[a-z]+", m["name"].lower())))
        if overlap > best_score:
            best, best_score = m, overlap

    ambiguities = []
    if qty == 0:
        ambiguities.append("Quantity could not be determined from the request.")
    if best is None:
        ambiguities.append("No catalogue material matched the description.")

    return ParsedRequisition(
        material_id=best["id"] if best else None,
        material_name=best["name"] if best else raw_text[:80],
        qty=qty,
        uom=best["uom"] if best else "unit",
        delivery_location_id="LOC-001",
        confidence=0.35 if best else 0.1,
        ambiguities=ambiguities,
    )


# ─────────────────────────────────────────────
# 2. Invoice OCR (vision)
# ─────────────────────────────────────────────

class OCRInvoice(BaseModel):
    """Structured output contract for invoice extraction (BUILD_PLAN §4.2)."""

    supplier_name: str
    po_reference: Optional[str] = Field(
        default=None, description="PO number on the invoice, e.g. PO-1001. "
                                  "null if the invoice shows none."
    )
    qty_invoiced: float
    unit_price_invoiced: float
    tax: float = 0.0
    total: float
    field_confidence: dict[str, float] = Field(
        default_factory=dict,
        description="Per-field 0-1 confidence for each field above",
    )


OCR_SYSTEM = """You read supplier invoices and extract billing fields exactly \
as printed.

- Transcribe what is on the document. Never correct, infer, or complete a value \
that is not there.
- If the invoice shows no PO number, set po_reference to null. A missing PO \
reference is a real business scenario, not a reading error -- do not guess one.
- Give an honest per-field confidence. A smudged or ambiguous figure should \
score low; that score is used downstream to decide whether a human looks at it."""


def extract_invoice(image_bytes: bytes, media_type: str = "image/png"
                    ) -> tuple[OCRInvoice | None, bool]:
    """Vision OCR over an invoice image. Returns (extracted, used_ai)."""
    client = _get_client()
    if client is None:
        return None, False

    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=OCR_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.standard_b64encode(image_bytes).decode(),
                        },
                    },
                    {"type": "text", "text": "Extract the billing fields from this invoice."},
                ],
            }],
            output_format=OCRInvoice,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            logger.warning("invoice OCR returned no parse")
            return None, False
        return response.parsed_output, True
    except Exception:
        logger.exception("invoice OCR failed")
        return None, False


# ─────────────────────────────────────────────
# 3. Supplier reasoning (narration only)
# ─────────────────────────────────────────────

REASONING_SYSTEM = """You explain a supplier selection that has ALREADY been \
decided by a scoring formula.

You are not choosing the supplier and must not second-guess the scores. Write \
2-3 sentences a procurement manager could paste into an approval: name the \
concrete factors that drove the score, in plain language, using the actual \
numbers given. No preamble, no bullet points."""


def write_supplier_reasoning(*, requisition_text, candidates) -> tuple[list[str], bool]:
    """
    One call for the whole candidate set. Returns (reasonings, used_ai) with
    one string per candidate, in the order given.
    """
    client = _get_client()
    if client is None:
        return [_fallback_reasoning(c) for c in candidates], False

    try:
        payload = json.dumps(candidates, indent=2, default=str)
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=REASONING_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Requisition: {requisition_text}\n\n"
                    f"Scored candidates (rank 1 was selected):\n{payload}\n\n"
                    f"Return a JSON array of {len(candidates)} strings, one "
                    f"explanation per candidate in the same order. Return only "
                    f"the JSON array."
                ),
            }],
        )
        if response.stop_reason == "refusal":
            return [_fallback_reasoning(c) for c in candidates], False
        text = next((b.text for b in response.content if b.type == "text"), "")
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return [_fallback_reasoning(c) for c in candidates], False
        reasons = json.loads(match.group(0))
        if len(reasons) != len(candidates):
            return [_fallback_reasoning(c) for c in candidates], False
        return [str(x) for x in reasons], True
    except Exception:
        logger.exception("supplier reasoning failed; using fallback")
        return [_fallback_reasoning(c) for c in candidates], False


def _fallback_reasoning(c) -> str:
    verdict = "Selected as best overall fit." if c.get("rank") == 1 else \
              "Not selected -- lower combined score."
    return (
        f"{c.get('supplier_name')} scores {c.get('overall_score')} overall: "
        f"quality {c.get('quality_score')}, reliability {c.get('reliability_score')}, "
        f"{c.get('quoted_lead_time_days')}-day lead time, unit price "
        f"{c.get('quoted_unit_price')}. {verdict}"
    )
