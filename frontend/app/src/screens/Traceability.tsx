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

export default function Traceability() {
  const { poId } = useParams();
  const navigate = useNavigate();
  const [query, setQuery] = useState(poId ?? "");
  const [trace, setTrace] = useState<Trace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!poId) {
      setTrace(null);
      return;
    }
    setLoading(true);
    api
      .gateway<Trace>(`/traceability/${poId}`)
      .then((d) => {
        setTrace(d);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [poId]);

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
      {loading && <Spinner label="Assembling audit trail" />}

      {!poId && !loading && (
        <Empty message="Enter a purchase order number" hint="e.g. PO-1001" />
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

          <Panel title={`Audit Trail (${trace.timeline.length} events)`} icon="timeline">
            {trace.timeline.length === 0 ? (
              <Empty message="No events recorded." />
            ) : (
              <ol className="p-5">
                {trace.timeline.map((e, i) => {
                  const isException = e.event_type.includes("EXCEPTION");
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
