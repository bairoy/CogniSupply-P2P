import { ReactNode, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { GATEWAY, PROCUREMENT, QueueItem, TimelineEvent, api } from "../api";
import { PERM, RequirePermission } from "../auth";
import {
  Badge,
  Empty,
  ErrorNote,
  Icon,
  Panel,
  ago,
  money,
  moneyCompact,
  severityTone,
} from "../components/ui";
import { LiveEvent, useRefetchOn } from "../hooks/useEventStream";

/**
 * The three documents a match exception is an argument between. Only the
 * fields the comparison renders -- the full shape is in Invoice Settlement.
 */
interface MatchDocuments {
  invoice: { id: string; qty_invoiced: number; unit_price_invoiced: number; total: number };
  purchase_order: { id: string; qty: number; unit_price: number; supplier_name: string;
    expected_total: number } | null;
  goods_receipt: { id: string; qty_received: number } | null;
  match_result: { id: string; status: string; reason: string } | null;
  variance: number | null;
}

/* 3WAY_MATCH_POLICY.md §"Tolerance evaluation". Mirrored here so the screen
   highlights exactly what the worker rejected on -- not a second opinion. */
const QTY_TOLERANCE = 0.02;
const PRICE_TOLERANCE = 0.03;

function variancePct(actual: number, baseline: number) {
  if (!baseline) return null;
  return Math.abs(actual - baseline) / baseline;
}

/**
 * Exception Management Queue.
 *
 * The queue unions two sources: `exceptions` (match failures) and `alerts`
 * (dock conflicts, delays). They live in different tables because they are
 * genuinely different things -- the gateway joins them for presentation only.
 * Alerts are acknowledgeable; only exceptions are resolvable, which is why
 * `resolvable` comes down per row rather than being inferred in the UI.
 */
export default function Exceptions({ events }: { events: LiveEvent[] }) {
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [counts, setCounts] = useState({ total: 0, critical: 0 });
  const [selected, setSelected] = useState<QueueItem | null>(null);
  const [chain, setChain] = useState<TimelineEvent[]>([]);
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [docs, setDocs] = useState<MatchDocuments | null>(null);
  const [docsError, setDocsError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .gateway<{ queue: QueueItem[]; counts: { total: number; critical: number } }>(
        "/exceptions/queue?status=OPEN",
      )
      .then((d) => {
        setQueue(d.queue);
        setCounts(d.counts);
        setError(null);
        setSelected((prev) => (prev ? d.queue.find((q) => q.id === prev.id) ?? null : null));
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(load, [load]);
  useRefetchOn(
    events,
    ["EXCEPTION_CREATED", "EXCEPTION_RESOLVED", "EXCEPTION_ASSIGNED", "ALERT_CREATED",
     "ALERT_ACKNOWLEDGED"],
    load,
  );

  /*
   * The three documents behind a match exception.
   *
   * The queue row only carries the PO id, because that is what the operator
   * needs to recognise the row. The comparison needs the invoice, so this
   * resolves exception -> invoice_id through the procurement service's own
   * exception list, then reads the invoice, which already returns the PO and
   * the goods receipt alongside it. Two reads, no new endpoint, and the
   * numbers come from the same query Invoice Settlement renders -- so the two
   * screens can never quote different figures for the same dispute.
   */
  useEffect(() => {
    setDocs(null);
    setDocsError(null);
    if (!selected || selected.source !== "exception") return;

    let cancelled = false;
    const exceptionId = selected.id;
    (async () => {
      try {
        const list = await api.procurement<{
          exceptions: { id: string; invoice_id: string | null }[];
        }>("/exceptions?status=ALL&limit=200");
        const invoiceId = list.exceptions.find((e) => e.id === exceptionId)?.invoice_id;
        if (cancelled) return;
        if (!invoiceId) {
          setDocsError("This exception has no invoice attached to compare against.");
          return;
        }
        const detail = await api.procurement<MatchDocuments>(`/invoices/${invoiceId}`);
        if (!cancelled) setDocs(detail);
      } catch (e) {
        if (!cancelled) {
          setDocsError(e instanceof Error ? e.message : "Could not load the source documents");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected]);

  /* Root-cause chain: the full cross-entity story behind the selected row. */
  useEffect(() => {
    const po = selected?.entity_id;
    if (!po || !po.startsWith("PO-")) {
      setChain([]);
      return;
    }
    api
      .gateway<{ timeline: TimelineEvent[] }>(`/traceability/${po}`)
      .then((d) => setChain(d.timeline))
      .catch(() => setChain([]));
  }, [selected]);

  async function resolve(resolution: "APPROVE" | "REJECT") {
    if (!selected) return;
    setBusy(true);
    try {
      await api.post(PROCUREMENT, `/exceptions/${selected.id}/resolve`, {
        resolution,
        // No resolved_by: since v5 the server takes the resolver from the
        // bearer token, so a hardcoded user id here would be both a lie and
        // ignored.
        notes: notes || `${resolution} via Exception Management Queue`,
      });
      setNotes("");
      setSelected(null);
      load();
    } catch (e) {
      alert(e instanceof Error ? e.message : "Resolution failed");
    } finally {
      setBusy(false);
    }
  }

  async function acknowledge() {
    if (!selected) return;
    setBusy(true);
    try {
      await api.post(GATEWAY, `/alerts/${selected.id}/acknowledge`);
      setSelected(null);
      load();
    } catch (e) {
      alert(e instanceof Error ? e.message : "Acknowledgement failed");
    } finally {
      setBusy(false);
    }
  }

  if (error) return <ErrorNote error={error} />;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-display">Exception Management Queue</h1>
          <p className="text-body-lg text-on-surface-variant">
            Triage and resolution for 3-way match exceptions and yard alerts the
            autonomous path could not settle.
          </p>
        </div>
        <div className="flex gap-2">
          <Badge tone="danger">{counts.critical} critical</Badge>
          <Badge tone="neutral">{counts.total} in queue</Badge>
        </div>
      </header>

      <div className="grid gap-6 xl:grid-cols-[1.5fr_1fr]">
        <Panel title="Open Exceptions & Alerts" icon="filter_list">
          {queue.length === 0 ? (
            <Empty
              message="Queue is clear."
              hint="Every transaction settled without manual intervention."
            />
          ) : (
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="th">Severity</th>
                  <th className="th">Reference</th>
                  <th className="th">Exception type</th>
                  <th className="th">Age</th>
                  <th className="th">Financial impact</th>
                  <th className="th">Owner</th>
                </tr>
              </thead>
              <tbody>
                {queue.map((q) => (
                  <tr
                    key={`${q.source}-${q.id}`}
                    onClick={() => setSelected(q)}
                    className={`cursor-pointer hover:bg-surface-container-low ${
                      selected?.id === q.id ? "bg-surface-container-low" : ""
                    }`}
                  >
                    <td className="td">
                      <Badge tone={severityTone(q.severity)}>{q.severity}</Badge>
                    </td>
                    <td className="td mono font-semibold text-primary">{q.entity_id ?? q.id}</td>
                    <td className="td">
                      {q.type.replace(/_/g, " ")}
                      <div className="text-body-sm text-outline">{q.source}</div>
                    </td>
                    <td className="td text-on-surface-variant">{ago(q.created_at)}</td>
                    <td className="td tnum">
                      {q.impact_amount ? moneyCompact(q.impact_amount) : "—"}
                    </td>
                    <td className="td text-on-surface-variant">{q.owner}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <div className="flex flex-col gap-6">
          <Panel title="Root Cause Analysis" icon="timeline">
            {!selected ? (
              <Empty
                message="Select an exception"
                hint="Its full cross-entity history appears here."
              />
            ) : chain.length === 0 ? (
              <div className="p-5">
                <p className="text-body-md">{selected.detail ?? "No further detail."}</p>
                <p className="mt-2 text-body-sm text-on-surface-variant">
                  {selected.source === "alert"
                    ? "Yard alerts are operational signals and carry no purchase-order chain."
                    : "No audit trail found for this record."}
                </p>
              </div>
            ) : (
              <ol className="flex flex-col gap-0 p-5">
                {chain.slice(-8).map((e, i) => (
                  <li key={`${e.entity_id}-${i}`} className="flex gap-3">
                    <div className="flex flex-col items-center">
                      <span
                        className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${
                          e.event_type.includes("EXCEPTION") ? "bg-error" : "bg-primary-container"
                        }`}
                      />
                      {i < Math.min(chain.length, 8) - 1 && (
                        <span className="w-px flex-1 bg-outline-variant" />
                      )}
                    </div>
                    <div className="pb-4">
                      <p className="mono font-semibold text-on-surface">{e.event_type}</p>
                      <p className="text-body-sm text-on-surface-variant">{e.summary}</p>
                      <p className="text-body-sm text-outline">{ago(e.at)}</p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </Panel>

          <Panel title="Resolution & Approval" icon="gavel">
            {!selected ? (
              <Empty message="Nothing selected." />
            ) : (
              <div className="flex flex-col gap-3 p-5">
                <div className="rounded-lg bg-surface-container-low p-3">
                  <span className="label">Automated finding</span>
                  <p className="mt-1 text-body-md">{selected.detail ?? "—"}</p>
                </div>

                {selected.source === "exception" && (
                  <ThreeWayCompare docs={docs} error={docsError} />
                )}

                {selected.entity_id?.startsWith("PO-") && (
                  <Link
                    to={`/traceability/${selected.entity_id}`}
                    className="text-body-sm font-semibold text-primary hover:underline"
                  >
                    Open the full audit trail for {selected.entity_id} →
                  </Link>
                )}

                {selected.resolvable ? (
                  <RequirePermission
                    permission={PERM.exceptionResolve}
                    action="resolve exceptions (Finance and Administrators can)"
                  >
                    <textarea
                      value={notes}
                      onChange={(e) => setNotes(e.target.value)}
                      rows={3}
                      placeholder="Enter approval justification (recorded against the exception)…"
                      className="w-full resize-none rounded-lg border border-outline-variant p-3 text-body-md outline-none focus:border-primary-container focus:ring-2 focus:ring-primary-container/20"
                    />
                    <div className="mt-2 flex gap-2">
                      <button
                        className="btn-primary flex-1"
                        disabled={busy}
                        onClick={() => resolve("APPROVE")}
                      >
                        <Icon name="check" /> Approve &amp; release payment
                      </button>
                      <button
                        className="btn-secondary flex-1"
                        disabled={busy}
                        onClick={() => resolve("REJECT")}
                      >
                        <Icon name="block" /> Reject
                      </button>
                    </div>
                    <p className="mt-2 text-body-sm text-on-surface-variant">
                      Approval overrides a deterministic refusal, so both the decision and its
                      author are recorded for audit — the author being whoever is signed in.
                    </p>
                  </RequirePermission>
                ) : (
                  <RequirePermission
                    permission={PERM.alertAck}
                    action="acknowledge alerts"
                  >
                    <button className="btn-secondary" disabled={busy} onClick={acknowledge}>
                      <Icon name="done_all" /> Acknowledge alert
                    </button>
                  </RequirePermission>
                )}
              </div>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────
   PO vs GRN vs Invoice
   ───────────────────────────────────────────── */

/** A figure that may be the one under dispute. */
function Figure({ value, bad, title }: { value: string; bad?: boolean; title?: string }) {
  return (
    <span
      title={title}
      className={`tnum ${bad ? "text-[15px] font-bold text-error" : "font-medium text-on-surface"}`}
    >
      {value}
      {bad && <Icon name="priority_high" className="!text-[14px] align-middle text-error" />}
    </span>
  );
}

/**
 * The dispute, laid out as the three documents that disagree.
 *
 * The reason string already says what failed; this shows it. Quantity is
 * compared against the RECEIPT and price against the PO, exactly as the match
 * worker does (3WAY_MATCH_POLICY.md) -- comparing invoice quantity to ordered
 * quantity here would light up a different cell than the one the engine
 * actually rejected on, and an approver would be reconciling the wrong number.
 */
function ThreeWayCompare({ docs, error }: { docs: MatchDocuments | null; error: string | null }) {
  if (error) {
    return (
      <div className="rounded-lg border border-outline-variant/60 bg-surface-container-low p-3">
        <span className="label">Document comparison</span>
        <p className="mt-1 text-body-sm text-on-surface-variant">{error}</p>
      </div>
    );
  }
  if (!docs) {
    return (
      <div className="rounded-lg border border-outline-variant/60 p-3">
        <span className="label">Document comparison</span>
        <p className="mt-1 text-body-sm text-on-surface-variant">Loading source documents…</p>
      </div>
    );
  }

  const { invoice, purchase_order: po, goods_receipt: gr } = docs;

  const qtyVariance = gr ? variancePct(invoice.qty_invoiced, gr.qty_received) : null;
  const priceVariance = po ? variancePct(invoice.unit_price_invoiced, po.unit_price) : null;
  const qtyBad = qtyVariance !== null && qtyVariance > QTY_TOLERANCE;
  const priceBad = priceVariance !== null && priceVariance > PRICE_TOLERANCE;
  const totalBad = qtyBad || priceBad;
  // Short/over delivery against the order. Not what the engine rejects on --
  // it is context for the approver, so it is amber, not red.
  const shortDelivery = po && gr && po.qty !== gr.qty_received;

  const columns: {
    key: string;
    title: string;
    icon: string;
    reference: string;
    accent: string;
    rows: { label: string; node: ReactNode }[];
  }[] = [
    {
      key: "po",
      title: "Purchase Order",
      icon: "shopping_cart",
      reference: po?.id ?? "—",
      accent: "border-outline-variant/60",
      rows: [
        { label: "Qty ordered", node: <Figure value={po ? String(po.qty) : "—"} /> },
        { label: "Unit price", node: <Figure value={po ? money(po.unit_price) : "—"} /> },
        { label: "Committed", node: <Figure value={po ? money(po.expected_total) : "—"} /> },
      ],
    },
    {
      key: "gr",
      title: "Goods Receipt",
      icon: "inventory_2",
      reference: gr?.id ?? "not received",
      accent: "border-outline-variant/60",
      rows: [
        {
          label: "Qty received",
          node: (
            <span
              className={`tnum font-medium ${shortDelivery ? "text-warning" : "text-on-surface"}`}
              title={shortDelivery ? "Differs from the quantity ordered" : undefined}
            >
              {gr ? gr.qty_received : "—"}
            </span>
          ),
        },
        { label: "Unit price", node: <span className="text-outline">not carried</span> },
        { label: "Value", node: <span className="text-outline">not carried</span> },
      ],
    },
    {
      key: "inv",
      title: "Invoice",
      icon: "receipt_long",
      reference: invoice.id,
      accent: totalBad ? "border-error/50 bg-error-container/25" : "border-outline-variant/60",
      rows: [
        {
          label: "Qty invoiced",
          node: (
            <Figure
              value={String(invoice.qty_invoiced)}
              bad={qtyBad}
              title={qtyBad ? `${(qtyVariance! * 100).toFixed(1)}% above the receipt` : undefined}
            />
          ),
        },
        {
          label: "Unit price",
          node: (
            <Figure
              value={money(invoice.unit_price_invoiced)}
              bad={priceBad}
              title={priceBad ? `${(priceVariance! * 100).toFixed(1)}% off the PO rate` : undefined}
            />
          ),
        },
        { label: "Billed", node: <Figure value={money(invoice.total)} bad={totalBad} /> },
      ],
    },
  ];

  return (
    <div className="rounded-lg border border-outline-variant/60 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="label">Purchase order vs goods receipt vs invoice</span>
        {docs.match_result && (
          <Badge tone={docs.match_result.status === "EXCEPTION" ? "danger" : "success"}>
            {docs.match_result.status}
          </Badge>
        )}
      </div>

      <div className="mt-2.5 grid grid-cols-3 gap-2">
        {columns.map((c) => (
          <div key={c.key} className={`rounded-lg border p-2.5 ${c.accent}`}>
            <p className="flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-on-surface-variant">
              <Icon name={c.icon} className="!text-[14px]" />
              {c.title}
            </p>
            <p className="mono mt-0.5 truncate text-primary" title={c.reference}>
              {c.reference}
            </p>
            <dl className="mt-2 flex flex-col gap-1.5">
              {c.rows.map((r) => (
                <div key={r.label}>
                  <dt className="text-[11px] uppercase text-outline">{r.label}</dt>
                  <dd className="text-body-sm">{r.node}</dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      </div>

      {/* the specific arithmetic that failed */}
      <div className="mt-2.5 flex flex-col gap-1.5">
        {qtyBad && (
          <p className="rounded bg-error-container/60 px-2.5 py-1.5 text-body-sm text-on-error-container">
            <strong>Quantity variance {(qtyVariance! * 100).toFixed(1)}%</strong> — received{" "}
            {gr!.qty_received}, invoiced {invoice.qty_invoiced}. Exceeds the{" "}
            {(QTY_TOLERANCE * 100).toFixed(0)}% tolerance.
          </p>
        )}
        {priceBad && (
          <p className="rounded bg-error-container/60 px-2.5 py-1.5 text-body-sm text-on-error-container">
            <strong>Price variance {(priceVariance! * 100).toFixed(1)}%</strong> — PO rate{" "}
            {money(po!.unit_price)}, invoiced {money(invoice.unit_price_invoiced)}. Exceeds the{" "}
            {(PRICE_TOLERANCE * 100).toFixed(0)}% tolerance.
          </p>
        )}
        {shortDelivery && !qtyBad && (
          <p className="rounded bg-warning-container/60 px-2.5 py-1.5 text-body-sm text-on-surface">
            Delivery differs from the order — {po!.qty} ordered, {gr!.qty_received} received. The
            invoice is billed against the receipt, so this alone is not a match failure.
          </p>
        )}
        {docs.variance !== null && (
          <p className="flex justify-between rounded bg-surface-container-low px-2.5 py-1.5 text-body-sm">
            <span>Net financial exposure</span>
            <span className={`tnum font-semibold ${docs.variance > 0 ? "text-error" : "text-success"}`}>
              {docs.variance > 0 ? "+" : ""}
              {money(docs.variance)}
            </span>
          </p>
        )}
        {!qtyBad && !priceBad && (
          <p className="text-body-sm text-on-surface-variant">
            {docs.match_result?.reason ??
              "No tolerance breach on quantity or price — the refusal came from a hard rule (missing PO or duplicate invoice)."}
          </p>
        )}
      </div>
    </div>
  );
}
