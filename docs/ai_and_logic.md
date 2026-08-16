# AI and Logic: The Defensibility Strategy

A major risk in AI hackathons is building a system that hallucinates financial or logistical decisions. This codebase was specifically architected to prevent that, making it highly defensible in an enterprise context.

## 1. Where AI is Used (`shared/llm.py`)
We use Claude/OpenAI for tasks they excel at: understanding unstructured data.
- **NLP Requisitions:** A user types "Need 300 mtr of alluminium extrusion profile." The LLM parses this into structured JSON — `{material_id: "MAT-010", qty: 300, uom: "meter"}` — matching against the real catalogue, absorbing the typo, and raising an *ambiguity* rather than guessing when the request is under-specified.
- **Invoice OCR:** The LLM uses Vision capabilities to look at an image of a supplier invoice and extract the PO number, quantity, and total amount, along with confidence scores.
- **Supplier narration:** After a mathematical formula decides which supplier is best, the LLM writes a human-readable explanation of the math so a manager can quickly approve it.
- **Match narration (v8):** After the deterministic 3-way match has returned its
  verdict, `write_match_reasoning()` writes that verdict up as an audit note and
  stores it in `match_results.ai_narration`. It is handed the finished decision
  and the numbers behind it. It is a **second rendering of the decision, never a
  second decision** — `status` and `reason` stay authoritative, and if the prose
  ever disagreed with `reason`, `reason` wins.

## 2. Where AI is FORBIDDEN (`shared/match_policy.py` & `shared/procurement_scoring.py`)
- **3-Way Matching:** The decision to pay an invoice is **100% deterministic code**. It uses standard accounting logic (Quantity variance <= 2%, Price variance <= 3%). An LLM is never allowed to say "This price looks close enough, go ahead and pay it." 
- **Supplier Scoring:** The choice of which supplier gets a ₹1,00,00,000 order is based on a weighted arithmetic formula over 5 real columns (Price, Quality, Reliability, Lead Time, Risk).
- **Predictive invoice risk (v8):** `GET /dashboard/supplier-risk` forecasts which
  open POs are most likely to invoice badly — and it is a *smoothed base rate*,
  not a model. Every input is returned with every score so any number can be
  re-derived by hand, and the response reports how thin the evidence is
  (`confidence`, sample sizes) rather than asserting a bare percentage.

**Interview Tip (Crucial):**
If a judge asks, *"How do you prevent the AI from making bad financial decisions?"*
**Your Answer:** *"By design, the AI is completely isolated from the decision boundary. AI is used as a highly capable data extractor (OCR, NLP) and a narrator. But the actual business logic—the 3-way match, the dock door scheduling, the supplier scoring—is pure deterministic code. Our system relies on AI for usability, but falls back on math for auditability."*

## 3. Graceful Degradation
The `llm.py` module is designed so that if the API keys are missing, or
Anthropic/OpenAI servers go down, the system doesn't crash. Every one of the
four tasks falls back to a **deterministic** result and reports which path ran
via `used_ai`. The match narration's fallback, for instance, is built from
`decision.reason` — which already states the actual numbers — so the panel gets
a genuinely useful sentence rather than a placeholder. The demo will never fail
live on stage due to a network timeout.

The narration call runs *inside* match-worker's transaction, so it is capped at
`MATCH_NARRATION_TIMEOUT_SECONDS` (12s): the worst case is a plainer note, never
a stalled consumer or an unmatched invoice.

## 4. Proving the Claim (v8)
Saying "AI stays outside the decision boundary" is cheap; `backend/eval/run_eval.py`
measures it.

- The **match classifier** is scored against the fault the seeder *injected*
  (`scenario`), never against `ground_truth.json`'s own `expected_match_status`
  — that field is written by the same `evaluate()` call the suite grades, so
  scoring against it returns F1 = 1.00 by construction.
- The **NLP parser** is scored against 30 hand-labelled phrasings in
  `backend/eval/requisitions.json`, deliberately *not* drawn from seeded text
  (every seeded requisition uses one template, so scoring on it would measure
  one sentence sixty times).
- The harness records whether each case reached the live model or the fallback
  stub, because a score that silently blends the two measures neither.
- **OCR reports `not_measured`.** Nothing renders invoice images yet, so there
  is no scan to read. An honest gap beats a fabricated score.
