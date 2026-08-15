import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AtRiskItem, Overview, PipelineStage, api } from "../api";
import {
  Badge,
  Empty,
  ErrorNote,
  Icon,
  KpiTile,
  Panel,
  Spinner,
  ago,
  money,
  pct,
  severityTone,
} from "../components/ui";
import { LiveEvent, useRefetchOn } from "../hooks/useEventStream";
import TrailerMap from "../components/TrailerMap";

const STAGE_ICON: Record<string, string> = {
  requisition: "description",
  sourcing: "neurology",
  po: "shopping_cart",
  transit: "local_shipping",
  receiving: "inventory_2",
  match: "rule",
  payment: "account_balance",
};

export default function ControlTower({ events }: { events: LiveEvent[] }) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [stages, setStages] = useState<PipelineStage[]>([]);
  const [atRisk, setAtRisk] = useState<AtRiskItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  // REST on mount = current state. The WebSocket only tells us WHEN to re-read
  // (README §5): the stream carries changes, never the whole picture.
  const load = useCallback(() => {
    Promise.all([
      api.gateway<Overview>("/dashboard/overview"),
      api.gateway<{ stages: PipelineStage[] }>("/dashboard/pipeline"),
      api.gateway<{ at_risk: AtRiskItem[] }>("/dashboard/at-risk"),
    ])
      .then(([o, p, r]) => {
        setOverview(o);
        setStages(p.stages);
        setAtRisk(r.at_risk);
        setError(null);
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(load, [load]);
  useRefetchOn(
    events,
    [
      "MATCH_COMPLETED", "EXCEPTION_CREATED", "EXCEPTION_RESOLVED", "PAYMENT_APPROVED",
      "PAYMENT_PAID", "GOODS_RECEIVED", "PO_CREATED", "PO_STATUS_CHANGED",
      "TRAILER_DEPARTED", "TRAILER_ARRIVED", "ALERT_CREATED",
    ],
    load,
  );

  if (error) return <ErrorNote error={error} />;
  if (!overview) return <Spinner label="Loading control tower" />;

  const k = overview.kpis;

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-display">Mission Control</h1>
        <p className="text-body-lg text-on-surface-variant">
          Real-time health of the inbound-to-pay pipeline.
        </p>
      </header>

      {/* ---- KPI tiles. Every figure is measured from this run (README §8). ---- */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <KpiTile
          label="First-pass match rate"
          value={pct(k.first_pass_match_rate)}
          icon="trending_up"
          tone="success"
          progress={k.first_pass_match_rate * 100}
          sub={<span className="text-body-sm text-on-surface-variant">auto-approved</span>}
        />
        <KpiTile
          label="Touchless"
          value={pct(k.touchless_rate)}
          icon="auto_awesome"
          tone="primary"
          progress={k.touchless_rate * 100}
          sub={<span className="text-body-sm text-on-surface-variant">zero human touch</span>}
        />
        <KpiTile
          label="Dock utilisation"
          value={pct(k.dock_utilisation, 0)}
          icon="warehouse"
          tone={k.dock_utilisation > 0.9 ? "warning" : "info"}
          progress={k.dock_utilisation * 100}
          sub={
            <span className="text-body-sm text-on-surface-variant">
              {overview.docks_occupied}/{overview.docks_total} doors
            </span>
          }
        />
        <KpiTile
          label="Human intervention required"
          value={overview.open_exceptions}
          icon="person_alert"
          tone="danger"
          sub={
            overview.critical_exceptions > 0 ? (
              <Badge tone="danger">{overview.critical_exceptions} critical</Badge>
            ) : undefined
          }
        />
      </div>

      {/* ---- secondary KPI strip ---- */}
      <div className="grid gap-4 sm:grid-cols-3">
        <div className="card-pad">
          <span className="label">Avg truck turnaround</span>
          <p className="mt-1 text-headline-lg tnum">
            {k.avg_turnaround_minutes !== null ? `${k.avg_turnaround_minutes} min` : "—"}
          </p>
          <p className="text-body-sm text-on-surface-variant">dock assignment → goods receipt</p>
        </div>
        <div className="card-pad">
          <span className="label">Avg P2P cycle time</span>
          <p className="mt-1 text-headline-lg tnum">
            {k.avg_p2p_cycle_hours !== null ? `${k.avg_p2p_cycle_hours} hrs` : "—"}
          </p>
          <p className="text-body-sm text-on-surface-variant">PO raised → payment approved</p>
        </div>
        <div className="card-pad">
          <span className="label">Active trailers</span>
          <p className="mt-1 text-headline-lg tnum">{overview.active_trailers}</p>
          <p className="text-body-sm text-on-surface-variant">
            {overview.pending_invoices} invoice(s) awaiting match
          </p>
        </div>
      </div>

      {/* ---- pipeline funnel ---- */}
      <Panel title="Pipeline Volume & Health" icon="linear_scale">
        <div className="flex items-start gap-2 overflow-x-auto px-5 py-6">
          {stages.map((s, i) => (
            <div key={s.key} className="flex items-start gap-2">
              <div className="flex w-28 shrink-0 flex-col items-center gap-2 text-center">
                <span className="rounded-md bg-surface-container px-2 py-0.5 text-body-sm font-semibold tnum">
                  {s.count}
                </span>
                <div className="grid h-12 w-12 place-items-center rounded-full border border-outline-variant bg-surface-container-lowest">
                  <Icon name={STAGE_ICON[s.key] ?? "circle"} className="text-primary" />
                </div>
                <span className="text-body-sm font-medium">{s.label}</span>
                {s.delayed ? <Badge tone="warning">{s.delayed} delayed</Badge> : null}
                {s.exceptions ? <Badge tone="danger">{s.exceptions} exceptions</Badge> : null}
                {s.in_progress ? <Badge tone="primary">{s.in_progress} unloading</Badge> : null}
              </div>
              {i < stages.length - 1 && (
                <div className="mt-11 h-px w-6 shrink-0 bg-outline-variant" />
              )}
            </div>
          ))}
        </div>
      </Panel>

      {/* ---- live map ---- */}
      <Panel title="Live Inbound Tracker" icon="map">
        <TrailerMap height={380} />
      </Panel>

      {/* ---- at risk ---- */}
      <Panel
        title="At-Risk Orders & Exceptions"
        icon="warning"
        action={
          <Link to="/exceptions" className="text-body-sm font-semibold text-primary hover:underline">
            View all ({overview.open_exceptions})
          </Link>
        }
      >
        {atRisk.length === 0 ? (
          <Empty message="Nothing at risk right now." hint="Exceptions and delays appear here." />
        ) : (
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className="th">Reference</th>
                <th className="th">Issue</th>
                <th className="th">Supplier</th>
                <th className="th">Impact</th>
                <th className="th">Age</th>
                <th className="th">Owner</th>
              </tr>
            </thead>
            <tbody>
              {atRisk.map((r) => (
                <tr key={`${r.kind}-${r.entity_id}`} className="hover:bg-surface-container-low">
                  <td className="td">
                    <Link
                      to={
                        r.reference_id.startsWith("PO-")
                          ? `/traceability/${r.reference_id}`
                          : r.reference_id.startsWith("TRL-")
                            ? `/track/${r.reference_id}`
                            : "/exceptions"
                      }
                      className="mono font-semibold text-primary hover:underline"
                    >
                      {r.reference_id}
                    </Link>
                  </td>
                  <td className="td">
                    <Badge tone={severityTone(r.severity)}>{r.issue_type.replace(/_/g, " ")}</Badge>
                  </td>
                  <td className="td text-on-surface-variant">{r.supplier ?? "—"}</td>
                  <td className="td tnum">{r.value !== null ? money(r.value) : "—"}</td>
                  <td className="td text-on-surface-variant">{ago(r.created_at)}</td>
                  <td className="td text-on-surface-variant">{r.owner}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
