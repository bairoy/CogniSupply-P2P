# Interview Cheat Sheet — Quick Answers for Any Question

This is your rapid reference. Every question a judge might ask about this project, answered in 2–4 sentences you can say out loud.

---

## Architecture Questions

**Q: What is the overall architecture of this system?**
> It's a microservices system with two domain APIs (Yard and Procurement), a read-only gateway that serves the frontend, and three background workers. Services communicate asynchronously through Redis Streams. The database is a single PostgreSQL instance that all services share as the source of truth. The simulator acts as the outside world — trucks, suppliers, IoT sensors — by calling the same HTTP APIs that a real WMS or TMS would call.

**Q: Why did you use microservices instead of a monolith?**
> Because the two use cases have completely different ownership rules. The Yard API owns physical logistics data; the Procurement API owns financial documents. If they were one service, a bad query in the invoice processing logic could accidentally write to the trailers table. Separating them makes the ownership rule enforceable in code, not just in documentation.

**Q: How do services communicate with each other?**
> They don't talk to each other directly for state changes. When the Yard API confirms a goods receipt, it publishes a `GOODS_RECEIVED` event to a Redis Stream. The match_worker subscribes to that stream and acts on it. This means neither service needs to know about the other — they only need to agree on the event schema.

**Q: What is CQRS and how did you apply it?**
> CQRS means Command Query Responsibility Segregation — writes and reads go to different places. In our system, the Yard API and Procurement API handle all writes (commands). The Dashboard Gateway handles all reads (queries). The Gateway never writes to domain tables — if it needs to return a unified list of exceptions and dock delays, it does a SQL UNION read-only, it does not write a combined record anywhere. This keeps the data ownership clean.

**Q: What is the Transactional Outbox pattern?**
> It solves the problem of guaranteeing an event is published to Redis when you also wrote to the database. If you write to the DB and then crash before sending to Redis, the event is lost forever. With the Outbox pattern, you write the event into the `event_log` table in the same database transaction as the domain write. Both commit atomically. A background thread (`reconcile_unpublished()`) then reads unpublished rows and pushes them to Redis. If the server crashes, the event survives in the DB and gets published when it restarts.

---

## AI Questions

**Q: How does the AI work in your system?**
> We use AI in three places: (1) NLP requisition intake — a user types what they need in plain English and the LLM extracts a structured JSON with material, quantity, date, and location. (2) Invoice OCR — the LLM Vision API reads an invoice image and extracts PO number, quantity, and price with per-field confidence scores. (3) Supplier reasoning — after a math formula picks the best supplier, the LLM writes a 2–3 sentence explanation of why it won. AI extracts and explains. It never decides.

**Q: You show an AI-written note on the match screen. Isn't that the AI deciding?**
> No, and the ordering is what proves it. `evaluate()` runs first and returns the verdict; only then is the LLM called, and it is handed the finished decision and the numbers behind it. It writes the audit note. `match_policy.py` imports no model at all, so there is no path from the narration back into the outcome. `status` and `reason` stay authoritative — if the prose ever disagreed with `reason`, `reason` wins. And if the API key is missing or the call times out at 12s, we store a deterministic sentence built from the same numbers, so the panel behaves identically either way.

**Q: Why doesn't AI make the 3-way match decision?**
> Because an LLM can hallucinate. If the AI decides to pay a ₹10 lakh invoice by saying "this looks roughly right," that's a financial audit problem. Our match policy is pure deterministic code: if the quantity variance exceeds 2% or the price variance exceeds 3%, it raises an exception. Full stop. This is directly unit-testable, produces a complete audit trail, and can be explained to a CFO without saying "the AI thought it was fine."

**Q: What happens if the AI API goes down?**
> The system degrades gracefully. Every AI call in `llm.py` has a fallback. If the API is unavailable, `parse_requisition()` returns a stub parse, `extract_invoice()` returns `None`, and `write_supplier_reasoning()` generates a template explanation from the numeric scores. The system reports `"ai_available": false` so the UI can show a warning. The system keeps running — dock scheduling, 3-way matching, and payment processing are all deterministic and don't need AI.

**Q: Which LLM provider do you use?**
> The system supports both Anthropic Claude and OpenAI GPT. You configure it via `LLM_PROVIDER` in the `.env` file. If both keys are set, you can force one provider. If neither is set, the system runs in fallback mode. The frontend surface area is identical regardless of provider — no code in any service has an Anthropic vs OpenAI branch, only `llm.py` does.

---

## E2 (Yard) Questions

**Q: How does the dock scheduling work?**
> We use Google OR-Tools CP-SAT, which is a constraint programming solver. The dock worker listens for events that change the yard state (new trailer, ETA update, dock going out of service). When one fires, it builds an optimization problem: minimize total weighted wait time across all trucks, subject to constraints like door type compatibility and no double-booking. The solver runs in milliseconds and produces a schedule. If a truck is delayed by 90 minutes, the entire schedule re-optimizes around that change.

**Q: Why OR-Tools instead of a simple queuing algorithm?**
> A simple first-come-first-served or priority queue looks at one truck at a time. OR-Tools solves for all trucks simultaneously. For example, a low-priority truck might get an earlier slot because the high-priority truck needs a reefer door that's occupied, and putting the low-priority truck in a later dry-van slot frees the optimal door for the high-priority one. Simple rules can't see that; constraint programming can.

**Q: How does the customer tracker work?**
> A customer visits `/track/{reference}` with any of: their order ID, tracking number, trailer ID, or PO number. The server resolves whichever reference type it is and finds the associated trailer. It returns the current GPS position, ETA, delivery status, and event history. The page uses Mapbox GL JS to render a real vector map with the driving route, and maintains a WebSocket connection for real-time ETA updates — if the truck hits traffic, the customer sees the updated ETA within seconds.

**Q: How does the IoT goods receipt work?**
> When a truck docks, the yard operator clicks "Trigger unload scan" on the Dock Vision panel. This simulates a warehouse camera scanning the pallet load. The scan runs for 3 seconds, animating pallet detections one by one with bounding boxes and confidence scores. When complete, it posts `POST /trailers/{id}/unload` with the scanned quantity derived from the PO's expected quantity (with a seeded ±5–8% variance). This creates the Goods Receipt Note that the 3-way match needs.

---

## PR2 (Procurement) Questions

**Q: How does the NLP requisition work?**
> A user types what they need in conversational English. The request is sent to the LLM with the system prompt telling it to extract material, quantity, unit of measure, required date, and delivery location. If the request is ambiguous, the LLM returns clarifying questions and the conversation continues. The conversation history is passed on every round so the LLM has context. Once all fields are clear, it returns a structured JSON that creates a requisition record.

**Q: How does supplier selection work?**
> It's a 5-factor weighted scoring formula — Price, Quality, Lead Time, Reliability, and Risk. Each supplier in the catalogue has historical scores for these dimensions. The formula runs against all eligible suppliers and ranks them. The LLM then writes a natural language explanation of the top result. A procurement officer reviews the recommendation and clicks "Award contract" — this creates the PO and triggers the autonomous fulfillment chain.

**Q: What is 3-way matching?**
> 3-way matching is a standard accounting control. Before paying a supplier invoice, you verify three documents agree: the Purchase Order (what you authorized), the Goods Receipt Note (what you actually received), and the Supplier Invoice (what the supplier claims they should be paid). If all three match within tolerance (2% on quantity, 3% on price), payment is auto-approved. If they don't match, it raises an exception for a human to review. This prevents paying for goods that weren't received or paying inflated prices.

**Q: How is the payment process touchless?**
> When the 3-way match passes, the match_worker automatically creates an `APPROVED` payment record, attributed to the service account `USR-000`. The simulator then calls `POST /payments/{id}/pay` after a 2-minute delay, simulating the payment release. The entire path from requisition to payment settles with zero human involvement, as long as the invoice matches within tolerance.

**Q: Your "Predictive Invoice Risk" panel — is that machine learning?**
> No, and the endpoint says so rather than letting you assume it. It's a smoothed base rate: each supplier's own exception rate pulled toward a prior in proportion to how little history backs it. On 27 matched invoices, anything calling itself ML would be overclaiming. Two details make it honest. First, smoothing — suppliers here have between 0 and 8 matched invoices, so a raw rate would publish a supplier whose only two invoices both failed as "100% risk". Second, the money figure multiplies by the *measured* median dispute size (~8.5% of order value), not the whole PO — otherwise you'd claim the entire order is at stake when a typical mismatch disputes a fraction of it. Every input comes back with every score, so a judge can re-derive any number by hand.

**Q: How do you know any of this actually works?**
> `backend/eval/run_eval.py`. It scores the 3-way match as a 5-class classifier — precision, recall, F1, confusion matrix — and the NLP parser against 30 hand-labelled phrasings. `GET /kpi/model-performance` serves the result and returns **404 until the harness has actually been run**, because an honest "not measured yet" beats a fabricated number.

**Q: Your eval reports F1 = 1.00. Why should I believe that?**
> Two reasons, and I'd rather you interrogate both. First, the answer key: the obvious one is `ground_truth.json`'s `expected_match_status`, and it's a trap — `seed.py` writes that field from the same `evaluate()` call the suite grades, so scoring against it returns 1.00 by construction for any input. We score against `scenario` instead: the fault the seeder *injected*, chosen before the policy runs. Second, the baseline: the mix is ~74% clean, so an always-APPROVED classifier already scores 0.74, and the harness prints that floor next to the accuracy. The case that actually earns the score is the near-miss — a 1.5% quantity variance that must still be APPROVED. A rule that flagged every variance would ace the other five scenarios and fail only there.

**Q: What's NOT measured?**
> OCR. `invoices.document_path` is only written by the image-upload path, and nothing in the seed or simulator renders an invoice image, so there is no scan to read. The seeded `ocr_raw` was written by `seed.py`, and grading it would grade a `random.uniform()` call. The suite reports `not_measured` with the reason rather than omitting it or inventing a score.

---

## Database & Infrastructure Questions

**Q: Why PostgreSQL instead of a NoSQL database?**
> Because supply chain data is inherently relational. A PO links to a supplier, a shipment links to a PO, a trailer links to a shipment, a goods receipt links to a trailer. These are foreign key relationships. Trying to model a P2P cycle in a document store means either embedding everything (data duplication, no constraints) or managing references manually (what a relational DB does natively). PostgreSQL also gives us transactions for the Transactional Outbox pattern, which we depend on for event atomicity.

**Q: What does the seed data look like?**
> It seeds about 70–80% "happy path" records — invoices that cleanly match their POs — and 20–30% mismatches, split between quantity errors, price errors, missing PO references, and near-misses (inside tolerance, to prove the tolerance band does something). The near-misses are what prove the system has a tolerance band at all — a system that only shows clean vs. broken never demonstrates it can handle edge cases.

**Q: What is the role of the Simulator?**
> The simulator acts as the physical world. It moves trucks (GPS ticks), triggers arrivals and departures, submits invoice images, and sends goods receipt signals. Critically, it does this by making real HTTP API calls — not by writing directly to the database. This means the demo proves the APIs work, not just that the database has the right data. The simulator's scenarios (delay a truck, inject a price mismatch) all trigger through real API endpoints, producing real events that propagate through the real system.
