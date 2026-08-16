# Screen Documentation: Procurement — NLP Requisition & Supplier Selection

## What This Screen Is For
This is where the **autonomous P2P cycle starts**. A procurement officer describes what they need in plain English — a conversational chat interface extracts the structured intent, and an AI-driven supplier selection process picks the best supplier and creates a Purchase Order automatically.

> **Route:** `/procurement`  
> **Who sees it:** Procurement roles and Admins  
> **Use case:** PR2 — requirements #1 (NLP intake), #2 (AI supplier selection), #3 (Auto PO)

---

## The Split-Screen Design

The screen is divided into **two columns side by side**:
- **Left:** The chat conversation — where the user types their request
- **Right:** The structured result — what the AI extracted from the conversation

This layout is intentional. The brief asks for "conversational NLP intake," but a procurement officer's job is not just to chat — it's to verify the machine understood correctly before committing a purchase. Putting the extracted fields on the right means every sentence typed has its consequence immediately visible. If the AI misunderstood the quantity, the officer sees it instantly and corrects it in the next message.

---

## Component 1: The NLP Chat Interface (Left Column)

### How it works:
1. User types a free-form request: *"We need 500 meters of industrial aluminium tubing delivered to the Bhiwandi plant by next Friday"*
2. The message is sent to `POST /requisitions/chat` along with the conversation `history` (so the AI has context).
3. Two outcomes:

**Outcome A — Clarifying questions:**
If the request is ambiguous (e.g., "Raise a requisition for hydraulic seals" — no quantity, no location), the LLM returns clarifying questions:
- *"How many units do you need?"*
- *"Which delivery location?"*
The questions appear as blue message bubbles. The user answers, and the conversation continues. History is passed on every round.

**Outcome B — Parsed:**
Once the AI has enough information, it returns a structured JSON with:
- `material_id` + `material_name`
- `qty` and `uom` (unit of measure)
- `required_date`
- `delivery_location_id`
- `confidence` score (0–1)
- `ambiguities` list (any fields the AI was uncertain about)

### The example prompts:
Three "quick-fill" buttons are shown above the composer:
- **Standard Order** — a fully specified request (no ambiguities, parses in one turn)
- **Urgent Order** — similar but with urgency marker
- **Ambiguous (triggers clarification)** — "Raise a requisition for hydraulic seals" — deliberately vague, triggers the Q&A flow

These are for the demo — they allow a judge to trigger the clarification flow in one click.

### The `ai_available` flag:
The chat response always includes `ai_available: true/false`. If AI is unavailable (no API key configured), the screen shows a warning badge. This transparency means judges know if they are seeing real AI or the deterministic fallback.

---

## Component 2: Structured Extraction Panel (Right Column)

**What it shows:** The fields the AI extracted from the conversation, displayed as a structured form-like panel:
- Material name and ID
- Quantity and unit of measure
- Required delivery date
- Delivery location
- Overall confidence score (shown as a percentage)
- Any ambiguities flagged

**Why it updates live:** Every time a clarifying answer is submitted, the `parsed` draft updates. The user can watch the AI gradually fill in the fields as the conversation progresses. This makes the AI's "understanding" visible in real time, not hidden inside a server call.

---

## Component 3: Supplier Recommendation Cards

**When they appear:** After the requisition is fully parsed (`status === "parsed"`), a "Select Supplier" button appears. Clicking it calls `POST /requisitions/{id}/select-supplier`.

**What comes back:** A ranked list of all suppliers who can fulfil this request, each scored on 5 factors:

| Score | What it measures |
|---|---|
| **Price score** | How competitive the quoted unit price is (lower price = higher score) |
| **Quality score** | Supplier's historical defect rate and quality certifications |
| **Lead time score** | Can they deliver by the required date? (shorter lead time = higher score) |
| **Reliability score** | Historical on-time delivery rate |
| **Risk score** | Financial stability, contract compliance history |

**The overall score** is a weighted combination of these five. The supplier with rank 1 is recommended.

**The AI narration (`reasoning` field):** Each supplier card includes a 2–3 sentence explanation written by the LLM: *"Tata Steel scores 87.4 overall. Their ₹1,180/m quote is the most competitive by 8%, and their 14-day lead time comfortably meets the Friday deadline. Their 99.2% on-time rate across 47 recent deliveries drives the strong reliability score."*

**Interview tip:** This is a key architectural point. The AI **does not choose the supplier** — a deterministic arithmetic formula does. The AI only writes the human-readable explanation of why the formula picked that supplier. This means the decision is auditable and reproducible, but the explanation is natural language. If asked: *"Did AI select the supplier?"* — Answer: *"The scoring formula selected the supplier. AI wrote the justification. These are two very different things."*

---

## Component 4: PO Creation Confirmation

**After clicking "Award contract" on the recommended supplier:**
- Calls `POST /requisitions/{id}/select-supplier` with the chosen `supplier_id`.
- The backend creates a `purchase_order` record and publishes `PO_CREATED` to Redis.
- The `supplier_agent` (a background worker) picks up `PO_CREATED` and calls the supplier's confirmation endpoint, changing PO status to `CONFIRMED`.
- The Procurement screen shows the new PO number and a success banner.
- A link takes the user to the Traceability screen where they can watch the full P2P journey unfold.

---

## The Catalogue Panel (Master Data)

A small "Catalogue" panel shows all materials and their units of measure. This is loaded from `GET /catalogue` and acts as a reference for the user when typing their request. It shows what materials are in the approved catalogue and whether they require special approval.

---

## Data Flow

```
User types: "Need 300 mtr of alluminium extrusion profile"
      │
      ▼
POST /requisitions/chat { message, history: [] }
      │
      ├─ Status "clarifying" → return questions to user → user answers → repeat
      └─ Status "parsed"     → show structured extraction + "Select Supplier" button
      │
      ▼  (user clicks Select Supplier)
POST /requisitions/{id}/select-supplier
  → 5-factor scoring runs (deterministic math in procurement_scoring.py)
  → LLM writes reasoning for each candidate
  → Returns ranked recommendation list
      │
      ▼  (user clicks Award Contract on rank-1 card)
POST /requisitions/{id}/select-supplier { supplier_id: ... }
  → purchase_order created in DB
  → PO_CREATED event published to Redis
      │
      ▼  (background: supplier_agent picks up PO_CREATED)
  → Calls supplier confirmation API
  → PO status → CONFIRMED
  → Creates shipment + trailer (truck is now en route)
```
