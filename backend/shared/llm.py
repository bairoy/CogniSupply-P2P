"""
The AI layer. One module, one client, four tasks:

  1. parse_requisition()  -- conversational NLP intake  (PR2 brief item 1)
  2. extract_invoice()    -- vision OCR over an invoice image (PR2 brief item 4)
  3. write_supplier_reasoning() -- narrates an ALREADY-COMPUTED score
  4. write_match_reasoning()    -- narrates an ALREADY-DECIDED 3-way match

WHERE AI IS AND IS NOT USED
---------------------------
AI extracts, classifies and explains. It never decides.

  - Supplier scoring is arithmetic (shared/procurement_scoring.py); the model
    only writes prose about the result.
  - The 3-way match decision is deterministic (shared/match_policy.py) and
    contains no model call at all. write_match_reasoning() is handed the
    finished verdict and writes it up; it cannot change an outcome, and
    match_policy.py does not import this module.

This is the same principle 3WAY_MATCH_POLICY.md states: a probabilistic
"looks fine, pay it" does not survive an audit. Keeping the model outside the
decision boundary is what makes the system defensible.

TWO PROVIDERS, ONE CONTRACT
---------------------------
Either Anthropic or OpenAI can back all three tasks. Which one runs is decided
once, here, from the environment:

  LLM_PROVIDER=anthropic|openai   force a provider (its key must be set)
  LLM_PROVIDER unset              auto: Anthropic if ANTHROPIC_API_KEY is set,
                                  else OpenAI if OPENAI_API_KEY is set

Callers never see the difference. Both paths return the SAME Pydantic models
(ParsedRequisition, OCRInvoice) and the same (result, used_ai) tuple, so no
service has a provider branch in it. Only two details differ under the hood and
both are contained in this module:

  - OpenAI's strict structured-output mode forbids free-form maps, so the
    OCR call uses a mirror model with one named field per confidence score
    (_OpenAIOCRInvoice) and converts back to OCRInvoice.
  - GPT-5 counts reasoning tokens against max_completion_tokens, so the OpenAI
    budget is larger than the Anthropic one.

GRACEFUL DEGRADATION
--------------------
Every function here falls back to a deterministic stub when no provider key is
set or a call fails, and reports which path ran via `ai_available`. A live demo
must never die because of a network blip -- it should visibly degrade instead.
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
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

# max_tokens caps thinking PLUS the response on this model, so it needs
# headroom well above the size of the JSON we expect back.
MAX_TOKENS = 4096

# Measured against the live API on all three tasks (clean parse, deliberately
# ambiguous parse, invoice OCR, narration): gpt-5.4-mini answers in ~1.5s where
# gpt-5 takes 10-18s, with no difference in the extracted fields. Nothing here
# decides anything -- extraction and narration is exactly the work a small model
# is good at -- and requisition intake is a synchronous request an operator sits
# waiting on, so the latency is worth more than the headroom. Set OPENAI_MODEL
# to gpt-5.4 or gpt-5.5 if you want the bigger model anyway.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")

# Same max-tokens rule as above, more so: the GPT-5 family spends reasoning
# tokens out of max_completion_tokens before it emits a single character of
# JSON, and a run that exhausts the budget mid-reasoning returns an EMPTY parse
# rather than an error. Hence the bigger number. Like Opus 5 these models reject
# temperature/top_p, so none are passed.
OPENAI_MAX_TOKENS = 8192

_client = None
_provider: Optional[str] = None
_client_checked = False


def ai_available() -> bool:
    return _get_client()[1] is not None


def ai_provider() -> Optional[str]:
    """"anthropic", "openai", or None when running in fallback mode."""
    return _get_client()[0]


def _resolve_provider() -> Optional[str]:
    """
    Pick the provider from the environment. An explicit LLM_PROVIDER with no
    matching key is a misconfiguration worth shouting about -- we still fall
    back rather than crash, but silently using the other vendor's key would
    hide the mistake until the bill arrived.
    """
    has = {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
    }
    forced = (os.environ.get("LLM_PROVIDER") or "").strip().lower()

    if forced:
        if forced not in has:
            logger.warning("LLM_PROVIDER=%r is not anthropic|openai -- falling back", forced)
            return None
        if not has[forced]:
            logger.warning(
                "LLM_PROVIDER=%s but %s_API_KEY is not set -- NLP/OCR run in "
                "deterministic fallback mode", forced, forced.upper()
            )
            return None
        return forced

    for name in ("anthropic", "openai"):
        if has[name]:
            return name

    logger.warning(
        "neither ANTHROPIC_API_KEY nor OPENAI_API_KEY is set -- NLP/OCR run in "
        "deterministic fallback mode"
    )
    return None


def _get_client():
    """
    Lazily construct the client. Returns (provider, client), both None when no
    provider is usable -- an absent key is a normal state, not an error.
    """
    global _client, _provider, _client_checked
    if _client_checked:
        return _provider, _client
    _client_checked = True

    provider = _resolve_provider()
    if provider is None:
        return None, None

    try:
        if provider == "anthropic":
            import anthropic

            _client = anthropic.Anthropic()
            logger.info("AI layer using anthropic/%s", ANTHROPIC_MODEL)
        else:
            import openai

            _client = openai.OpenAI()
            logger.info("AI layer using openai/%s", OPENAI_MODEL)
        _provider = provider
    except Exception:
        logger.exception("could not construct %s client; falling back", provider)
        _client, _provider = None, None
    return _provider, _client


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
to null and add an ambiguity explaining the choice you could not make, proactively suggesting the closest matches.
- If the requested delivery location does not match an available location, set delivery_location_id to null and add an ambiguity. Proactively suggest the available locations from the list rather than just rejecting it.
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
    provider, client = _get_client()
    if client is None:
        return _fallback_parse(raw_text, materials), False

    catalogue = "\n".join(f"  {m['id']}: {m['name']} (uom: {m['uom']})" for m in materials)
    locs = "\n".join(f"  {loc['id']}: {loc['name']}" for loc in locations)
    system = PARSE_SYSTEM.format(materials=catalogue, locations=locs, today=today)

    messages = []
    for turn in (history or []):
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": raw_text})

    try:
        if provider == "anthropic":
            response = client.messages.parse(
                model=ANTHROPIC_MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                output_format=ParsedRequisition,
            )
            if response.stop_reason == "refusal":
                logger.warning("requisition parse refused; using fallback")
                return _fallback_parse(raw_text, materials), False
            parsed = response.parsed_output
        else:
            # OpenAI has no separate system parameter -- the system prompt is
            # the first message, and history follows it in the same list.
            response = client.chat.completions.parse(
                model=OPENAI_MODEL,
                max_completion_tokens=OPENAI_MAX_TOKENS,
                messages=[{"role": "system", "content": system}] + messages,
                response_format=ParsedRequisition,
            )
            message = response.choices[0].message
            if message.refusal:
                logger.warning("requisition parse refused; using fallback")
                return _fallback_parse(raw_text, materials), False
            parsed = message.parsed

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


class _OpenAIFieldConfidence(BaseModel):
    """One named score per extracted field -- see _OpenAIOCRInvoice."""

    supplier_name: float
    po_reference: float
    qty_invoiced: float
    unit_price_invoiced: float
    tax: float
    total: float


class _OpenAIOCRInvoice(BaseModel):
    """
    OCRInvoice for OpenAI's strict structured-output mode, which forbids
    free-form maps: `dict[str, float]` becomes `additionalProperties: {...}`
    in the JSON schema and the API rejects the request outright. Naming the
    six fields is the same information with a schema the API accepts, and
    _to_ocr_invoice() collapses it back to the shared contract so nothing
    downstream knows which provider produced it.
    """

    supplier_name: str
    po_reference: Optional[str] = Field(
        default=None, description="PO number on the invoice, e.g. PO-1001. "
                                  "null if the invoice shows none."
    )
    qty_invoiced: float
    unit_price_invoiced: float
    tax: float = 0.0
    total: float
    field_confidence: _OpenAIFieldConfidence = Field(
        description="Per-field 0-1 confidence for each field above"
    )

    def _to_ocr_invoice(self) -> OCRInvoice:
        return OCRInvoice(
            supplier_name=self.supplier_name,
            po_reference=self.po_reference,
            qty_invoiced=self.qty_invoiced,
            unit_price_invoiced=self.unit_price_invoiced,
            tax=self.tax,
            total=self.total,
            field_confidence=self.field_confidence.model_dump(),
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
    provider, client = _get_client()
    if client is None:
        return None, False

    b64 = base64.standard_b64encode(image_bytes).decode()
    prompt = "Extract the billing fields from this invoice."

    try:
        if provider == "anthropic":
            response = client.messages.parse(
                model=ANTHROPIC_MODEL,
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
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
                output_format=OCRInvoice,
            )
            if response.stop_reason == "refusal" or response.parsed_output is None:
                logger.warning("invoice OCR returned no parse")
                return None, False
            return response.parsed_output, True

        # OpenAI takes the image as a data: URI rather than a base64 block.
        response = client.chat.completions.parse(
            model=OPENAI_MODEL,
            max_completion_tokens=OPENAI_MAX_TOKENS,
            messages=[
                {"role": "system", "content": OCR_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
            response_format=_OpenAIOCRInvoice,
        )
        message = response.choices[0].message
        if message.refusal or message.parsed is None:
            logger.warning("invoice OCR returned no parse")
            return None, False
        return message.parsed._to_ocr_invoice(), True
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
numbers given. All prices are Indian rupees -- write them with a ₹ prefix \
(₹1,250 or ₹1.2 lakh), never a dollar sign. No preamble, no bullet points."""


def write_supplier_reasoning(*, requisition_text, candidates) -> tuple[list[str], bool]:
    """
    One call for the whole candidate set. Returns (reasonings, used_ai) with
    one string per candidate, in the order given.
    """
    provider, client = _get_client()
    if client is None:
        return [_fallback_reasoning(c) for c in candidates], False

    try:
        payload = json.dumps(candidates, indent=2, default=str)
        user_prompt = (
            f"Requisition: {requisition_text}\n\n"
            f"Scored candidates (rank 1 was selected):\n{payload}\n\n"
            f"Return a JSON array of {len(candidates)} strings, one "
            f"explanation per candidate in the same order. Return only "
            f"the JSON array."
        )

        if provider == "anthropic":
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=MAX_TOKENS,
                system=REASONING_SYSTEM,
                messages=[{"role": "user", "content": user_prompt}],
            )
            if response.stop_reason == "refusal":
                return [_fallback_reasoning(c) for c in candidates], False
            text = next((b.text for b in response.content if b.type == "text"), "")
        else:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                max_completion_tokens=OPENAI_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": REASONING_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
            )
            message = response.choices[0].message
            if message.refusal:
                return [_fallback_reasoning(c) for c in candidates], False
            text = message.content or ""

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
        f"₹{c.get('quoted_unit_price')}. {verdict}"
    )


# ─────────────────────────────────────────────
# 4. Match narration (narration only)
# ─────────────────────────────────────────────

# This call happens INSIDE match-worker's open transaction, on the consumer
# thread, so a hang here stalls the whole 3-way match pipeline rather than one
# request. A hard ceiling is not optional: it is what bounds the blast radius of
# a slow provider to "this narration fell back to the deterministic sentence".
MATCH_NARRATION_TIMEOUT_SECONDS = 12

MATCH_SYSTEM = """You explain a 3-way match result that has ALREADY been decided \
by a deterministic tolerance policy.

The decision is final and is not yours to make, agree with, or question. Never \
write that something "should" be approved or rejected, never suggest a \
different outcome, and never hedge about whether the decision is correct -- \
you are writing the audit note for a conclusion that is already recorded.

Write 2-3 sentences an accounts-payable clerk could paste into an audit log: \
say what was compared, quote the actual numbers given, and state what the \
policy concluded. If it is an exception, say plainly what a person now has to \
check. All amounts are Indian rupees -- write them with a ₹ prefix (₹1,250 or \
₹1.2 lakh), never a dollar sign. No preamble, no bullet points, no headings."""


def write_match_reasoning(*, po_id, decision, supplier_name=None) -> tuple[str, bool]:
    """
    Narrate an already-decided 3-way match. Returns (narration, used_ai).

    `decision` is a shared.match_policy.MatchDecision that has ALREADY been
    computed. Nothing here can change it: the model is handed the verdict and
    the numbers behind it and asked only to write them up. This is the same
    boundary write_supplier_reasoning() sits on, and the reason 3WAY_MATCH
    _POLICY.md survives an audit -- a probabilistic "looks fine, pay it" does
    not, so the probabilistic part never touches the decision.

    Falls back to a deterministic sentence when no provider key is set or the
    call fails, so match-worker behaves identically with and without an API key.
    """
    fallback = _fallback_match_reasoning(po_id, decision, supplier_name)

    provider, client = _get_client()
    if client is None:
        return fallback, False

    try:
        facts = {
            "po_id": po_id,
            "supplier": supplier_name,
            "decision": decision.status,
            "exception_type": decision.exception_type,
            "policy_reason": decision.reason,
            "severity": decision.severity,
            "impact_amount_inr": _num(decision.impact_amount),
            "qty_variance_pct": _num(decision.qty_variance_pct),
            "price_variance_pct": _num(decision.price_variance_pct),
        }
        user_prompt = (
            "Write the audit note for this completed 3-way match.\n\n"
            f"{json.dumps(facts, indent=2, default=str)}\n\n"
            "Return only the note itself, as plain prose."
        )

        if provider == "anthropic":
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=MAX_TOKENS,
                system=MATCH_SYSTEM,
                messages=[{"role": "user", "content": user_prompt}],
                timeout=MATCH_NARRATION_TIMEOUT_SECONDS,
            )
            if response.stop_reason == "refusal":
                return fallback, False
            text = next((b.text for b in response.content if b.type == "text"), "")
        else:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                max_completion_tokens=OPENAI_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": MATCH_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                timeout=MATCH_NARRATION_TIMEOUT_SECONDS,
            )
            message = response.choices[0].message
            if message.refusal:
                return fallback, False
            text = message.content or ""

        text = text.strip()
        # An empty completion is a failure wearing a success's clothes -- the
        # GPT-5 family returns one when reasoning exhausts the token budget.
        # Storing "" would silently blank the panel this feeds.
        if not text:
            return fallback, False
        return text, True
    except Exception:
        logger.exception("match narration failed; using fallback")
        return fallback, False


def _num(value):
    """Decimal -> float for the prompt payload; None stays None."""
    return float(value) if value is not None else None


def _fallback_match_reasoning(po_id, decision, supplier_name) -> str:
    """
    The deterministic note. It is built from `decision.reason`, which already
    states the actual numbers -- so the no-API-key path is a genuinely useful
    sentence rather than a placeholder, and the panel never has a hole in it.
    """
    subject = f"{po_id}" if po_id else "This invoice"
    who = f" from {supplier_name}" if supplier_name else ""
    if decision.status == "APPROVED":
        return (
            f"3-way match on {subject}{who} passed: purchase order, goods "
            f"receipt and invoice agree within policy tolerance. {decision.reason}. "
            f"Approved for payment automatically, with no human review required."
        )
    impact = ""
    if decision.impact_amount is not None:
        impact = f" Amount in dispute: ₹{float(decision.impact_amount):,.2f}."
    return (
        f"3-way match on {subject}{who} failed policy check "
        f"{decision.exception_type}: {decision.reason}.{impact} Raised as a "
        f"{decision.severity or 'medium'}-severity exception for review before payment."
    )
