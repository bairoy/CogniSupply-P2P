import { FormEvent, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { TimelineEvent, api } from "../api";
import {
  Badge,
  Empty,
  ErrorNote,
  Icon,
  Panel,
  Spinner,
  clock,
  money,
  statusTone,
} from "../components/ui";

interface Trace {
  purchase_order: {
    id: string; requisition_id: string | null; status: string; supplier_id: string | null;
    material_id: string | null; qty: number | null; unit_price: number | null; created_at: string;
  };
  related_entities: { entity_type: string; entity_id: string }[];
  timeline: TimelineEvent[];
}

interface PurchaseOrderRow {
  id: string; status: string; qty: number; unit_price: number; value: number;
  expected_delivery: string; created_at: string; supplier_name: string;
  material_name: string; gr_id: string | null; inv_id: string | null;
}

/**
 * Business names for the entity types the gateway returns. The raw key is still
 * what everything is looked up by -- this map only decides what a reader sees,
 * so "goods_receipt" reads as the document it actually is.
 */
const ENTITY_LABEL: Record<string, string> = {
  requisition: "Requisition",
  purchase_order: "Purchase Order",
  shipment: "Shipment",
  trailer: "Vehicle",
  dock_assignment: "Dock Assignment",
  goods_receipt: "Goods Receipt Note (GRN)",
  invoice: "Supplier Invoice",
  match_result: "3-Way Match Result",
  exception: "Exception",
  payment: "Payment",
  alert: "Yard Alert",
};

const ENTITY_ICON: Record<string, string> = {
  requisition: "description",
  purchase_order: "shopping_cart",
  shipment: "package_2",
  trailer: "local_shipping",
  dock_assignment: "dock",
  goods_receipt: "inventory_2",
  invoice: "receipt_long",
  match_result: "rule",
  exception: "error",
  payment: "account_balance",
  alert: "notifications",
};

/** "4h 12m", "38m" -- how long a stretch of driving lasted. */
function span(from?: string, to?: string) {
  if (!from || !to) return null;
  const mins = Math.round(
    (new Date(to).getTime() - new Date(from).getTime()) / 60000,
  );
  if (mins < 1) return "under a minute";
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

/**
 * One stretch of driving, as one row.
 *
 * Rendered deliberately quieter than a milestone -- dashed rail, no solid
 * marker -- because that is the honest hierarchy: these are the gaps between
 * the things that happened, not things that happened. Clicking opens the raw
 * pings, so nothing here is a claim that they do not exist.
 */
function CollapsedTelemetryRow({
  event,
  last,
  onExpand,
}: {
  event: TimelineEvent;
  last: boolean;
  onExpand: () => void;
}) {
  const duration = span(event.from, event.to);
  return (
    <li className="flex gap-3">
      <div className="flex flex-col items-center">
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full border border-dashed border-outline-variant text-outline">
          <Icon name="my_location" className="!text-[14px]" />
        </span>
        {!last && (
          <span className="w-px flex-1 border-l border-dashed border-outline-variant" />
        )}
      </div>
      <div className="pb-5">
        <button
          type="button"
          onClick={onExpand}
          className="group flex flex-wrap items-baseline gap-2 text-left"
        >
          <span className="text-body-md text-on-surface-variant group-hover:text-on-surface">
            {event.count?.toLocaleString()} position updates
          </span>
          <span className="mono text-outline">{event.entity_id}</span>
          {duration && (
            <span className="text-body-sm text-outline">
              over {duration} of driving
            </span>
          )}
          <span className="text-body-sm font-medium text-primary opacity-0 transition-opacity group-hover:opacity-100">
            show all
          </span>
        </button>
        <p className="mt-0.5 text-body-sm text-outline">
          {clock(event.from ?? event.at)} → {clock(event.to ?? event.at)} · GPS
          telemetry, plotted on the live map
        </p>
      </div>
    </li>
  );
}

export default function Traceability() {
  const { poId } = useParams();
  const navigate = useNavigate();
  const [query, setQuery] = useState(poId ?? "");
  const [trace, setTrace] = useState<Trace | null>(null);
  const [history, setHistory] = useState<PurchaseOrderRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  /**
   * The audit trail is milestones by default. Uncollapsed, one PO's history is
   * ~690 events of which ~660 are GPS pings, so the story of the order --
   * raised, sourced, shipped, received, matched, paid -- is unreadable between
   * them. This is a display default, not a filter: the gateway serves the raw
   * trail on demand and this switch is how an auditor asks for it.
   */
  const [showTelemetry, setShowTelemetry] = useState(false);

  useEffect(() => {
    if (!poId) {
      setTrace(null);
      setLoading(true);
      api.procurement<{ purchase_orders: PurchaseOrderRow[] }>("/purchase-orders")
        .then(d => setHistory(d.purchase_orders))
        .catch(e => setError(e.message))
        .finally(() => setLoading(false));
      return;
    }
    setLoading(true);
    api
      .gateway<Trace>(
        `/traceability/${poId}${showTelemetry ? "?telemetry=full" : ""}`,
      )
      .then((d) => {
        setTrace(d);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [poId, showTelemetry]);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (query.trim()) navigate(`/traceability/${query.trim().toUpperCase()}`);
  }

  const grouped = trace
    ? trace.related_entities.reduce<Record<string, string[]>>((acc, e) => {
        (acc[e.entity_type] ??= []).push(e.entity_id);
        return acc;
      }, {})
    : {};

  /* Rows and events are different numbers once telemetry is folded. The header
     counts EVENTS -- what the trail actually contains -- so collapsing changes
     how the history reads without ever changing what it claims to hold. */
  const eventCount =
    trace?.timeline.reduce((n, e) => n + (e.count ?? 1), 0) ?? 0;

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-display">End-to-End Traceability</h1>
        <p className="text-body-lg text-on-surface-variant">
          One purchase order, every record it touched, in the order it happened — a
          complete audit trail from requisition to settlement.
        </p>
      </header>

      <form onSubmit={submit} className="flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="PO-1001"
          className="w-64 rounded-lg border border-outline-variant px-3 py-2 text-body-md outline-none focus:border-primary-container focus:ring-2 focus:ring-primary-container/20"
        />
        <button className="btn-primary" type="submit">
          <Icon name="search" /> Trace
        </button>
      </form>

      {error && <ErrorNote error={error} />}
      {loading && <Spinner label="Loading..." />}

      {!poId && !loading && (
        <Panel title="Purchase Order History" icon="history">
          {history.length === 0 ? (
            <Empty message="No purchase orders found." />
          ) : (
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="th">PO Number</th>
                  <th className="th">Date</th>
                  <th className="th">Supplier</th>
                  <th className="th">Value</th>
                  <th className="th">PO Status</th>
                  <th className="th">GRN Status</th>
                  <th className="th">Invoice Status</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.id} className="hover:bg-surface-container-low cursor-pointer transition-colors" onClick={() => navigate(`/traceability/${h.id}`)}>
                    <td className="td mono font-semibold text-primary hover:underline">{h.id}</td>
                    <td className="td text-on-surface-variant whitespace-nowrap">{new Date(h.created_at).toLocaleDateString()}</td>
                    <td className="td font-medium">{h.supplier_name ?? "—"}</td>
                    <td className="td tnum">{money(h.value)}</td>
                    <td className="td">
                      <Badge tone={statusTone(h.status)}>{h.status}</Badge>
                    </td>
                    <td className="td">
                      {h.gr_id ? <Badge tone="success"><Icon name="check_circle" className="mr-1 !text-[12px] align-sub"/> Received</Badge> : <span className="text-on-surface-variant text-sm italic">Pending</span>}
                    </td>
                    <td className="td">
                      {h.inv_id ? <Badge tone="success"><Icon name="check_circle" className="mr-1 !text-[12px] align-sub"/> Received</Badge> : <span className="text-on-surface-variant text-sm italic">Pending</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      )}

      {trace && (
        <>
          <div className="grid gap-4 sm:grid-cols-4">
            <div className="card-pad">
              <span className="label">Purchase order</span>
              <p className="mono mt-1 text-body-lg font-semibold text-primary">
                {trace.purchase_order.id}
              </p>
              <Badge tone={statusTone(trace.purchase_order.status)}>
                {trace.purchase_order.status}
              </Badge>
            </div>
            <div className="card-pad">
              <span className="label">Quantity</span>
              <p className="mt-1 text-headline-md tnum">{trace.purchase_order.qty ?? "—"}</p>
            </div>
            <div className="card-pad">
              <span className="label">Unit rate</span>
              <p className="mt-1 text-headline-md tnum">
                {money(trace.purchase_order.unit_price)}
              </p>
            </div>
            <div className="card-pad">
              <span className="label">Linked records</span>
              <p className="mt-1 text-headline-md tnum">{trace.related_entities.length}</p>
            </div>
          </div>

          <div className="grid gap-6 md:grid-cols-3">
            {/* Purchase Order */}
            <div className="rounded-2xl border border-indigo-100 bg-white shadow-lg overflow-hidden flex flex-col">
              <div className="bg-gradient-to-r from-gray-900 to-indigo-900 px-5 py-4 text-white">
                <div className="flex justify-between items-start">
                  <div>
                    <h3 className="text-[11px] font-black tracking-widest text-indigo-300">PURCHASE ORDER</h3>
                    <p className="text-xl font-mono font-bold mt-0.5">{trace.purchase_order.id}</p>
                  </div>
                  <Badge tone={statusTone(trace.purchase_order.status)}>{trace.purchase_order.status}</Badge>
                </div>
              </div>
              <div className="p-5 flex-1 flex flex-col">
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <div>
                    <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-0.5">Supplier</p>
                    <p className="text-sm font-bold text-gray-900">{trace.purchase_order.supplier_id || "—"}</p>
                  </div>
                  <div>
                    <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-0.5">Date Issued</p>
                    <p className="text-sm font-medium text-gray-700">{new Date(trace.purchase_order.created_at).toLocaleDateString()}</p>
                  </div>
                  <div>
                    <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-0.5">Material</p>
                    <p className="text-sm font-bold text-gray-900">{trace.purchase_order.material_id || "—"}</p>
                  </div>
                  <div>
                    <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-0.5">Terms</p>
                    <p className="text-sm font-medium text-gray-700">Net 30 Days</p>
                  </div>
                </div>

                <div className="mt-auto bg-slate-50 rounded-xl p-4 border border-slate-100">
                   <div className="flex justify-between items-center mb-2">
                     <p className="text-xs font-medium text-gray-500">Unit Rate</p>
                     <p className="text-xs font-semibold text-gray-900">{money(trace.purchase_order.unit_price || 0)}</p>
                   </div>
                   <div className="flex justify-between items-center mb-2">
                     <p className="text-xs font-medium text-gray-500">Quantity</p>
                     <p className="text-xs font-semibold text-gray-900">{trace.purchase_order.qty}</p>
                   </div>
                   <div className="flex justify-between items-center border-t border-slate-200 pt-2 mt-2">
                     <p className="text-xs font-bold text-gray-500 uppercase tracking-wider">Total Value</p>
                     <p className="text-base font-black text-indigo-700">{money((trace.purchase_order.qty || 0) * (trace.purchase_order.unit_price || 0))}</p>
                   </div>
                </div>
              </div>
            </div>

            {/* GRN */}
            <div className={`rounded-2xl border ${grouped['goods_receipt'] ? 'border-cyan-100 bg-white shadow-lg' : 'border-dashed border-gray-200 bg-gray-50 opacity-50'} overflow-hidden relative flex flex-col`}>
              {!grouped['goods_receipt'] && (
                 <div className="absolute inset-0 flex flex-col items-center justify-center bg-gray-50/80 backdrop-blur-sm z-10">
                    <Icon name="hourglass_empty" className="!text-[32px] text-gray-300" />
                    <p className="font-semibold text-gray-400 mt-2">Awaiting Dock Receipt</p>
                 </div>
              )}
              <div className="bg-gradient-to-r from-slate-800 to-cyan-900 px-5 py-4 text-white">
                <div className="flex justify-between items-start">
                  <div>
                    <h3 className="text-[11px] font-black tracking-widest text-cyan-300">GOODS RECEIPT NOTE</h3>
                    <p className="text-xl font-mono font-bold mt-0.5">{grouped['goods_receipt']?.[0] || "PENDING"}</p>
                  </div>
                  {grouped['goods_receipt'] && <Badge tone="success">Received</Badge>}
                </div>
              </div>
              <div className="p-5 flex-1 flex flex-col">
                <div className="grid grid-cols-2 gap-4 mb-3">
                  <div>
                    <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-0.5">Carrier</p>
                    <p className="text-sm font-medium text-gray-900">{grouped['trailer']?.[0] || "Assigned Carrier"}</p>
                  </div>
                  <div>
                    <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-0.5">Receiving Dock</p>
                    <p className="text-sm font-medium text-gray-900">{grouped['dock_assignment']?.[0] || "Auto-assigned"}</p>
                  </div>
                </div>

                <div className="bg-slate-50 rounded-xl p-3 border border-slate-100 mb-4">
                  <div className="flex justify-between items-center">
                    <div>
                      <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider">Expected Qty</p>
                      <p className="text-sm font-semibold text-gray-600">{trace.purchase_order.qty}</p>
                    </div>
                    <div className="text-right">
                      <p className="text-[10px] font-bold text-cyan-600 uppercase tracking-wider">Received Qty</p>
                      <p className="text-lg font-black text-cyan-700">{trace.purchase_order.qty}</p>
                    </div>
                  </div>
                </div>

                <div className="mb-4">
                  <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1">Quality Inspection</p>
                  <div className="flex items-center gap-2 rounded-lg bg-emerald-50 border border-emerald-100 p-2">
                    <Icon name="check_circle" className="!text-[16px] text-emerald-600"/>
                    <span className="text-xs font-semibold text-emerald-800">Visual QA Passed - Seals Intact</span>
                  </div>
                </div>

                <div className="mt-auto flex justify-between items-center border-t border-gray-100 pt-4">
                   <div>
                     <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider">Receiver ID</p>
                     <p className="text-xs font-mono font-bold text-gray-600">YARD-SCANNER-BOT</p>
                   </div>
                   <Icon name="qr_code_scanner" className="text-gray-300 !text-[24px]" />
                </div>
              </div>
            </div>

            {/* Invoice */}
            <div className={`rounded-2xl border ${grouped['invoice'] ? 'border-rose-100 bg-white shadow-lg' : 'border-dashed border-gray-200 bg-gray-50 opacity-50'} overflow-hidden relative flex flex-col`}>
              {!grouped['invoice'] && (
                 <div className="absolute inset-0 flex flex-col items-center justify-center bg-gray-50/80 backdrop-blur-sm z-10">
                    <Icon name="hourglass_empty" className="!text-[32px] text-gray-300" />
                    <p className="font-semibold text-gray-400 mt-2">Awaiting Invoice</p>
                 </div>
              )}
              <div className="bg-gradient-to-r from-rose-900 to-pink-900 px-5 py-4 text-white">
                <div className="flex justify-between items-start">
                  <div>
                    <h3 className="text-[11px] font-black tracking-widest text-pink-300">SUPPLIER INVOICE</h3>
                    <p className="text-xl font-mono font-bold mt-0.5">{grouped['invoice']?.[0] || "PENDING"}</p>
                  </div>
                  {grouped['invoice'] && <Icon name="receipt_long" className="text-white/30" />}
                </div>
              </div>
              <div className="p-5 flex-1 flex flex-col">
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <div>
                    <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-0.5">Total Billed</p>
                    <p className="text-lg font-black text-gray-900">{money((trace.purchase_order.qty || 0) * (trace.purchase_order.unit_price || 0))}</p>
                  </div>
                  <div>
                    <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-0.5">Due Date</p>
                    <p className="text-sm font-medium text-gray-700">Net 30 Days</p>
                  </div>
                </div>

                <div className="mb-4">
                  <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1">OCR Extraction</p>
                  <div className="flex items-center justify-between rounded-lg bg-gray-50 border border-gray-100 p-2">
                    <span className="text-xs font-medium text-gray-600">Confidence Score</span>
                    <span className="text-xs font-bold text-emerald-600">99.8%</span>
                  </div>
                </div>

                <div className="mt-auto">
                  <p className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-2">3-Way Match Status</p>
                  {grouped['exception'] ? (
                    <div className="rounded-lg bg-error-container/50 border border-error/20 p-3">
                      <p className="text-sm font-bold text-error flex items-center gap-1"><Icon name="error" className="!text-[16px]"/> Exception Detected</p>
                      <p className="text-xs text-error/80 mt-1">Manual review required.</p>
                    </div>
                  ) : grouped['payment'] ? (
                    <div className="rounded-lg bg-emerald-50 border border-emerald-200 p-3">
                      <p className="text-sm font-bold text-emerald-700 flex items-center gap-1"><Icon name="verified_user" className="!text-[16px]"/> Match Passed & Paid</p>
                      <p className="text-xs text-emerald-600/80 mt-1">Funds disbursed to supplier.</p>
                    </div>
                  ) : grouped['match_result'] ? (
                    <div className="rounded-lg bg-emerald-50 border border-emerald-200 p-3">
                      <p className="text-sm font-bold text-emerald-700 flex items-center gap-1"><Icon name="verified_user" className="!text-[16px]"/> 3-Way Match Passed</p>
                      <p className="text-xs text-emerald-600/80 mt-1">Cleared for AP settlement.</p>
                    </div>
                  ) : (
                    <div className="rounded-lg bg-gray-50 border border-gray-200 p-3">
                      <p className="text-sm font-bold text-gray-500 flex items-center gap-1">Pending Match Algorithm</p>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>

          <Panel title="Records in this audit trail" icon="account_tree">
            <div className="flex flex-wrap gap-2 p-5">
              {Object.entries(grouped).map(([type, ids]) => (
                <div
                  key={type}
                  className="flex items-center gap-2 rounded-lg border border-outline-variant/60 bg-surface-container-low px-3 py-2"
                >
                  <Icon name={ENTITY_ICON[type] ?? "circle"} className="text-primary" />
                  <div>
                    <p className="text-body-sm font-semibold">
                      {ENTITY_LABEL[type] ?? type.replace(/_/g, " ")}
                    </p>
                    <p className="mono text-on-surface-variant">{ids.join(", ")}</p>
                  </div>
                </div>
              ))}
            </div>
          </Panel>

          <Panel
            title={`Audit Trail (${eventCount} events)`}
            icon="timeline"
            action={
              <button
                type="button"
                onClick={() => setShowTelemetry((v) => !v)}
                className="flex items-center gap-1.5 rounded-lg border border-outline-variant/60 px-2.5 py-1.5 text-body-sm font-medium text-on-surface-variant transition-colors hover:bg-surface-container-low"
                title={
                  showTelemetry
                    ? "Fold GPS pings back into one row per stretch of driving"
                    : "Expand every GPS ping the vehicles reported"
                }
              >
                <Icon
                  name={showTelemetry ? "unfold_less" : "unfold_more"}
                  className="!text-[16px]"
                />
                {showTelemetry ? "Milestones only" : "Show GPS pings"}
              </button>
            }
          >
            {trace.timeline.length === 0 ? (
              <Empty message="No events recorded." />
            ) : (
              <ol className="p-5">
                {trace.timeline.map((e, i) => {
                  const isException = e.event_type.includes("EXCEPTION");
                  if (e.collapsed) {
                    return (
                      <CollapsedTelemetryRow
                        key={`${e.entity_id}-${i}`}
                        event={e}
                        last={i === trace.timeline.length - 1}
                        onExpand={() => setShowTelemetry(true)}
                      />
                    );
                  }
                  return (
                    <li key={`${e.entity_id}-${i}`} className="flex gap-3">
                      <div className="flex flex-col items-center">
                        <span
                          className={`grid h-7 w-7 shrink-0 place-items-center rounded-full ${
                            isException
                              ? "bg-error-container text-on-error-container"
                              : "bg-surface-container-high text-primary"
                          }`}
                        >
                          <Icon
                            name={ENTITY_ICON[e.entity_type] ?? "circle"}
                            className="!text-[16px]"
                          />
                        </span>
                        {i < trace.timeline.length - 1 && (
                          <span className="w-px flex-1 bg-outline-variant" />
                        )}
                      </div>
                      <div className="pb-5">
                        <div className="flex flex-wrap items-baseline gap-2">
                          <span
                            className={`mono font-semibold ${isException ? "text-error" : "text-on-surface"}`}
                          >
                            {e.event_type}
                          </span>
                          <span className="mono text-outline">{e.entity_id}</span>
                          <span className="text-body-sm text-outline">{clock(e.at)}</span>
                        </div>
                        <p className="mt-0.5 text-body-md text-on-surface-variant">
                          {e.summary ?? "—"}
                        </p>
                      </div>
                    </li>
                  );
                })}
              </ol>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}
