import { useCallback, useEffect, useState } from "react";
import {
  LoadPlanLine,
  OutboundOrderDetail,
  OutboundOrderSummary,
  YARD,
  api,
} from "../api";
import { useAuth } from "../auth";
import {
  Badge,
  Empty,
  ErrorNote,
  Icon,
  Panel,
  Spinner,
  Tone,
  ago,
  clock,
  statusTone,
} from "../components/ui";
import { LiveEvent, useRefetchOn } from "../hooks/useEventStream";

/**
 * Outbound Operations (v7).
 *
 * The screen deliberately reads as a PIPELINE, not a table with a status
 * column: an outbound order is only ever in one of six places, and which place
 * is the question an operator actually has. The stage rail across the top is
 * therefore the primary navigation, and the list below it is a filtered view of
 * one stage.
 *
 * Every action button here is the real endpoint, gated on the real capability.
 * A user without `outbound:write` sees the buttons disabled rather than hidden,
 * because "you cannot do this" is more useful to a demo audience than a control
 * that silently is not there.
 */

const STAGES = [
  { key: "PLANNED", label: "Load Planned", icon: "list_alt",
    hint: "Pick lines released, picking not started" },
  { key: "STAGED", label: "Staged", icon: "inventory_2",
    hint: "Picked to the staging lane, awaiting a dock door" },
  { key: "LOADING", label: "At Dock Door", icon: "dock",
    hint: "Vehicle berthed and loading" },
  { key: "SHIPPED", label: "Dispatched", icon: "local_shipping",
    hint: "Goods issued, vehicle has cleared the gate" },
  { key: "DELIVERED", label: "Delivered", icon: "task_alt",
    hint: "Delivery confirmed by the customer" },
] as const;

function priorityTone(priority?: string | null): Tone {
  switch ((priority ?? "").toLowerCase()) {
    case "critical":
      return "danger";
    case "high":
      return "warning";
    case "normal":
      return "info";
    default:
      return "neutral";
  }
}

/** A pick line's fill, with the short-pick case called out rather than rounded away. */
function LineBar({ line }: { line: LoadPlanLine }) {
  const staged = line.qty_ordered ? (line.qty_staged / line.qty_ordered) * 100 : 0;
  const loaded = line.qty_ordered ? (line.qty_loaded / line.qty_ordered) * 100 : 0;
  const short = line.status === "SHORT";
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-body-md">{line.material_name ?? line.material_id}</span>
        <span className="mono tnum text-on-surface-variant">
          {line.qty_loaded > 0 ? `${line.qty_loaded} loaded` : `${line.qty_staged} staged`}
          {" / "}
          {line.qty_ordered}
        </span>
      </div>
      <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-surface-container-high">
        <div
          className={`absolute inset-y-0 left-0 rounded-full ${short ? "bg-warning" : "bg-info"}`}
          style={{ width: `${Math.min(100, staged)}%` }}
        />
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-success"
          style={{ width: `${Math.min(100, loaded)}%` }}
        />
      </div>
      {short && (
        <span className="text-body-sm text-warning">
          Short-picked by {line.qty_ordered - line.qty_staged} — ships as a partial
        </span>
      )}
    </div>
  );
}

export default function Outbound({ events }: { events: LiveEvent[] }) {
  const { user } = useAuth();
  const canWrite = user?.permissions.includes("outbound:write") ?? false;

  const [orders, setOrders] = useState<OutboundOrderSummary[]>([]);
  const [stage, setStage] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<OutboundOrderDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    return api
      .yard<{ orders: OutboundOrderSummary[] }>("/outbound-orders?limit=100")
      .then((d) => {
        setOrders(d.orders);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  // REST on mount, WebSocket deltas after -- README §5. The screen is never
  // reconstructed from the event stream, only refreshed by it.
  useEffect(() => {
    load();
  }, [load]);

  useRefetchOn(
    events,
    ["OUTBOUND_ORDER_CREATED", "LOAD_PLAN_CREATED", "LOAD_STAGED", "GOODS_ISSUED",
     "OUTBOUND_DELIVERED", "DOCK_ASSIGNED", "DOCK_REASSIGNED", "TRAILER_DOCKED",
     "TRAILER_ARRIVED", "TRAILER_EXITED"],
    load,
  );

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    api
      .yard<OutboundOrderDetail>(`/outbound-orders/${selected}`)
      .then(setDetail)
      .catch((e) => setError(e.message));
  }, [selected, orders]);

  async function act(path: string, label: string) {
    setBusy(label);
    setError(null);
    try {
      await api.post(YARD, path, {});
      await load();
      if (selected) {
        setDetail(await api.yard<OutboundOrderDetail>(`/outbound-orders/${selected}`));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Outbound action failed");
    } finally {
      setBusy(null);
    }
  }

  const counts = STAGES.map((s) => ({
    ...s,
    count: orders.filter((o) => o.status === s.key).length,
  }));
  const shown = stage ? orders.filter((o) => o.status === stage) : orders;

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-display">Outbound Fulfilment &amp; Dispatch</h1>
        <p className="text-body-lg text-on-surface-variant">
          Customer orders through picking, staging, dock loading and proof of delivery.
          Outbound vehicles contend for the same dock doors as inbound ones, inside the
          same automated schedule.
        </p>
      </header>

      {error && <ErrorNote error={error} />}

      {/* ---- stage rail: the primary navigation, not decoration ---- */}
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {counts.map((s) => {
          const active = stage === s.key;
          return (
            <button
              key={s.key}
              type="button"
              title={s.hint}
              onClick={() => setStage(active ? null : s.key)}
              className={`card-pad flex flex-col gap-2 text-left transition ${
                active
                  ? "ring-2 ring-primary-container"
                  : "hover:bg-surface-container-low"
              }`}
            >
              <span className="flex items-center gap-2 label">
                <Icon name={s.icon} className="!text-[18px] text-primary" />
                {s.label}
              </span>
              <span className="text-display tnum leading-none">{s.count}</span>
            </button>
          );
        })}
      </div>

      {stage && (
        <button
          type="button"
          onClick={() => setStage(null)}
          className="self-start text-body-sm text-primary hover:underline"
        >
          ← Showing {stage} only. Show all {orders.length}.
        </button>
      )}

      <div className="grid gap-6 xl:grid-cols-[1.3fr_1fr]">
        <Panel
          title={`Orders (${shown.length})`}
          icon="orders"
          action={
            <button
              type="button"
              onClick={load}
              className="text-body-sm text-primary hover:underline"
            >
              Refresh
            </button>
          }
        >
          {loading ? (
            <Spinner label="Loading outbound orders" />
          ) : shown.length === 0 ? (
            <Empty
              message="No outbound orders in this stage"
              hint="Orders appear here as soon as customer demand is released to the warehouse."
            />
          ) : (
            <table className="w-full text-body-md">
              <thead className="sticky top-0 bg-surface-container-lowest">
                <tr className="border-b border-outline-variant/60 text-left">
                  <th className="px-5 py-2 label">Order</th>
                  <th className="px-3 py-2 label">Customer</th>
                  <th className="px-3 py-2 label">Pick progress</th>
                  <th className="px-3 py-2 label">Vehicle</th>
                  <th className="px-3 py-2 label">Dock door</th>
                  <th className="px-3 py-2 label">Status</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((o) => (
                  <tr
                    key={o.id}
                    onClick={() => setSelected(o.id)}
                    className={`cursor-pointer border-b border-outline-variant/40 hover:bg-surface-container-low ${
                      selected === o.id ? "bg-surface-container-low" : ""
                    }`}
                  >
                    <td className="px-5 py-2.5">
                      <span className="mono font-semibold text-primary">{o.id}</span>
                      <span className="ml-2">
                        <Badge tone={priorityTone(o.priority)}>{o.priority}</Badge>
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="block">{o.customer_name}</span>
                      <span className="block text-body-sm text-on-surface-variant">
                        {o.destination ?? "—"} · {o.line_count} line
                        {o.line_count === 1 ? "" : "s"}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="tnum">{o.staged_pct}%</span>
                      <span className="ml-1 text-body-sm text-on-surface-variant">
                        ({o.qty_staged}/{o.qty_ordered})
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      {o.trailer ? (
                        <>
                          <span className="mono">{o.trailer.id}</span>
                          <span className="block text-body-sm text-on-surface-variant">
                            {o.trailer.status}
                            {o.trailer.eta ? ` · ETA ${clock(o.trailer.eta)}` : ""}
                          </span>
                        </>
                      ) : (
                        <span className="text-outline">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 mono">
                      {o.dock_assignment?.dock_id ?? <span className="text-outline">—</span>}
                    </td>
                    <td className="px-3 py-2.5">
                      <Badge tone={statusTone(o.status)}>{o.status}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        {/* ---- detail ---- */}
        <Panel title={detail ? detail.id : "Order Detail"} icon="inventory">
          {!detail ? (
            <Empty
              message="Select an order"
              hint="Its pick lines, vehicle, dock history and audit trail appear here."
            />
          ) : (
            <div className="flex flex-col gap-5 p-5">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-headline-md">{detail.customer_name}</span>
                  <Badge tone={statusTone(detail.status)}>{detail.status}</Badge>
                  <Badge tone={priorityTone(detail.priority)}>{detail.priority}</Badge>
                </div>
                <p className="text-body-sm text-on-surface-variant">
                  {detail.destination.name ?? "no destination set"} · created{" "}
                  {ago(detail.created_at)}
                </p>
              </div>

              {/* ---- actions: the real endpoints, gated on the real capability ---- */}
              <div className="flex flex-wrap gap-2">
                {detail.status === "PLANNED" && (
                  <button
                    type="button"
                    disabled={!canWrite || busy !== null}
                    onClick={() => act(`/outbound-orders/${detail.id}/stage`, "stage")}
                    className="btn-primary disabled:opacity-40"
                  >
                    <Icon name="inventory_2" /> {busy === "stage" ? "Staging…" : "Release to staging"}
                  </button>
                )}
                {detail.status === "STAGED" && !detail.trailer && (
                  <button
                    type="button"
                    disabled={!canWrite || busy !== null}
                    onClick={() =>
                      act(`/outbound-orders/${detail.id}/dispatch`, "dispatch")
                    }
                    className="btn-primary disabled:opacity-40"
                  >
                    <Icon name="local_shipping" />{" "}
                    {busy === "dispatch" ? "Dispatching…" : "Assign vehicle"}
                  </button>
                )}
                {detail.trailer?.status === "DOCKED" && (
                  <button
                    type="button"
                    disabled={!canWrite || busy !== null}
                    onClick={() => act(`/trailers/${detail.trailer!.id}/load`, "load")}
                    className="btn-primary disabled:opacity-40"
                  >
                    <Icon name="package_2" />{" "}
                    {busy === "load" ? "Issuing…" : "Complete load & post GI"}
                  </button>
                )}
                {detail.trailer?.status === "LOADED" && (
                  <button
                    type="button"
                    disabled={!canWrite || busy !== null}
                    onClick={() => act(`/trailers/${detail.trailer!.id}/depart`, "depart")}
                    className="btn-primary disabled:opacity-40"
                  >
                    <Icon name="logout" /> {busy === "depart" ? "Releasing…" : "Gate out"}
                  </button>
                )}
                {detail.trailer?.status === "DEPARTED" && (
                  <button
                    type="button"
                    disabled={!canWrite || busy !== null}
                    onClick={() =>
                      act(`/trailers/${detail.trailer!.id}/deliver`, "deliver")
                    }
                    className="btn-primary disabled:opacity-40"
                  >
                    <Icon name="task_alt" />{" "}
                    {busy === "deliver" ? "Confirming…" : "Confirm proof of delivery"}
                  </button>
                )}
                {!canWrite && (
                  <p className="text-body-sm text-on-surface-variant">
                    Your role is read-only on this screen — outbound actions require{" "}
                    <span className="mono">outbound:write</span>.
                  </p>
                )}
              </div>

              <div>
                <span className="label">Pick lines</span>
                <div className="mt-2 flex flex-col gap-3">
                  {detail.lines.map((l) => (
                    <LineBar key={l.load_plan_id} line={l} />
                  ))}
                </div>
              </div>

              {detail.trailer && (
                <div className="rounded-lg border border-outline-variant/60 bg-surface-container-low p-3">
                  <span className="label">Collecting vehicle</span>
                  <p className="mt-1">
                    <span className="mono font-semibold">{detail.trailer.id}</span>{" "}
                    <Badge tone={statusTone(detail.trailer.status)}>
                      {detail.trailer.status}
                    </Badge>
                  </p>
                  <p className="text-body-sm text-on-surface-variant">
                    {detail.shipment?.carrier ?? "carrier unknown"} ·{" "}
                    {detail.trailer.load_type ?? "—"}
                    {detail.trailer.eta ? ` · ETA ${clock(detail.trailer.eta)}` : ""}
                  </p>
                </div>
              )}

              {detail.dock_assignments.length > 0 && (
                <div>
                  <span className="label">Dock door history</span>
                  <ul className="mt-2 flex flex-col gap-1">
                    {detail.dock_assignments.map((da) => (
                      <li key={da.id} className="flex items-center gap-2 text-body-sm">
                        <span className="mono font-semibold">{da.dock_id}</span>
                        <Badge tone={statusTone(da.status)}>{da.status}</Badge>
                        <span className="text-on-surface-variant">
                          {da.planned_start ? clock(da.planned_start) : "—"}
                          {da.planned_end ? `–${clock(da.planned_end)}` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {detail.goods_issue && (
                <div className="rounded-lg border border-success/30 bg-success-container/40 p-3">
                  <span className="label">Goods Issue (GI) posted</span>
                  <p className="mt-1">
                    <span className="mono font-semibold">{detail.goods_issue.id}</span> ·{" "}
                    <span className="tnum">{detail.goods_issue.qty_issued}</span> units ·{" "}
                    {clock(detail.goods_issue.issued_at)}
                  </p>
                  <p className="text-body-sm text-on-surface-variant">
                    verified by {detail.goods_issue.verified_by}
                  </p>
                </div>
              )}

              <div>
                <span className="label">Audit trail ({detail.timeline.length})</span>
                <ol className="mt-2 flex flex-col gap-2">
                  {detail.timeline.map((e) => (
                    <li key={e.event_id} className="flex gap-2 text-body-sm">
                      <span className="mono text-outline shrink-0">{clock(e.timestamp)}</span>
                      <span className="min-w-0">
                        <span className="mono font-semibold">{e.event_type}</span>
                        <span className="block text-on-surface-variant">
                          {(e.payload?.summary as string) ?? e.entity_id}
                        </span>
                      </span>
                    </li>
                  ))}
                </ol>
              </div>
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
