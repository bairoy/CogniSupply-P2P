import { ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import Map, {
  AttributionControl,
  Layer,
  Marker,
  NavigationControl,
  Popup,
  Source,
  type MapRef,
} from "react-map-gl/mapbox";
import "mapbox-gl/dist/mapbox-gl.css";
import { api } from "../api";
import { useAuth } from "../auth";
import { Icon, ago, clock } from "../components/ui";
import { useTrackStream } from "../hooks/useTrackStream";

/**
 * Customer Visibility Portal -- the public, customer-facing delivery tracker.
 *
 * This is the brief's E2 requirement #1 -- "accept a tracking number, trailer
 * ID, or shipment reference" -- and the reason /track/{ref} resolves all three
 * server-side rather than making the caller know which one they hold.
 *
 * WHY IT LOOKS NOTHING LIKE THE REST OF THE APP
 *
 * Every other screen is an internal console: sidebar, global search, live
 * event rail, dock cost models. This one is seen by someone outside the
 * company who typed a consignment number into a link, and it is rendered
 * outside the Shell (see App.tsx) precisely so none of that chrome reaches
 * them. They get one question answered -- where is my delivery, and when does
 * it land -- in the visual language of a parcel tracker, not a control tower.
 * Nothing here exposes a dock cost, a supplier score or an internal queue.
 */

interface TrackResult {
  reference: string;
  resolved_as: { entity_type: string; entity_id: string };
  trailer: { id: string; status: string; eta: string | null; priority: string; load_type: string };
  shipment: { id: string; po_id: string; carrier: string | null; tracking_number: string | null;
    expected_arrival: string | null };
  dock: { dock_id: string; status: string; docked_at: string | null } | null;
  origin: { name: string | null; latitude: number | null; longitude: number | null };
  destination: { name: string | null; latitude: number | null; longitude: number | null };
  current_position: { latitude: number; longitude: number; recorded_at: string } | null;
  /* Runs of GPS pings arrive folded into a single row carrying `count` and the
     span -- the gateway's ?telemetry=collapsed, which is its default. A
     customer wants to know the vehicle kept moving, not to scroll 600 identical
     lines to find out. The map still draws every point. */
  timeline: {
    event_type: string;
    at: string;
    summary: string | null;
    collapsed?: boolean;
    count?: number;
    from?: string;
    to?: string;
  }[];
  delivery_progress_pct: number;
}

/**
 * Mapping runs on Mapbox GL JS (via react-map-gl) -- vector basemap plus the
 * Mapbox Directions API for the real driving line between origin and
 * destination. A public pk.* token is a client-side credential by design; it
 * still only reaches the bundle because it is VITE_-prefixed.
 *
 * No token is a soft failure, not a crash: the tracker drops the map panel and
 * every other part of the page -- status, milestones, history -- still renders.
 */
const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined;

/**
 * Mapbox Standard (GL JS v3), not Light. Light's ocean is hsl(220,1%,86%) --
 * 1% saturation -- so any coastal route renders against a dead grey slab.
 * Standard gives real water, landcover and a v3 label hierarchy. Overridable
 * because a basemap is taste, and taste should not need a redeploy.
 */
// `||` not `??`: an env key present but blank (VITE_MAPBOX_STYLE= in .env, as
// .env.example ships it) is "", which ?? would happily pass to mapStyle.
const MAP_STYLE = import.meta.env.VITE_MAPBOX_STYLE || "mapbox://styles/mapbox/standard";

/**
 * Generous top padding specifically: the truck pin is 44px tall and anchored
 * at its centre, so a uniform pad clips it against the top edge whenever the
 * vehicle is near the northern end of its route.
 */
// Sides clear 90px because a pin's name chip is up to 150px wide and centred on
// the pin -- 72 clipped "Tata Steel Jamshedpur…" against the right edge.
const FIT_PADDING = { top: 76, bottom: 64, left: 90, right: 90 };

type LngLat = [number, number];

/** Metres/seconds from the Directions API, shown on the map's stat card. */
interface RouteMeta {
  distance: number;
  duration: number;
}

const km = (metres: number) => `${Math.round(metres / 1000).toLocaleString()} km`;

const hrs = (seconds: number) => {
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
};

/** A pin plus its always-on name chip -- the map should not need clicking to read. */
function PlacePin({
  icon,
  name,
  tone,
}: {
  icon: string;
  name: string;
  tone: "origin" | "destination";
}) {
  const ring = tone === "origin" ? "border-primary text-primary" : "border-success text-success";
  return (
    <span className="flex flex-col items-center gap-1">
      <span
        className={`grid h-9 w-9 place-items-center rounded-full border-[2.5px] bg-white shadow-[0_4px_12px_rgb(15_23_42/0.28)] ${ring}`}
      >
        <Icon name={icon} className="!text-[18px]" />
      </span>
      <span className="max-w-[150px] truncate rounded-full bg-white/95 px-2 py-0.5 text-[11px] font-semibold text-on-surface shadow-[0_2px_8px_rgb(15_23_42/0.18)] backdrop-blur">
        {name}
      </span>
    </span>
  );
}

function TruckPin() {
  return (
    <span className="relative grid h-11 w-11 place-items-center">
      {/* the halo reads as "this one is live" without needing a legend */}
      <span className="absolute inset-0 animate-ping rounded-full bg-primary/40" />
      <span className="absolute inset-[-6px] rounded-full bg-primary/15" />
      <span className="relative grid h-11 w-11 place-items-center rounded-full border-[3px] border-white bg-primary text-white shadow-[0_6px_16px_rgb(79_70_229/0.55)]">
        <Icon name="local_shipping" className="!text-[20px]" />
      </span>
    </span>
  );
}

/**
 * The slice of the route already driven, so the line itself carries
 * delivery_progress_pct rather than only the bar above it. Walks the geometry
 * accumulating segment length (planar is fine at this zoom -- it is a ratio,
 * not a distance we quote to anyone) and cuts at the target fraction,
 * interpolating the final partial segment so the join lands under the truck.
 */
function travelledSlice(coords: LngLat[], pct: number): LngLat[] {
  if (coords.length < 2 || pct <= 0) return [];
  if (pct >= 100) return coords;

  const seg = coords.slice(1).map((c, i) => Math.hypot(c[0] - coords[i][0], c[1] - coords[i][1]));
  const total = seg.reduce((a, b) => a + b, 0);
  if (total === 0) return [];

  let target = (total * pct) / 100;
  const out: LngLat[] = [coords[0]];
  for (let i = 0; i < seg.length; i++) {
    if (target <= seg[i]) {
      const t = seg[i] === 0 ? 0 : target / seg[i];
      const [ax, ay] = coords[i];
      const [bx, by] = coords[i + 1];
      out.push([ax + (bx - ax) * t, ay + (by - ay) * t]);
      break;
    }
    target -= seg[i];
    out.push(coords[i + 1]);
  }
  return out;
}

/**
 * Index of the route vertex closest to the vehicle. The truck is drawn at its
 * reported GPS fix, so splitting the line anywhere else puts the colour change
 * visibly away from the pin -- which is exactly what happens on an ARRIVED
 * trailer, where the gateway pins delivery_progress_pct at 70 while the truck
 * is already sitting on the destination.
 */
function nearestIndex(coords: LngLat[], point: LngLat): number {
  let best = 0;
  let bestDist = Infinity;
  for (let i = 0; i < coords.length; i++) {
    const d = (coords[i][0] - point[0]) ** 2 + (coords[i][1] - point[1]) ** 2;
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  return best;
}

function lineFeature(coords: LngLat[]): GeoJSON.Feature<GeoJSON.LineString> {
  return { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: coords } };
}

const MILESTONES = [
  { key: "TRAILER_DEPARTED", label: "Picked up", icon: "factory" },
  { key: "TRAILER_LOCATION_UPDATED", label: "In transit", icon: "local_shipping" },
  { key: "TRAILER_ARRIVED", label: "At destination", icon: "flag" },
  // NB: not "dock" -- that Material symbol is a phone dock and renders as a
  // handset next to four logistics icons.
  { key: "TRAILER_DOCKED", label: "Unloading", icon: "warehouse" },
  { key: "GOODS_RECEIVED", label: "Delivered", icon: "task_alt" },
];

/** Plain-English status. The internal vocabulary means nothing to a customer. */
const CUSTOMER_STATUS: Record<string, { label: string; blurb: string; tone: string }> = {
  EN_ROUTE: {
    label: "On the way",
    blurb: "Your consignment is in transit and tracking to schedule.",
    tone: "bg-info-container text-info",
  },
  ARRIVED: {
    label: "Arrived at facility",
    blurb: "The vehicle has reached the delivery site and is awaiting a bay.",
    tone: "bg-info-container text-info",
  },
  DOCKED: {
    label: "Unloading now",
    blurb: "Your consignment is at the bay and is being unloaded.",
    tone: "bg-[#e2dfff] text-primary",
  },
  UNLOADED: {
    label: "Delivered",
    blurb: "Your consignment has been received and checked in.",
    tone: "bg-success-container text-success",
  },
  DEPARTED: {
    label: "Delivered",
    blurb: "Delivery is complete and the vehicle has left the site.",
    tone: "bg-success-container text-success",
  },
  DELAYED: {
    label: "Running late",
    blurb: "This delivery is behind its original window. The estimate below is current.",
    tone: "bg-warning-container text-warning",
  },
};

/** Customer-readable line for an internal event type. */
const EVENT_LABEL: Record<string, string> = {
  SHIPMENT_CREATED: "Shipment booked",
  TRAILER_DEPARTED: "Collected from origin",
  TRAILER_LOCATION_UPDATED: "Location update",
  ETA_UPDATED: "Arrival estimate revised",
  TRAILER_ARRIVED: "Arrived at delivery site",
  DOCK_ASSIGNED: "Unloading bay allocated",
  DOCK_REASSIGNED: "Unloading bay changed",
  DOCK_DELAYED: "Waiting for an unloading bay",
  TRAILER_DOCKED: "Unloading started",
  GOODS_RECEIVED: "Delivered and checked in",
  TRAILER_EXITED: "Vehicle departed site",
  GOODS_ISSUED: "Loaded for despatch",
};

function label(eventType: string) {
  return EVENT_LABEL[eventType] ?? eventType.replace(/_/g, " ").toLowerCase();
}

/**
 * Service level, in the words a customer would use for it.
 *
 * `trailers.priority` is a scheduling input -- it is the wait weight in the
 * dock cost model -- and "critical" on a delivery page reads as an emergency
 * rather than as the tier someone paid for. The vocabulary is the four values
 * in schema.sql (low | normal | high | critical), shared with
 * outbound_orders.priority.
 */
const SERVICE_LEVEL: Record<string, string> = {
  low: "Economy",
  normal: "Standard",
  high: "Priority",
  critical: "Urgent",
};

/**
 * Unmapped values fall through as-is rather than to a placeholder: priority is
 * TEXT and append-only (CLAUDE.md), so a fifth tier can appear without this
 * file changing, and showing the raw word beats showing "—" for a real one.
 */
function serviceLevel(priority?: string | null): string {
  if (!priority) return "—";
  return SERVICE_LEVEL[priority.toLowerCase()] ?? priority.replace(/_/g, " ");
}

/**
 * The portal shell: masthead, page body, footer. No app chrome anywhere.
 *
 * `live` is the tracker socket's state. Undefined on the loading and error
 * screens, which have no socket yet -- the chip then makes no claim either way
 * rather than asserting a connection that does not exist.
 */
function Portal({ children, live }: { children: ReactNode; live?: boolean }) {
  const { user } = useAuth();
  return (
    <div className="min-h-full bg-surface-dim/40">
      <header className="bg-inverse-surface">
        <div className="mx-auto flex max-w-4xl items-center justify-between gap-4 px-6 py-5">
          <div className="flex items-center gap-3">
            <span className="grid h-10 w-10 place-items-center rounded-lg bg-inverse-on-surface/10 text-inverse-on-surface">
              <Icon name="package_2" />
            </span>
            <div>
              <p className="text-headline-md leading-tight text-inverse-on-surface">
                Delivery Tracking
              </p>
              <p className="text-body-sm text-inverse-on-surface/60">
                Live consignment status
              </p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <span className="flex items-center gap-1.5 rounded-full bg-inverse-on-surface/10 px-3 py-1 text-body-sm text-inverse-on-surface/80">
              <span
                className={`h-2 w-2 rounded-full ${
                  live === false ? "bg-warning-container" : "bg-success-container animate-pulse"
                }`}
              />
              {live === false ? "Reconnecting…" : "Updating live"}
            </span>
            {user && (
              <Link to="/" className="btn-primary py-1.5 px-4 text-sm whitespace-nowrap !rounded-full">
                <Icon name="exit_to_app" className="!text-[16px]" /> Return to Control Tower
              </Link>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-6 py-8">{children}</main>

      <footer className="mx-auto max-w-4xl px-6 pb-10 pt-2">
        <p className="text-body-sm text-on-surface-variant">
          {live === false
            ? "Live updates are reconnecting — status is refreshing every 8 seconds meanwhile."
            : "Status updates the moment your delivery moves."}{" "}
          Times shown in your local timezone. Questions about this delivery? Quote the
          consignment reference above.
        </p>
      </footer>
    </div>
  );
}

export default function Track() {
  const { ref } = useParams();
  const [data, setData] = useState<TrackResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [routeCoords, setRouteCoords] = useState<LngLat[] | null>(null);
  const [routeMeta, setRouteMeta] = useState<RouteMeta | null>(null);
  const [popup, setPopup] = useState<null | "origin" | "destination" | "truck">(null);
  const mapRef = useRef<MapRef>(null);

  const originLngLat = data?.origin.longitude != null && data?.origin.latitude != null
    ? ([data.origin.longitude, data.origin.latitude] as LngLat) : null;
  const destLngLat = data?.destination.longitude != null && data?.destination.latitude != null
    ? ([data.destination.longitude, data.destination.latitude] as LngLat) : null;

  /**
   * Road route from the Mapbox Directions API, fetched once per consignment --
   * the origin/destination pair is fixed for the life of a shipment, so the
   * 8s status poll must never re-request it.
   */
  useEffect(() => {
    if (!MAPBOX_TOKEN || !originLngLat || !destLngLat) return;
    let cancelled = false;
    const url =
      `https://api.mapbox.com/directions/v5/mapbox/driving/` +
      `${originLngLat.join(",")};${destLngLat.join(",")}` +
      `?geometries=geojson&overview=full&access_token=${MAPBOX_TOKEN}`;
    fetch(url)
      .then((res) => res.json())
      .then((body) => {
        // No drivable route (an overseas leg, say) is a normal answer, not an
        // error -- the map falls back to the straight dashed line below.
        if (!cancelled && body.routes?.[0]?.geometry?.coordinates) {
          setRouteCoords(body.routes[0].geometry.coordinates as LngLat[]);
          setRouteMeta({
            distance: body.routes[0].distance,
            duration: body.routes[0].duration,
          });
        }
      })
      .catch((err) => console.error("Mapbox Directions fetch failed", err));
    return () => {
      cancelled = true;
    };
  }, [originLngLat?.[0], originLngLat?.[1], destLngLat?.[0], destLngLat?.[1]]);

  const load = useCallback(() => {
    if (!ref) return;
    api
      .gateway<TrackResult>(`/track/${ref}`)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(e.message));
  }, [ref]);

  /**
   * Live deltas over /ws/track/{ref}, with the poll kept as the fallback it
   * always was -- README §5's two paths, applied to the customer portal.
   *
   * The socket only says "re-read"; this REST call is still the only thing
   * that decides what the page shows. `pollMs` stretches to 30s while the
   * socket is up, so a page open for an hour makes ~120 fewer requests and
   * still recovers on its own if a delta is ever dropped.
   */
  const { live, pollMs } = useTrackStream(ref, load);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(load, pollMs);
    return () => window.clearInterval(timer);
  }, [load, pollMs]);

  /**
   * Everything the map layers need, derived once per render pass. Falls back to
   * the straight origin->destination line while the Directions call is in
   * flight (or if it found no road route), so the map is never empty.
   */
  const progressPct = data?.delivery_progress_pct ?? 0;
  const posLng = data?.current_position?.longitude;
  const posLat = data?.current_position?.latitude;
  const map = useMemo(() => {
    if (!originLngLat || !destLngLat) return null;
    const coords = routeCoords ?? [originLngLat, destLngLat];
    const lons = coords.map((c) => c[0]);
    const lats = coords.map((c) => c[1]);

    // Prefer the vehicle's own fix over the percentage: the two disagree on an
    // ARRIVED trailer, and only one of them is where the truck pin is drawn.
    const done =
      posLng != null && posLat != null
        ? coords.slice(0, nearestIndex(coords, [posLng, posLat]) + 1)
        : travelledSlice(coords, progressPct);

    return {
      isRoad: routeCoords !== null,
      full: lineFeature(coords),
      done: lineFeature(done),
      bounds: [
        [Math.min(...lons), Math.min(...lats)],
        [Math.max(...lons), Math.max(...lats)],
      ] as [LngLat, LngLat],
    };
  }, [
    routeCoords,
    originLngLat?.[0], originLngLat?.[1],
    destLngLat?.[0], destLngLat?.[1],
    progressPct, posLng, posLat,
  ]);

  /**
   * Re-frame once the road route lands. initialViewState is honoured only at
   * mount, and at mount all we have is the straight origin->destination line --
   * a real route bulges away from it (coastal highways especially), so without
   * this the map stays framed on a corridor the truck does not drive down.
   */
  const bounds = map?.bounds;
  useEffect(() => {
    if (!bounds) return;
    mapRef.current?.getMap().fitBounds(bounds, { padding: FIT_PADDING, duration: 900 });
  }, [bounds?.[0][0], bounds?.[0][1], bounds?.[1][0], bounds?.[1][1]]);

  /**
   * Mapbox Standard is configured through the style, not through paint props.
   * Dropping POI/transit labels leaves the route as the only thing competing
   * for attention. Guarded: on a classic style these calls simply do not apply.
   */
  const onMapLoad = useCallback(() => {
    const gl = mapRef.current?.getMap();
    if (!gl?.setConfigProperty) return;
    try {
      gl.setConfigProperty("basemap", "lightPreset", "day");
      gl.setConfigProperty("basemap", "showPointOfInterestLabels", false);
      gl.setConfigProperty("basemap", "showTransitLabels", false);
    } catch {
      /* classic style (light-v11 etc.) has no basemap fragment -- fine */
    }
  }, []);

  if (error) {
    return (
      <Portal live={live}>
        <div className="card-pad text-center">
          <Icon name="search_off" className="!text-[32px] text-outline" />
          <h1 className="mt-2 text-headline-lg">We could not find that consignment</h1>
          <p className="mt-1 text-body-md text-on-surface-variant">
            Check the reference and try again. You can use a tracking number, a vehicle ID or a
            shipment reference.
          </p>
          <p className="mono mt-3 text-outline">{error}</p>
        </div>
      </Portal>
    );
  }

  if (!data) {
    return (
      <Portal live={live}>
        <div className="card-pad flex items-center justify-center gap-2 py-16 text-on-surface-variant">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-outline-variant border-t-primary" />
          <span className="text-body-md">Retrieving your delivery status…</span>
        </div>
      </Portal>
    );
  }

  const reached = new Set(data.timeline.map((t) => t.event_type));
  const maxReachedIndex = MILESTONES.reduce((max, m, idx) => reached.has(m.key) ? Math.max(max, idx) : max, -1);
  const status =
    CUSTOMER_STATUS[data.trailer.status] ?? {
      label: data.trailer.status.replace(/_/g, " ").toLowerCase(),
      blurb: "Your consignment is being processed.",
      tone: "bg-surface-container-high text-on-surface-variant",
    };
  const delivered = reached.has("GOODS_RECEIVED");
  /** The vehicle has finished driving -- which happens before, and independently
   *  of, the goods being booked in. Only the map's distance card cares. */
  const journeyComplete = delivered || progressPct >= 100 || reached.has("TRAILER_ARRIVED");

  return (
    <Portal live={live}>
      <div className="flex flex-col gap-5">
        {/* ---- headline status ---- */}
        <section className="card overflow-hidden">
          <div className="flex flex-wrap items-start justify-between gap-4 p-6">
            <div className="min-w-0">
              <span className="label">Consignment</span>
              <p className="mono text-headline-lg text-on-surface">
                {data.shipment.tracking_number ?? data.reference}
              </p>
              <p className="mt-1 text-body-md text-on-surface-variant">
                {data.origin.name ?? "Origin"}{" "}
                <Icon name="arrow_forward" className="!text-[16px] align-middle" />{" "}
                {data.destination.name ?? "Destination"}
              </p>
            </div>
            <div className="text-right">
              <span className={`badge !px-3 !py-1 !text-[13px] ${status.tone}`}>
                {status.label}
              </span>
              <p className="mt-2 label">
                {delivered ? "Delivered at" : "Estimated arrival"}
              </p>
              <p className="text-display leading-none tnum">{clock(data.trailer.eta)}</p>
            </div>
          </div>

          <p className="px-6 pb-5 text-body-md text-on-surface-variant">{status.blurb}</p>

          {/* progress */}
          <div className="px-6">
            <div className="h-2 overflow-hidden rounded-full bg-surface-container-high">
              <div
                className={`h-full rounded-full transition-all duration-700 ${delivered ? "bg-success" : "bg-primary-container"}`}
                style={{ width: `${data.delivery_progress_pct}%` }}
              />
            </div>
          </div>

          {/* live map -- Mapbox GL JS */}
          {map && originLngLat && destLngLat && data.current_position && (
            <div className="relative z-0 mt-6 h-[380px] w-full border-y border-outline-variant/30 sm:h-[480px]">
              {MAPBOX_TOKEN ? (
                <>
                <Map
                  ref={mapRef}
                  mapboxAccessToken={MAPBOX_TOKEN}
                  mapStyle={MAP_STYLE}
                  initialViewState={{ bounds: map.bounds, fitBoundsOptions: { padding: FIT_PADDING } }}
                  // Mercator, not Standard's default globe: fitBounds is exact
                  // in mercator, and a curved horizon on a domestic leg reads
                  // as decoration rather than information.
                  projection={{ name: "mercator" }}
                  // The map sits mid-page: grabbing the wheel would trap the
                  // reader's scroll. Pan and the +/- control stay available.
                  scrollZoom={false}
                  dragRotate={false}
                  touchPitch={false}
                  attributionControl={false}
                  onLoad={onMapLoad}
                  reuseMaps
                  style={{ width: "100%", height: "100%" }}
                >
                  <NavigationControl position="top-right" showCompass={false} />
                  {/* Mapbox terms require visible attribution. Compact keeps it
                      to an (i) disc instead of a line of text across the map. */}
                  <AttributionControl compact position="bottom-right" />

                  <Source id="route" type="geojson" data={map.full}>
                    {/* soft glow, then white casing: the line stays readable
                        over water, landcover and motorway fills alike */}
                    <Layer
                      id="route-glow"
                      type="line"
                      slot="middle"
                      layout={{ "line-cap": "round", "line-join": "round" }}
                      paint={{
                        "line-color": "#4f46e5",
                        "line-width": 16,
                        "line-blur": 14,
                        "line-opacity": 0.3,
                      }}
                    />
                    <Layer
                      id="route-casing"
                      type="line"
                      slot="middle"
                      layout={{ "line-cap": "round", "line-join": "round" }}
                      paint={{ "line-color": "#ffffff", "line-width": 9, "line-opacity": 0.95 }}
                    />
                    <Layer
                      id="route-remaining"
                      type="line"
                      slot="middle"
                      layout={{ "line-cap": "round", "line-join": "round" }}
                      paint={{
                        "line-color": "#94a3b8",
                        "line-width": 4.5,
                        "line-opacity": 0.85,
                        // dashes only on the straight-line fallback, so a
                        // guessed path never reads as a surveyed road route
                        ...(map.isRoad ? {} : { "line-dasharray": [2, 2] }),
                      }}
                    />
                  </Source>

                  {/* lineMetrics is what makes line-gradient legal -- the
                      travelled leg fades cyan->indigo along its own length */}
                  <Source id="route-done" type="geojson" data={map.done} lineMetrics>
                    <Layer
                      id="route-done"
                      type="line"
                      slot="middle"
                      layout={{ "line-cap": "round", "line-join": "round" }}
                      paint={{
                        "line-width": 5.5,
                        "line-gradient": [
                          "interpolate",
                          ["linear"],
                          ["line-progress"],
                          0, "#06b6d4",
                          0.5, "#4f46e5",
                          1, "#4338ca",
                        ],
                      }}
                    />
                  </Source>

                  <Marker
                    longitude={originLngLat[0]}
                    latitude={originLngLat[1]}
                    anchor="top"
                    offset={[0, -18]}
                    onClick={() => setPopup("origin")}
                  >
                    <PlacePin icon="factory" tone="origin" name={data.origin.name ?? "Origin"} />
                  </Marker>

                  <Marker
                    longitude={destLngLat[0]}
                    latitude={destLngLat[1]}
                    anchor="top"
                    offset={[0, -18]}
                    onClick={() => setPopup("destination")}
                  >
                    <PlacePin
                      icon="flag"
                      tone="destination"
                      name={data.destination.name ?? "Destination"}
                    />
                  </Marker>

                  <Marker
                    longitude={data.current_position.longitude}
                    latitude={data.current_position.latitude}
                    anchor="center"
                    onClick={() => setPopup("truck")}
                  >
                    <TruckPin />
                  </Marker>

                  {popup === "origin" && (
                    <Popup
                      longitude={originLngLat[0]}
                      latitude={originLngLat[1]}
                      anchor="bottom"
                      offset={20}
                      closeButton={false}
                      onClose={() => setPopup(null)}
                    >
                      <span className="text-body-sm font-semibold">{data.origin.name ?? "Origin"}</span>
                    </Popup>
                  )}
                  {popup === "destination" && (
                    <Popup
                      longitude={destLngLat[0]}
                      latitude={destLngLat[1]}
                      anchor="bottom"
                      offset={20}
                      closeButton={false}
                      onClose={() => setPopup(null)}
                    >
                      <span className="text-body-sm font-semibold">
                        {data.destination.name ?? "Destination"}
                      </span>
                    </Popup>
                  )}
                  {popup === "truck" && (
                    <Popup
                      longitude={data.current_position.longitude}
                      latitude={data.current_position.latitude}
                      anchor="bottom"
                      offset={24}
                      closeButton={false}
                      onClose={() => setPopup(null)}
                    >
                      <span className="text-body-sm font-semibold">Current position</span>
                      <span className="block text-body-sm text-on-surface-variant">
                        {ago(data.current_position.recorded_at)}
                      </span>
                    </Popup>
                  )}
                </Map>

                {/* Trip stat card. Distance and drive time are the Directions
                    API's own numbers -- the map is the source, not decoration.
                    pointer-events-none so it never eats a pan gesture. */}
                {routeMeta && (
                  <div className="pointer-events-none absolute left-4 top-4 flex gap-4 rounded-xl border border-white/60 bg-white/85 px-4 py-2.5 shadow-[0_8px_24px_rgb(15_23_42/0.14)] backdrop-blur-md">
                    <div>
                      <span className="label !text-[10px]">Road distance</span>
                      <p className="text-body-md font-semibold tnum leading-tight">
                        {km(routeMeta.distance)}
                      </p>
                    </div>
                    <div className="w-px bg-outline-variant/60" />
                    <div>
                      {/* journeyComplete, not `delivered`: a trailer can finish
                          its run with no GOODS_RECEIVED row in the timeline, and
                          "Remaining 0 km" on an arrived vehicle reads as a bug */}
                      <span className="label !text-[10px]">
                        {journeyComplete ? "Total drive" : "Remaining"}
                      </span>
                      <p className="text-body-md font-semibold tnum leading-tight">
                        {journeyComplete
                          ? hrs(routeMeta.duration)
                          : `${km(routeMeta.distance * (1 - progressPct / 100))} · ${hrs(
                              routeMeta.duration * (1 - progressPct / 100),
                            )}`}
                      </p>
                    </div>
                  </div>
                )}
                </>
              ) : (
                <div className="flex h-full flex-col items-center justify-center gap-1 bg-surface-container-low text-center">
                  <Icon name="map" className="!text-[28px] text-outline" />
                  <p className="text-body-sm text-on-surface-variant">
                    Live map unavailable — no mapping token configured.
                  </p>
                  <p className="text-body-sm text-outline">
                    Set <span className="mono">VITE_MAPBOX_TOKEN</span> to enable it.
                  </p>
                </div>
              )}
            </div>
          )}

          {/* milestones */}
          <div className="flex flex-wrap justify-between gap-2 px-6 pb-6 pt-5">
            {MILESTONES.map((m, idx) => {
              const done = idx <= maxReachedIndex || reached.has(m.key);
              return (
                <div key={m.key} className="flex flex-1 flex-col items-center gap-1.5 text-center">
                  <span
                    className={`grid h-11 w-11 place-items-center rounded-full border-2 ${
                      done
                        ? "border-success bg-success-container text-success"
                        : "border-outline-variant bg-surface-container-low text-outline"
                    }`}
                  >
                    <Icon name={done ? "check" : m.icon} />
                  </span>
                  <span
                    className={`text-body-sm ${done ? "font-semibold text-on-surface" : "text-outline"}`}
                  >
                    {m.label}
                  </span>
                </div>
              );
            })}
          </div>
        </section>

        {/* ---- delivery details ---- */}
        <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[
            { label: "Carrier", value: data.shipment.carrier ?? "—", icon: "local_shipping" },
            { label: "Service", value: serviceLevel(data.trailer.priority), icon: "bolt" },
            { label: "Contents", value: data.trailer.load_type, icon: "inventory_2" },
            {
              label: "Journey completed",
              value: `${data.delivery_progress_pct}%`,
              icon: "route",
            },
          ].map((f) => (
            <div key={f.label} className="card-pad">
              <span className="label">{f.label}</span>
              <p className="mt-1 flex items-center gap-1.5 text-body-lg font-medium capitalize">
                <Icon name={f.icon} className="!text-[18px] text-primary" />
                {f.value}
              </p>
            </div>
          ))}
        </section>

        {/* ---- history ---- */}
        <section className="card">
          <header className="border-b border-outline-variant/60 px-6 py-4">
            <h2 className="flex items-center gap-2 text-headline-md">
              <Icon name="timeline" className="text-primary" />
              Tracking history
            </h2>
          </header>
          <ol className="p-6">
            {data.timeline.length === 0 && (
              <li className="text-body-md text-on-surface-variant">
                No movements recorded against this consignment yet.
              </li>
            )}
            {data.timeline.map((t, i) => {
              const last = i === data.timeline.length - 1;
              /* A folded run of GPS pings: one line, drawn quieter than a real
                 milestone, because "the vehicle kept driving" is context for
                 the events either side of it rather than an event itself. */
              if (t.collapsed) {
                const mins = Math.round(
                  (new Date(t.to ?? t.at).getTime() -
                    new Date(t.from ?? t.at).getTime()) / 60000,
                );
                const driving =
                  mins < 60 ? `${mins} minutes` : `${Math.floor(mins / 60)}h ${mins % 60}m`;
                return (
                  <li key={`tel-${i}`} className="flex gap-4">
                    <div className="flex flex-col items-center">
                      <span className="mt-1.5 h-3 w-3 shrink-0 rounded-full border border-dashed border-outline-variant" />
                      {!last && (
                        <span className="w-px flex-1 border-l border-dashed border-outline-variant" />
                      )}
                    </div>
                    <div className="pb-5">
                      <p className="text-body-md text-on-surface-variant">
                        In transit — tracked for {driving}
                      </p>
                      <p className="text-body-sm text-outline">
                        {t.count?.toLocaleString()} location reports · shown on the map above
                      </p>
                    </div>
                  </li>
                );
              }
              return (
                <li key={`${t.event_type}-${i}`} className="flex gap-4">
                  <div className="flex flex-col items-center">
                    {/* The gateway returns this oldest-first, so the live edge of
                        the journey is the LAST row -- that is the one to ring. */}
                    <span
                      className={`mt-1.5 h-3 w-3 shrink-0 rounded-full ${
                        last
                          ? "bg-primary-container ring-4 ring-primary-container/20"
                          : "bg-outline-variant"
                      }`}
                    />
                    {!last && <span className="w-px flex-1 bg-outline-variant" />}
                  </div>
                  <div className="pb-5">
                    <p className="text-body-md font-semibold capitalize">{label(t.event_type)}</p>
                    {t.summary && (
                      <p className="text-body-sm text-on-surface-variant">{t.summary}</p>
                    )}
                    <p className="text-body-sm text-outline">
                      {new Date(t.at).toLocaleString([], {
                        day: "2-digit",
                        month: "short",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}{" "}
                      · {ago(t.at)}
                    </p>
                  </div>
                </li>
              );
            })}
          </ol>
        </section>
      </div>
    </Portal>
  );
}
