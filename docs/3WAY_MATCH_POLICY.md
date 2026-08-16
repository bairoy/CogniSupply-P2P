# 3-Way Match Policy — LOCKED

Core principle, non-negotiable: **the pay/don't-pay decision is
deterministic and auditable, never an LLM judgment call.** AI's role is
around this engine (OCR extraction, anomaly flagging, explanation) —
never inside it. This matches enterprise practice (Dynamics 365's
tolerance-policy model) and the earlier research on why neurosymbolic/
deterministic 3-way match is what survives an audit, not a probabilistic
"looks fine, pay it."

## Scope of this document

This policy runs **only once both a `goods_receipts` row and an
`invoices` row exist for the same `po_id`.** Checking for that pairing —
an invoice can legitimately arrive before the trailer unloads, and vice
versa — is `match-worker`'s job per `api-contract.md`, and happens
*before* this policy is invoked, not within it. If the pairing isn't
complete yet, `match-worker` does nothing and waits for the other
trigger; it must never interpret a missing goods receipt as `QTY_MISMATCH`
or any other exception. This policy assumes that wait is already over.

## Inputs (all from `schema.sql`)

- `purchase_orders`: `qty`, `unit_price`, `status`
- `goods_receipts`: `qty_received` (the E2-owned record of what actually arrived)
- `invoices`: `qty_invoiced`, `unit_price_invoiced`, `po_id`

## Hard rules (checked before any tolerance math)

1. **`invoices.po_id IS NULL`** → `EXCEPTION`, `exception_type = 'MISSING_PO'`. (`po_id` is nullable in the locked schema specifically to make this scenario representable — an invoice can arrive with no PO reference.)
2. **`purchase_orders.status` already `'MATCHED'` or `'CLOSED'`** for this `po_id` → `EXCEPTION`, `exception_type = 'DUPLICATE_INVOICE'`. (Prevents paying the same PO twice.)

## Tolerance evaluation (only if hard rules pass)

Quantity is checked against what was **received** (`goods_receipts`),
not what was ordered — that's the correct 3-way-match definition: the
PO authorizes, the receipt confirms physical reality, the invoice bills
against that reality. Price is checked against the PO, since receipts
don't carry price.

```
qty_variance_pct   = |invoice.qty_invoiced - goods_receipt.qty_received| / goods_receipt.qty_received
price_variance_pct = |invoice.unit_price_invoiced - po.unit_price| / po.unit_price

QTY_TOLERANCE   = 2%   (tight — quantity mismatches are usually real errors, not rounding)
PRICE_TOLERANCE = 3%   (looser — covers rounding/FX/minor negotiated adjustments)
```

## Decision, in this exact order

```
1. po_id IS NULL                          → EXCEPTION / MISSING_PO
2. PO already MATCHED or CLOSED           → EXCEPTION / DUPLICATE_INVOICE
3. goods_receipt.qty_received <= 0        → EXCEPTION / OTHER  (guards the division below; a
                                             zero/negative receipt is a data problem, not a
                                             quantity variance — never divide by it)
4. po.unit_price <= 0                     → EXCEPTION / OTHER  (same reasoning, guards the
                                             price-variance division)
5. qty_variance_pct > QTY_TOLERANCE        → EXCEPTION / QTY_MISMATCH
6. price_variance_pct > PRICE_TOLERANCE    → EXCEPTION / PRICE_MISMATCH
7. otherwise                               → APPROVED
```

Steps 3 and 4 exist because `qty_variance_pct` and `price_variance_pct`
are computed by dividing by `goods_receipt.qty_received` and
`po.unit_price` respectively — checked *before* that division, not
after, so the worker never crashes on a division by zero (or a negative
value that would silently produce a nonsensical variance).

`reason` (on both `match_results` and, if applicable, `exceptions`)
always states the actual numbers, never just the verdict — e.g.
`"qty variance 10.0% (received 500, invoiced 550) exceeds 2% tolerance"`.
This is the explainability the whole point of a deterministic engine
buys you — use it, don't just log PASS/FAIL.

## Event sequence (matches `api-contract.md` exactly, no changes)

`MATCH_COMPLETED` always, entity_type=`match_result`. Then, depending on
outcome: `PAYMENT_APPROVED` (entity_type=`payment`) or
`EXCEPTION_CREATED` (entity_type=`exception`) — one transaction, one commit.

## v8 — `match_results.ai_narration` (narration, not decision)

A fourth column now sits beside `status` and `reason`: the same verdict written
up as prose for the audit log, from `shared/llm.write_match_reasoning()`.

**It changes nothing in this document.** The decision procedure above is
unaltered, `match_policy.py` imports no model, and the narration is generated
*after* `evaluate()` has already returned — it is handed the finished verdict
and the numbers behind it. Ordering matters and is enforced by construction:
there is no path from the narration back into the outcome.

- `status` and `reason` remain authoritative and are what every consumer keys
  on. If the prose ever disagrees with `reason`, **`reason` wins**.
- It is `NULL` for rows matched before v8 (no backfill — inventing prose for a
  decision the model never saw would put words in the auditor's mouth) and
  whenever no provider key is configured.
- The call is capped at 12s and falls back to a deterministic sentence built
  from `decision.reason`, so match-worker behaves identically with and without
  an API key.

This is the same boundary `write_supplier_reasoning()` sits on, and the reason
this policy survives an audit: a probabilistic "looks fine, pay it" does not,
so the probabilistic part never touches the decision.

## Extensibility note (not built now, but doesn't require a schema change later)

Tiered tolerance by material category (precision goods tighter, commodity
goods looser) is achievable via `materials.metadata->>'category'` —
already a JSONB column in the locked schema — without touching `schema.sql`.
Not implemented for Tier 1; flagged here so it's a deliberate later
decision, not a surprise schema request.
