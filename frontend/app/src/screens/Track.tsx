import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { Badge, ErrorNote, Icon, Panel, Spinner, ago, clock, statusTone } from "../components/ui";

/**
 * Customer-facing delivery tracker.
 *
 * This is the brief's E2 requirement #1 -- "accept a tracking number, trailer
 * ID, or shipment reference" -- and the reason /track/{ref} resolves all three
 * server-side rather than making the caller know which one they hold.
 */

interface TrackResult {
  reference: string;
  resolved_as: { entity_type: string; entity_id: string };
  trailer: { id: string; status: string; eta: string | null; priority: string; load_type: string };
  shipment: { id: string; po_id: string; carrier: string | null; tracking_number: string | null;
    expected_arrival: string | null };
  dock: { dock_id: string; status: string; docked_at: string | null } | null;
  origin: { name: string | null };
  destination: { name: string | null };
  current_position: { latitude: number; longitude: number; recorded_at: string } | null;
  timeline: { event_type: string; at: string; summary: string | null }[];
  delivery_progress_pct: number;
}

const MILESTONES = [
  { key: "TRAILER_DEPARTED", label: "Departed", icon: "factory" },
  { key: "TRAILER_LOCATION_UPDATED", label: "In transit", icon: "local_shipping" },
  { key: "TRAILER_ARRIVED", label: "Arrived at yard", icon: "flag" },
  { key: "TRAILER_DOCKED", label: "Docked", icon: "dock" },
  { key: "GOODS_RECEIVED", label: "Delivered", icon: "inventory_2" },
];

export default function Track() {
  const { ref } = useParams();
  const [data, setData] = useState<TrackResult | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  if (error) return <ErrorNote error={error} />;
  if (!data) return <Spinner label="Tracking" />;

  const reached = new Set(data.timeline.map((t) => t.event_type));

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="label">Tracking {data.reference}</p>
          <h1 className="text-display">{data.trailer.id}</h1>
          <p className="text-body-lg text-on-surface-variant">
            {data.shipment.carrier ?? "Carrier"} · {data.origin.name ?? "origin"} →{" "}
            {data.destination.name ?? "destination"}
          </p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <Badge tone={statusTone(data.trailer.status)}>{data.trailer.status.replace("_", " ")}</Badge>
          <Link to={`/traceability/${data.shipment.po_id}`} className="text-body-sm text-primary hover:underline">
            View {data.shipment.po_id} traceability →
          </Link>
        </div>
      </header>

      <div className="card-pad">
        <div className="flex items-end justify-between">
          <div>
            <span className="label">Estimated arrival</span>
            <p className="text-headline-lg tnum">{clock(data.trailer.eta)}</p>
          </div>
          <div className="text-right">
            <span className="label">Progress</span>
            <p className="text-headline-lg tnum text-primary">{data.delivery_progress_pct}%</p>
          </div>
        </div>
        <div className="mt-3 h-2 overflow-hidden rounded-full bg-surface-container-high">
          <div
            className="h-full rounded-full bg-primary-container transition-all"
            style={{ width: `${data.delivery_progress_pct}%` }}
          />
        </div>
        <div className="mt-5 flex flex-wrap justify-between gap-3">
          {MILESTONES.map((m) => {
            const done = reached.has(m.key);
            return (
              <div key={m.key} className="flex flex-1 flex-col items-center gap-1 text-center">
                <span
                  className={`grid h-9 w-9 place-items-center rounded-full ${
                    done ? "bg-primary-container text-on-primary" : "bg-surface-container-high text-outline"
                  }`}
                >
                  <Icon name={m.icon} className="!text-[18px]" />
                </span>
                <span className={`text-body-sm ${done ? "font-semibold" : "text-outline"}`}>
                  {m.label}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-4">
        {[
          { label: "Tracking number", value: data.shipment.tracking_number ?? "—", mono: true },
          { label: "Load type", value: data.trailer.load_type },
          { label: "Priority", value: data.trailer.priority },
          { label: "Dock", value: data.dock?.dock_id ?? "Not assigned", mono: true },
        ].map((f) => (
          <div key={f.label} className="card-pad">
            <span className="label">{f.label}</span>
            <p className={`mt-1 text-body-lg ${f.mono ? "mono" : ""}`}>{f.value}</p>
          </div>
        ))}
      </div>

      <Panel title="Delivery history" icon="timeline">
        <ol className="p-5">
          {data.timeline.map((t, i) => (
            <li key={i} className="flex gap-3">
              <div className="flex flex-col items-center">
                <span className="mt-1.5 h-2.5 w-2.5 rounded-full bg-primary-container" />
                {i < data.timeline.length - 1 && <span className="w-px flex-1 bg-outline-variant" />}
              </div>
              <div className="pb-4">
                <p className="mono font-semibold">{t.event_type}</p>
                <p className="text-body-sm text-on-surface-variant">{t.summary ?? "—"}</p>
                <p className="text-body-sm text-outline">{ago(t.at)}</p>
              </div>
            </li>
          ))}
        </ol>
      </Panel>
    </div>
  );
}
