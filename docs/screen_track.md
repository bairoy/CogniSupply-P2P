# Screen Documentation: Track — Customer Delivery Tracker

## What This Screen Is For
This is the **public-facing, customer-visible delivery tracker**. Unlike every other screen in the app, this one does not require login. A customer receives a tracking number via email and can type it here to see where their delivery is.

> **Route:** `/track/:ref` (where `ref` can be a Trailer ID, Tracking Number, PO ID, or Outbound Order ID)  
> **Who sees it:** External customers, warehouse staff, anyone with a tracking reference  
> **Use case:** E2 — "Where's My Truck?" requirement #1

---

## Why It Looks Different From Every Other Screen

Every other screen (YardDock, ControlTower, MatchPay) is inside the app shell — it has the sidebar, the top bar, the Cmd+K search, role-based menus. The Track screen is **completely outside that shell** (see `App.tsx` — it is matched before the auth wrapper).

The reason: a customer who typed a consignment number should see a clean, simple status page — not a warehouse control tower dashboard full of internal KPIs, dock costs, and supplier scores.

**Interview tip:** *"We deliberately separated the internal console from the customer-facing view. The same backend API serves both, but the frontend renders them completely differently based on audience. The Track screen is essentially a parcel tracker — it answers one question: where is my delivery and when does it arrive?"*

---

## The Flexible Reference System

The URL accepts any of these and resolves them correctly:
- **Trailer ID** (e.g., `TRL-00042`) — looks up the trailer directly
- **Tracking number** (e.g., `TRK-OB-55234`) — looks up the shipment, then its trailer
- **PO number** (e.g., `PO-1042`) — looks up the PO, then its shipment, then its trailer
- **Outbound Order ID** — looks up the customer order, then its dispatch trailer

This resolution happens **server-side** in the `_resolve_reference()` function in `dashboard_gateway/main.py`. The API tries each lookup in order and returns the first match. This means the customer only needs to know their order number — they don't need to understand what a "trailer" or "shipment" is.

---

## The 4 Main Panels

### 1. Map Panel (Mapbox GL JS)

**What it shows:** A full interactive map with:
- **Origin pin** (blue circle) — where the shipment started (e.g., "Tata Steel Jamshedpur")
- **Destination pin** (green circle) — where it's going (e.g., "Mumbai Warehouse")
- **Truck marker** — the current GPS position, animated as a pulsing dot
- **Route line** — the actual driving route between origin and destination (not a straight line), fetched from the Mapbox Directions API
- **Route stats card** (top-left of map) — shows distance in km and estimated drive time

**How the Mapbox integration works:**
- Uses `react-map-gl/mapbox` with the `mapbox://styles/mapbox/standard` style (a premium 3D vector map).
- The Mapbox token is read from `import.meta.env.VITE_MAPBOX_TOKEN` (the `VITE_` prefix means Vite injects it into the browser bundle at build time).
- On load, it calls the Mapbox Directions API: `GET https://api.mapbox.com/directions/v5/mapbox/driving/{origin},{destination}?access_token=...`
- The response contains a `geometry.coordinates` array — a polyline of lat/lng points following actual roads.
- This polyline is rendered as a GeoJSON `Source` with a `Layer` on the map.
- The truck pin is positioned at `current_position.latitude/longitude` from the tracking API.

**Graceful fallback:** If `VITE_MAPBOX_TOKEN` is not set, the entire map panel is hidden. The rest of the tracker — status, milestones, history — still renders. The app does not crash.

**Interview tip on Mapbox:** *"We chose Mapbox GL JS because it's a named commercial mapping API that satisfies the brief's requirement, and it renders WebGL vector tiles — the map is genuinely smooth and premium-looking compared to raster tile solutions. The free tier gives 50,000 map loads per month, which is more than enough."*

---

### 2. Status Card

**What it shows:** The human-readable status of the delivery:
- A progress bar (0–100%) showing how far through the journey the delivery is
- Current status translated to customer language: e.g., `EN_ROUTE` → "Your shipment is on the way", `DOCKED` → "Being unloaded at the warehouse"
- Priority mapped to customer-friendly language: `normal` → "Standard", `high` → "Priority", `critical` → "Urgent", `low` → "Economy"
- ETA displayed as a formatted date/time
- Carrier name and load type (dry van, reefer, flatbed)

**Why priority labels are translated:** Internally we use `high`, `normal`, `critical` etc. Showing these raw strings to a customer is confusing — "critical" sounds alarming. The screen maps them to customer-friendly equivalents.

---

### 3. Milestone Timeline

**What it shows:** A vertical list of key events in the delivery's journey, in chronological order.

Example milestones:
- ✅ PO Created
- ✅ Shipment dispatched
- ✅ Truck departed origin
- ✅ Truck arrived at warehouse gate
- ✅ Goods received (unloaded)
- ⏳ Delivered (pending)

**How it works:** The `/track/{ref}` API returns a `timeline` array — the sequence of domain events recorded for this trailer. The frontend maps each `event_type` to a human-readable label and icon. Only events that are meaningful to a customer are shown (internal events like `DOCK_ASSIGNED` are filtered out).

---

### 4. Real-Time WebSocket Hook (`useTrackStream`)

**What makes this different from simple polling:**
- The Track screen maintains a WebSocket connection to the gateway at
  `/ws/track/{ref}` — a **separate, public rail**, not the authenticated
  `/ws/dashboard` firehose. That matters: `/ws/dashboard` needs a token the
  customer does not have, and carries purchase orders, invoices, supplier
  scores and payments. Pushing that to a browser opened by someone outside the
  company is a disclosure whether or not the screen renders it.
- The socket is filtered twice: to this one trailer, and to an 11-event
  customer vocabulary (`ETA_UPDATED`, `TRAILER_ARRIVED`, `TRAILER_DOCKED`,
  `GOODS_RECEIVED`, …). Nothing from PR2 crosses it.
- Frames carry **no `entity_id` and no payload** — only "something changed".
  The page then re-reads `GET /track/{ref}`, which stays the single authority
  on what a customer may be shown, so the socket cannot leak anything the
  public REST endpoint does not already expose.
- An unresolvable reference is refused at the handshake with close code `1008`
  rather than left hanging open.
- Polling never stops, it **backs off**: 30s while the socket is healthy, 8s when it is not. The socket says *when* to re-read; REST remains the source of state, so a customer opening the page mid-journey sees the correct picture rather than an empty one.
- A badge shows **"Updating live"** (pulsing green) when the socket is connected, and **"Reconnecting…"** (amber) when it is not. Reconnection is exponential backoff, capped at 15s.

**Interview tip:** *"The customer tracker uses WebSocket for real-time ETA updates. If a truck hits traffic and its ETA changes, the update reaches the customer's browser within milliseconds of the backend processing it — not after the next 8-second poll cycle."*

---

## Data Flow

```
Customer visits /track/TRL-00042
      │
      ▼
GET /track/TRL-00042 (no auth required)
  → server resolves to trailer TRL-00042
  → returns: trailer status, shipment, dock, origin/destination,
             current GPS position, delivery_progress_pct, event timeline
      │
      ▼
Frontend renders: map, status card, milestones, history
      │
      ▼
Separately: Mapbox Directions API call
  → GET https://api.mapbox.com/directions/v5/mapbox/driving/{origin}/{dest}
  → returns: polyline coordinates
  → drawn on map as blue route line
      │
      ▼
WebSocket (useTrackStream hook) listens for:
  ETA_UPDATED, TRAILER_ARRIVED, TRAILER_DOCKED, GOODS_RECEIVED
      │
      ▼
On any event → re-fetch /track/{ref} → map and status card update
8-second polling as fallback if WebSocket disconnects
```
