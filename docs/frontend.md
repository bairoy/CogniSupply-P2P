# Frontend Architecture (React / Vite)

The frontend is a modern React application utilizing Tailwind CSS for styling and role-based access control.

## 1. Core Technologies
- **Vite:** Blazing fast build tool.
- **React Router:** For client-side navigation.
- **Tailwind CSS:** For rapid, utility-first UI styling.
- **WebSockets:** For real-time state pushes from the backend.

## 2. Role-Based Interfaces
The app changes entirely based on who logs in:
- **Yard Operator:** Sees `YardDock.tsx` (managing trucks, doors, and the IoT Scanner).
- **Procurement / Finance:** Sees `Procurement.tsx` (NLP intake, supplier award) and `MatchPay.tsx` (3-way match detail, exceptions, payment release — including the v8 **AI Audit Note** panel).
- **Executive/Admin:** Sees `ControlTower.tsx` (high-level KPIs, model performance, exception funnels).
- **External Customer:** Uses `Track.tsx` (the public-facing "Where's my truck?" portal without needing an account).

## 3. Real-Time Reactivity (`hooks/useEventStream.ts`, `hooks/useTrackStream.ts`)
Instead of having the frontend poll the server every 5 seconds (which crushes the database), the frontend maintains a persistent WebSocket connection to the `dashboard_gateway`. 

When an event happens on the backend (e.g., a truck arrives, or a match exception is created), the gateway pushes a tiny JSON message down the socket. The frontend listens for this message and selectively invalidates/re-fetches only the data that changed.

There are **two hooks, because there are two audiences**:

- **`useEventStream`** — signed-in staff. Connects to `/ws/dashboard` with the
  bearer token, receives every event in both domains. `useRefetchOn(events, [...], load)`
  is how a screen declares which event types should make it re-read.
- **`useTrackStream`** — the public tracker, which has no account and must not
  be sent PO, invoice, payment or supplier data. Connects to
  `/ws/track/{ref}`, which is scoped to one consignment. The socket carries no
  ids and no payload; it only says *something changed*, and the page re-reads
  `GET /track/{ref}` — so the public REST endpoint stays the single authority
  on what a customer may see.

**Polling never goes away.** Both hooks keep an interval running as a fallback,
and back off when the socket is healthy (`useTrackStream`: 8s when disconnected,
30s when live). This is the README's "two paths, not one" rule: REST for state,
WebSocket for *when to re-read it* — the stream is never used to reconstruct
state from scratch, so a client connecting five minutes into the demo sees the
correct picture rather than an empty screen.

## 4. Notable Components
- **IoT Dock Scanner (`YardDock.tsx`):** A visual simulation of a computer vision camera identifying pallets on a truck. It proves the requirement of a "simulated IoT goods receipt" visually, while triggering a real backend API call. The scanned quantity is **derived from the trailer's own `po_qty`** with a seeded ±5–8% variance — it used to be a hardcoded 500, which meant a truck carrying 1,200 units always "scanned" 500.
- **Public Tracker (`Track.tsx`):** Rendered *outside* the app shell — no sidebar, no Cmd+K, no internal KPIs. Mapbox GL JS draws the real driving route from the Directions API rather than a straight line, and internal vocabulary is translated for the audience (`critical` → "Urgent", not shown raw).
- **Predictive Invoice Risk (`ControlTower.tsx`):** Ranks open POs by money at risk. Fetched *separately* from the page's main `Promise.all`, so if the forecast fails the panel is simply absent instead of taking the whole Control Tower down.
- **MatchPay UI (`MatchPay.tsx`):** Visually aligns the PO, the Goods Receipt, and the Invoice side-by-side. It highlights exactly which field (Price or Quantity) caused the exception, making it trivial for a human to review.
