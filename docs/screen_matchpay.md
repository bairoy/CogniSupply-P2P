# Screen Documentation: MatchPay — Invoice Matching & Settlement

## What This Screen Is For
This is the **PR2 core screen** — it shows every supplier invoice, its 3-way match verdict, and its payment status. It is the screen a finance manager uses to see whether the system auto-approved a payment or escalated an exception.

> **Route:** `/match-pay` (list) and `/match-pay/:invoiceId` (detail)  
> **Who sees it:** Finance managers and Admins  
> **Use case:** PR2 — Procure-to-Pay

---

## Two Modes: List View and Detail View

The screen has two modes, switching based on whether a URL parameter is present:
- `/match-pay` → shows the **Payment Register** (all payments as a table)
- `/match-pay/INV-1042` → shows the **3-Way Match Detail** for that specific invoice

This is a standard master-detail pattern — click a row in the list to open the detail.

---

## Mode 1: Payment Register (List View)

**What it shows:** A table of all payment records with: Payment ID, Invoice ID, PO ID, GRN ID, Supplier name, Amount, Status, Match verdict, and Action.

**The status column** shows whether a payment is:
- `APPROVED` — 3-way match passed, payment is scheduled for autonomous release
- `PAID` — payment has been released (simulator called `POST /payments/{id}/pay`)
- `BLOCKED` — match failed, sent to exceptions queue

**The "Match verdict" column** shows a green "3-Way Verified" badge for APPROVED/PAID payments, and a dash for anything else. This is for the demo — judges can immediately see which invoices went touchless.

**How it works:**
- Calls `GET /payments` on mount to get all payment records.
- Clicking an invoice ID navigates to `/match-pay/{invoiceId}` for the detail view.

---

## Mode 2: 3-Way Match Detail View

This is the most important screen for the PR2 demo. It shows the full reconciliation of three documents side by side.

### Sub-component A: Match Summary / Variance Analysis Panel

**What it shows:** 
- The reason the match passed or failed (a human-readable string from `match_result.reason`).
- A financial reconciliation box showing: PO committed value, Invoice subtotal, and the **variance** between them.
- If it failed, a badge shows the exception type (e.g., `PRICE MISMATCH`) and a link to the exceptions queue.

**The variance math:**
```
PO committed value  = po.qty × po.unit_price
Invoice subtotal    = invoice.qty_invoiced × invoice.unit_price_invoiced
Variance            = Invoice subtotal − PO committed value
```
If variance is positive, the supplier invoiced more than the PO authorized. If it's within the 2%/3% tolerance, it passes. If it exceeds tolerance, it fails and creates an exception.

#### The AI Audit Note (v8)

Below the variance box, this panel renders `match_result.ai_narration` — the
same verdict written up as prose an AP clerk can paste straight into an audit
log. Example, produced live by the running match-worker:

> 3-way match completed for PO-1031, comparing the PO, receipt, and supplier
> invoice for Tata Steel Jamshedpur Works. The policy found a quantity
> mismatch: received 424.0 units versus invoiced 445.2 units, a 5.0% variance,
> while price variance was 0.0%; this exceeded the 2.0% tolerance and the match
> was marked EXCEPTION. AP now needs to check the quantity difference and
> resolve the ₹45,200 impact amount before further processing.

**Three deliberate choices here, and each one is a talking point:**

1. **It is placed *after* the reason line and the variance arithmetic.** The
   record is what the policy computed; the narration explains it. Putting the
   generated text first would make it look like the finding.
2. **It never replaces `reason`.** The panel renders the deterministic
   `match_result.reason` unconditionally. The note is an addition. If the two
   ever disagreed, `reason` wins — the caption on screen says so.
3. **It is null for pre-v8 rows, and the panel simply omits it.** The migration
   deliberately does not backfill: inventing prose for a decision the model
   never saw would put words in the auditor's mouth.

**Interview tip:** *"The LLM is called after `evaluate()` has already returned.
It's handed the finished verdict and the numbers, and asked to write it up. It
cannot reach the decision — `match_policy.py` imports no model at all. And if
the API key is missing or the call times out, we store a deterministic sentence
built from the same numbers, so the panel never has a hole in it."*

---

### Sub-component B: Document Intelligence (OCR) Panel

**What it shows:** The AI extraction confidence score for the invoice, and per-field confidence if available.

**How it works:**
- When the supplier submits an invoice image, the LLM Vision API reads it and returns:
  - An overall confidence score (e.g., 0.97 = 97%)
  - Per-field confidence: `po_number: 0.99`, `qty: 0.94`, `unit_price: 0.91`, etc.
- This panel displays those scores with green checkmarks for scores above 90% and warning icons for lower scores.
- If the invoice was submitted as structured JSON (by the simulator, not OCR), it shows "Received as a structured data feed — no per-field OCR confidence."

**Interview tip:** If asked "How does OCR work in your system?" — *"The supplier sends an invoice image. We call an LLM Vision API (Anthropic Claude or OpenAI GPT-4 Vision). The model returns structured JSON with field values and confidence scores for each field. If a field like the PO number has low confidence, a finance manager knows to double-check it manually before the 3-way match is trusted."*

---

### Sub-component C: Settlement Progress (Timeline)

**What it shows:** A vertical timeline with four steps:
1. **GRN posted** — goods receipt created (from the IoT scanner)
2. **Invoice received** — invoice submitted and OCR-processed
3. **3-way match cleared/failed** — deterministic policy ran
4. **Payment settled/scheduled** — payment status

**Why it's designed this way:** A finance manager reviewing an exception needs to know what the system already did and where it stopped. The timeline makes this obvious — steps with a green check happened automatically, steps with a red X are where it failed, and empty circles are things not yet done.

---

### Sub-component D: 3-Way Match Reconciliation Table

**The star of the screen.** This table puts three documents side by side:

| Source | Reference | Quantity | Unit Price | Subtotal |
|---|---|---|---|---|
| Purchase Order | PO-1042 | 500 | ₹1,200 | ₹6,00,000 |
| Goods Receipt Note (GRN) | GRN-2087 | 510 | n/a | n/a |
| Invoice | INV-4156 | 510 | ₹1,320 | ₹6,73,200 |

Below the table: GST row and Grand Total.

**If the match failed**, the Invoice row is highlighted in a red background so it's immediately visible which document caused the exception.

**The explanatory text at the bottom** is important:
> "Quantity is compared against what was **received**, not ordered — the PO authorises, the receipt confirms physical reality, the invoice bills against that reality. Price is compared against the PO, since receipts carry no price. Tolerances: 2% quantity, 3% price."

**Interview tip on the matching logic:** *"We compare the invoice quantity against the Goods Receipt, not the PO. This is standard accounting practice — what matters is what physically arrived, not what was ordered. If 500 were ordered but 510 arrived (and the invoice bills 510), that's still outside tolerance and creates an exception. The PO only authorizes, the receipt is what happened."*

---

## Data Flow

```
User opens /match-pay/:invoiceId
      │
      ▼
GET /invoices/{id}
Returns a single response with ALL related documents:
  - invoice details + OCR raw data + confidence
  - purchase_order (if linked)
  - goods_receipt (if exists)
  - match_result (if match ran)
  - exception (if match failed)
  - payment (if payment created)
  - variance (pre-computed by the API)
      │
      ▼
Frontend renders each panel using the nested data
No additional API calls needed — the detail endpoint returns the full picture.
```
