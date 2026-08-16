# Screen Documentation: ControlTower — Supply Chain Control Tower

## What This Screen Is For
This is the **executive/admin dashboard** — the high-level health view of the entire procure-to-pay pipeline. A manager opening this screen can answer "Is the system running well?" in under 10 seconds.

> **Route:** `/` (home screen for admins)  
> **Who sees it:** Admins and senior managers  
> **Use case:** Both E2 and PR2 — shows the end-to-end picture

---

## The 7 Main Components

### 1. KPI Tiles (Four Cards Across the Top)

**Four headline metrics, live from the database:**

| KPI | What it measures |
|---|---|
| **First-pass match rate** | What % of invoices passed the 3-way match on the first attempt (no exception raised) |
| **Straight-through processing** | What % of invoices were settled without any human touching them (fully touchless) |
| **Dock utilisation** | What % of dock doors are occupied right now |
| **Human intervention required** | How many exceptions are currently open (need a human to resolve) |

**How it works:**
- All four numbers come from `GET /dashboard/overview`.
- The gateway computes them live from the database — it counts rows, divides them, and returns percentages.
- The `useRefetchOn()` hook watches for events like `MATCH_COMPLETED`, `PAYMENT_APPROVED`, `GOODS_RECEIVED` via WebSocket and re-fetches when any fires.

**Interview tip:** If a judge asks "are these real numbers?" — Yes. These are computed on the fly from actual rows in the database. The seed data has a mix of clean (70–80%) and mismatched (20–30%) invoices, so the first-pass match rate reflects a realistic scenario, not a fabricated 100%.

---

### 2. Business Impact & ROI Section

**What it shows:** The financial value of automation — processing cost avoided, detention avoided, analyst hours freed.

**How it's calculated — and why it's honest:**
The code defines a `ROI` constant at the top of the file with benchmark rates:
- `manualInvoiceCost: 1200` — ₹1,200 is an industry benchmark for the all-in cost of processing one invoice manually (AP analyst time, error correction, follow-ups).
- `detentionPerMinute: 18` — ₹18/minute is a standard demurrage/detention charge.
- `baselineTurnaroundMinutes: 120` — a manually-scheduled yard takes ~2 hours gate-in to gate-out.

**The key integrity choice:** These are labelled **"Estimated processing cost avoided"**, not "savings." The code even has a comment: *"Nobody's bank balance moved."* If the demo run's average turnaround is slower than the 120-minute baseline, the detention tile shows ₹0 and explains why — it never fabricates a saving. A judge can change the ₹1,200 assumption and every number recalculates live.

**Interview tip:** If asked "How did you calculate ROI?" — Say: *"We separated measured volumes from assumed rates. Everything the system processed — touchless invoices, trucks turned, minutes saved — is measured from the actual run. The rates (₹1,200 per invoice, ₹18/min detention) are industry benchmarks we state explicitly on screen. A judge can dispute a rate and every number updates — we don't hide the assumptions inside the output."*

---

### 3. Secondary KPI Strip

Four more time-based metrics:
- **Avg truck turnaround** — gate-in to gate-out, in minutes
- **Avg P2P cycle time** — from PO raised to payment approved, in hours
- **Avg exception resolution time** — how long it takes a human to close an exception (shows "—" until the first one is resolved, which is honest — zero is not the same as instant)
- **Active trailers** — how many trucks are in the yard right now, plus pending invoices count

---

### 4. Pipeline Volume & Health (Funnel)

**What it shows:** A horizontal funnel with counts at each stage of the P2P pipeline.

Stages: Requisition → Sourcing → PO → Transit → Docking → Receiving → Invoice → Match → Payment

**How it works:**
- `GET /dashboard/pipeline` returns a list of stages with a count and optional `delayed` / `exceptions` badges.
- Each stage is rendered as a circle icon with a number above it. If there are delays or exceptions at a stage, colored badges appear below it.
- This view lets a manager see "we have 14 POs in transit but 3 are delayed" at a glance without clicking into any of them.

**Interview tip:** This directly satisfies the P2P analytics dashboard requirement. It shows the pipeline as a funnel — a judge can see exactly where work is piling up.

---

### 5. Live Shipment Map (`TrailerMap` component)

**What it shows:** A Mapbox GL JS map with markers for every active trailer's current GPS position.

**How it works:**
- The `TrailerMap` component is shared and also embedded in the customer-facing Track screen.
- It fetches trailer positions from the yard API and renders markers.
- Clicking a marker shows a popup with the trailer ID and status.

---

### 6. Predictive Invoice Risk (v8)

**What it shows:** The open POs most likely to produce a mismatched invoice,
ranked by the **money** the forecast puts in doubt — not by probability.

| Column | Meaning |
|---|---|
| PO / material | Links to the traceability timeline. An `invoice in` badge means the invoice has already arrived and is awaiting match — *imminent*, not larger |
| Order value | `qty × unit_price` |
| Mismatch risk | The supplier's smoothed exception rate, with a confidence line beneath it |
| Value at risk | `risk × typical severity × order value` |
| Likely issue | That supplier's modal exception type, historically |

**The model, in one block** (`GET /dashboard/supplier-risk`):

```
prior      = (house exception rate + suppliers.risk_score) / 2
score      = (exceptions + k*prior) / (matched + k)        k = 5
confidence = matched / (matched + k)
```

It is the supplier's observed rate pulled toward a prior in proportion to how
little history backs it. Every input comes back with every score, so any number
on screen can be re-derived by hand.

**Why it is built this way — the four things worth defending:**

1. **Smoothing is not decoration.** Suppliers here have between 0 and 8 matched
   invoices. A raw rate would publish a supplier whose only two invoices both
   failed as "100% risk". `k = 5` is the smallest value that stops that.
2. **A supplier with no history scores the prior, not a flattering zero**, and
   reports `observed_rate: null`, `confidence: 0`. The panel prints "no invoice
   history" beside the percentage — because 25% off no evidence and 25% off
   eight invoices are different claims.
3. **The rupee figure is grounded in measurement.** `typical severity` is the
   *measured median* of `exceptions.impact_amount / PO value` across every
   priced exception (≈8.5% here). Without it the only available number is
   `risk × whole PO value`, which asserts the entire order is at stake when the
   typical mismatch disputes a fraction of it — an overstatement of roughly 12×.
4. **The house rate counts the whole `match_results` table**, not the sum of
   per-supplier counts. An invoice arriving with no PO reference
   (`MISSING_PO`) joins to no supplier; excluding it would flatter the average
   every prior is built from. `attributed_to_a_supplier` reports the gap so the
   two figures reconcile on screen.

**Failure behaviour:** this panel is fetched *separately* from the page's main
`Promise.all`. A forecast is the least load-bearing thing on the Control Tower,
so if it fails the panel is absent rather than taking the whole screen down.

**Interview tip:** *"It's a base rate, not a trained model, and the endpoint
says so. On 27 matched invoices anything calling itself ML would be
overclaiming. What it does honestly is rank where a buyer should look first,
and show its working."*

---

### 7. At-Risk Orders Table

**What it shows:** The top exceptions and delayed deliveries that need attention.

**How it works:**
- `GET /dashboard/at-risk` returns a unified list combining:
  - Match exceptions (price/qty mismatches, missing POs)
  - Dock delay alerts
  - Requisitions that have been stuck in `PARSED` status for over 4 hours (approval stall)
- Each row links to the relevant screen: PO-prefixed items link to `/traceability/{po-id}`, TRL-prefixed items link to `/track/{id}`, others link to `/exceptions`.
- This is a **union across domains** — the gateway JOINs the `exceptions` table and the `alerts` table and presents them as one list, sorted by severity. This saves a manager from having to visit multiple tabs to see what's urgent.

---

## Data Flow

```
User opens ControlTower
      │
      ▼
GET /dashboard/overview    → KPIs, basis counts, dock summary
GET /dashboard/pipeline    → stage-by-stage pipeline counts
GET /dashboard/at-risk     → exceptions + delay alerts merged list

GET /dashboard/supplier-risk?limit=5   → predictive risk (fetched separately,
                                          failure = panel hidden, page survives)
      │
      ▼
WebSocket listens for 10+ event types
      │
      ▼
Any relevant event → re-fetch all 3 endpoints → re-render

ROI calculation (pure frontend math, no extra API call):
  invoiceCostAvoided = touchlessInvoices × ₹1,200
  detentionAvoided   = trucksTurned × minutesSavedPerTruck × ₹18
  totalCostAvoided   = invoiceCostAvoided + detentionAvoided
```
