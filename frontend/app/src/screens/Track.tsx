import { ReactNode, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { MapContainer, TileLayer, Marker, Popup, Polyline, GeoJSON } from "react-leaflet";
import L from "leaflet";
import { api } from "../api";
import { useAuth } from "../auth";
import { Icon, ago, clock } from "../components/ui";

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
  timeline: { event_type: string; at: string; summary: string | null }[];
  delivery_progress_pct: number;
}

const truckIcon = L.divIcon({
  className: "bg-transparent border-none",
  html: `<div class="flex h-10 w-10 items-center justify-center rounded-full border-2 border-white bg-primary text-white shadow-lg"><span class="material-symbols-outlined text-[20px]">local_shipping</span></div>`,
  iconSize: [40, 40],
  iconAnchor: [20, 20],
});

const originIcon = L.divIcon({
  className: "bg-transparent border-none",
  html: `<div class="flex h-8 w-8 items-center justify-center rounded-full border-2 border-primary bg-white text-primary shadow-sm"><span class="material-symbols-outlined text-[16px]">factory</span></div>`,
  iconSize: [32, 32],
  iconAnchor: [16, 16],
});

const destIcon = L.divIcon({
  className: "bg-transparent border-none",
  html: `<div class="flex h-8 w-8 items-center justify-center rounded-full border-2 border-success bg-white text-success shadow-sm"><span class="material-symbols-outlined text-[16px]">flag</span></div>`,
  iconSize: [32, 32],
  iconAnchor: [16, 16],
});

const MILESTONES = [
  { key: "TRAILER_DEPARTED", label: "Picked up", icon: "factory" },
  { key: "TRAILER_LOCATION_UPDATED", label: "In transit", icon: "local_shipping" },
  { key: "TRAILER_ARRIVED", label: "At destination", icon: "flag" },
  { key: "TRAILER_DOCKED", label: "Unloading", icon: "dock" },
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

/** The portal shell: masthead, page body, footer. No app chrome anywhere. */
function Portal({ children }: { children: ReactNode }) {
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
              <span className="h-2 w-2 rounded-full bg-success-container animate-pulse" />
              Updating live
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
          Status refreshes automatically every 8 seconds. Times shown in your local timezone.
          Questions about this delivery? Quote the consignment reference above.
        </p>
      </footer>
    </div>
  );
}

export default function Track() {
  const { ref } = useParams();
  const [data, setData] = useState<TrackResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [routeGeoJSON, setRouteGeoJSON] = useState<any>(null);

  useEffect(() => {
    if (data?.origin.latitude && data?.destination.latitude && !routeGeoJSON) {
      const lon1 = data.origin.longitude;
      const lat1 = data.origin.latitude;
      const lon2 = data.destination.longitude;
      const lat2 = data.destination.latitude;
      fetch(`https://router.project-osrm.org/route/v1/driving/${lon1},${lat1};${lon2},${lat2}?overview=full&geometries=geojson`)
        .then(res => res.json())
        .then(routeData => {
           if (routeData.code === "Ok") {
              setRouteGeoJSON(routeData.routes[0].geometry);
           }
        })
        .catch(err => console.error("OSRM fetch failed", err));
    }
  }, [data?.origin.latitude, data?.destination.latitude]);

  useEffect(() => {
    if (!ref) return;
    const load = () =>
      api
        .gateway<TrackResult>(`/track/${ref}`)
        .then((d) => {
          setData(d);
          setError(null);
        })
        .catch((e) => setError(e.message));
    load();
    const timer = window.setInterval(load, 8000);
    return () => window.clearInterval(timer);
  }, [ref]);

  if (error) {
    return (
      <Portal>
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
      <Portal>
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

  return (
    <Portal>
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

          {/* live map */}
          {data.origin.latitude && data.destination.latitude && data.current_position && (
            <div className="h-[300px] w-full mt-6 border-y border-outline-variant/30 relative z-0">
              <MapContainer 
                bounds={[
                  [data.origin.latitude, data.origin.longitude!],
                  [data.destination.latitude, data.destination.longitude!]
                ]}
                zoomControl={false}
                scrollWheelZoom={false}
                dragging={false}
                className="h-full w-full z-0"
              >
                <TileLayer
                  attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
                  url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
                />
                
                {routeGeoJSON ? (
                  <GeoJSON data={routeGeoJSON} style={{ color: "#4f46e5", weight: 5, opacity: 0.6 }} />
                ) : (
                  <Polyline 
                    positions={[
                      [data.origin.latitude, data.origin.longitude!],
                      [data.destination.latitude, data.destination.longitude!]
                    ]} 
                    color="#4f46e5" 
                    weight={4} 
                    dashArray="10, 10" 
                    opacity={0.5} 
                  />
                )}
                
                <Marker position={[data.origin.latitude, data.origin.longitude!]} icon={originIcon}>
                  <Popup>{data.origin.name}</Popup>
                </Marker>
                
                <Marker position={[data.destination.latitude, data.destination.longitude!]} icon={destIcon}>
                  <Popup>{data.destination.name}</Popup>
                </Marker>
                
                <Marker position={[data.current_position.latitude, data.current_position.longitude]} icon={truckIcon} zIndexOffset={1000}>
                  <Popup>Current Position</Popup>
                </Marker>
              </MapContainer>
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
            { label: "Service", value: data.trailer.priority, icon: "bolt" },
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
            {data.timeline.map((t, i) => (
              <li key={`${t.event_type}-${i}`} className="flex gap-4">
                <div className="flex flex-col items-center">
                  {/* The gateway returns this oldest-first, so the live edge of
                      the journey is the LAST row -- that is the one to ring. */}
                  <span
                    className={`mt-1.5 h-3 w-3 shrink-0 rounded-full ${
                      i === data.timeline.length - 1
                        ? "bg-primary-container ring-4 ring-primary-container/20"
                        : "bg-outline-variant"
                    }`}
                  />
                  {i < data.timeline.length - 1 && (
                    <span className="w-px flex-1 bg-outline-variant" />
                  )}
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
            ))}
          </ol>
        </section>
      </div>
    </Portal>
  );
}
