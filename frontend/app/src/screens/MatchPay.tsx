import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { PROCUREMENT, api } from "../api";

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

interface InvoiceDetail {
  invoice: {
    id: string; po_id: string | null; qty_invoiced: number; unit_price_invoiced: number;
    tax: number; total: number; ocr_confidence: number | null;
    ocr_raw: Record<string, unknown> | null; has_document: boolean; received_at: string;
  };
  purchase_order: { id: string; qty: number; unit_price: number; supplier_name: string;
    expected_total: number } | null;
  goods_receipt: { id: string; qty_received: number } | null;
  /**
   * `reason` is the deterministic policy output and the authoritative record.
   * `ai_narration` is the same verdict written up as prose (v8) and is null for
   * anything matched before v8, or when no provider key was configured. Render
   * `reason` unconditionally; the narration is an addition to it, never a
   * replacement — see docs/3WAY_MATCH_POLICY.md.
   */
  match_result: { id: string; status: string; reason: string;
    ai_narration: string | null } | null;
  exception: { id: string; exception_type: string; status: string; severity: string;
    impact_amount: number | null } | null;
  payment: { id: string; status: string; amount: number } | null;
  variance: number | null;
}

interface PaymentRow {
  id: string; invoice_id: string; amount: number; status: string;
  approved_at: string | null; paid_at: string | null; po_id: string | null;
  supplier_name: string | null; gr_id: string | null;
}

export default function MatchPay() {
  const { invoiceId } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<InvoiceDetail | null>(null);
  const [payments, setPayments] = useState<PaymentRow[]>([]);
  const [error, setError] = useState<string | null>(null);


  const loadPayments = useCallback(() => {
    api
      .procurement<{ payments: PaymentRow[] }>("/payments")
      .then((d) => setPayments(d.payments))
      .catch((e) => setError(e.message));
  }, []);

  useEffect(loadPayments, [loadPayments]);

  useEffect(() => {
    if (!invoiceId) {
      setDetail(null);
      return;
    }
    api
      .procurement<InvoiceDetail>(`/invoices/${invoiceId}`)
      .then((d) => {
        setDetail(d);
        setError(null);
      })
      .catch((e) => setError(e.message));
  }, [invoiceId]);

  if (error && !detail) return <ErrorNote error={error} />;

  /* ---------- list view ---------- */
  if (!invoiceId) {
    return (
      <div className="flex flex-col gap-6">
        <header>
          <h1 className="text-display">Invoice Matching &amp; Settlement</h1>
          <p className="text-body-lg text-on-surface-variant">
            Every supplier invoice, its 3-way match verdict, and its settlement status.
          </p>
        </header>
        <Panel title="Payment Register" icon="payments">
          {payments.length === 0 ? (
            <Empty message="No settlements raised yet." />
          ) : (
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="th">Payment</th>
                  <th className="th">Invoice</th>
                  <th className="th">Purchase order</th>
                  <th className="th">Goods receipt</th>
                  <th className="th">Supplier</th>
                  <th className="th">Amount</th>
                  <th className="th">Status</th>
                  <th className="th">Match verdict</th>
                  <th className="th">Action</th>
                </tr>
              </thead>
              <tbody>
                {payments.map((p) => (
                  <tr key={p.id} className="hover:bg-surface-container-low">
                    <td className="td mono">{p.id}</td>
                    <td className="td">
                      <button
                        className="mono font-semibold text-primary hover:underline"
                        onClick={() => navigate(`/match-pay/${p.invoice_id}`)}
                      >
                        {p.invoice_id}
                      </button>
                    </td>
                    <td className="td mono text-on-surface-variant">{p.po_id ?? "—"}</td>
                    <td className="td mono text-on-surface-variant">{p.gr_id ?? "—"}</td>
                    <td className="td text-on-surface-variant">{p.supplier_name ?? "—"}</td>
                    <td className="td tnum font-semibold">{money(p.amount)}</td>
                    <td className="td">
                      <Badge tone={statusTone(p.status)}>{p.status}</Badge>
                    </td>
                    <td className="td">
                      {(p.status === "APPROVED" || p.status === "PAID") ? (
                        <Badge tone="success"><Icon name="verified" className="!text-[14px] mr-1 align-sub"/>3-Way Verified</Badge>
                      ) : (
                        <span className="text-body-sm text-outline">—</span>
                      )}
                    </td>
                    <td className="td">
                      {p.status === "APPROVED" ? (
                        <span className="text-body-sm italic text-on-surface-variant">Auto-scheduled for release</span>
                      ) : p.status === "PAID" ? (
                        <span className="text-body-sm italic text-success">Released automatically</span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    );
  }

  /* ---------- detail view ---------- */
  if (!detail) return <Spinner label="Loading invoice" />;

  const { invoice, purchase_order: po, goods_receipt: gr, match_result: mr,
          exception: exc, payment } = detail;
  const failed = mr?.status === "EXCEPTION";
  const fieldConfidence =
    (invoice.ocr_raw?.field_confidence as Record<string, number> | undefined) ?? {};

  const invSubtotal = invoice.qty_invoiced * invoice.unit_price_invoiced;
  const poSubtotal = po?.expected_total ?? 0;
  const subtotalVariance = po ? invSubtotal - poSubtotal : 0;

  const steps = [
    {
      label: "GRN posted",
      done: !!gr,
      detail: gr ? `${gr.qty_received} units received` : "pending",
    },
    { label: "Invoice received", done: true, detail: clock(invoice.received_at) },
    {
      label: failed ? "3-way match failed" : "3-way match cleared",
      done: !!mr,
      detail: mr?.status ?? "pending",
      error: failed,
    },
    {
      label: payment?.status === "PAID" ? "Payment settled" : "Payment scheduled",
      done: !!payment,
      detail: payment?.status ?? "blocked",
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Badge tone={failed ? "danger" : "success"}>
              {failed ? "3-WAY MATCH FAILED — variance detected" : "3-WAY MATCH CLEARED"}
            </Badge>
            <Link to="/match-pay" className="text-body-sm text-primary hover:underline">
              ← Payment register
            </Link>
          </div>
          <h1 className="mt-1 text-display">Invoice {invoice.id}</h1>
          <p className="text-body-lg text-on-surface-variant">
            Supplier: {po?.supplier_name ?? "unknown"}
          </p>
        </div>
        {invoice.has_document && (
          <a
            className="btn-secondary"
            href={`${PROCUREMENT}/invoices/${invoice.id}/document`}
            target="_blank"
            rel="noreferrer"
          >
            <Icon name="visibility" /> View source document
          </a>
        )}
      </header>

      <div className="grid gap-6 lg:grid-cols-[380px_1fr]">
        <div className="flex flex-col gap-6">
          <Panel title={failed ? "Variance Analysis" : "Match Summary"} icon="rule">
            <div className="p-5">
              <p className="text-body-md text-on-surface-variant">
                {mr?.reason ?? "Awaiting 3-way match."}
              </p>
              {po && (
                <div
                  className={`mt-3 rounded-lg p-3 ${failed ? "bg-error-container/50" : "bg-success-container/50"}`}
                >
                  <div className="flex justify-between text-body-md">
                    <span>PO committed value</span>
                    <span className="tnum font-semibold">{money(po.expected_total)}</span>
                  </div>
                  <div className="flex justify-between text-body-md">
                    <span>Invoice subtotal (ex. tax)</span>
                    <span className="tnum font-semibold">{money(invSubtotal)}</span>
                  </div>
                  <div
                    className={`mt-1 flex justify-between border-t pt-1 text-body-md font-semibold ${
                      failed ? "border-error/30 text-on-error-container" : "border-success/30 text-success"
                    }`}
                  >
                    <span>Variance</span>
                    <span className="tnum">
                      {subtotalVariance >= 0 ? "+" : ""}
                      {money(subtotalVariance)}
                    </span>
                  </div>
                </div>
              )}
              {/* The prose rendering of the verdict above. Deliberately placed
                  AFTER the reason line and the variance arithmetic: the record
                  is what the policy computed, and the narration explains it.
                  Reversing that order would make the generated text look like
                  the finding. Absent entirely for pre-v8 matches, which is why
                  nothing above depends on it. */}
              {mr?.ai_narration && (
                <div className="mt-3 rounded-xl border border-indigo-100 bg-indigo-50/50 p-4">
                  <p className="flex items-center gap-2 text-sm font-bold text-indigo-800">
                    <Icon name="neurology" className="!text-[20px]" /> AI Audit Note
                  </p>
                  <p className="mt-2 text-body-md leading-relaxed text-indigo-900/80">
                    {mr.ai_narration}
                  </p>
                  <p className="mt-2 text-body-sm text-indigo-900/50">
                    Written from the completed match. The tolerance policy made the
                    decision; this only explains it.
                  </p>
                </div>
              )}
              {exc && (
                <div className="mt-3 flex items-center gap-2">
                  <Badge tone="danger">{exc.exception_type.replace(/_/g, " ")}</Badge>
                  <Link to="/exceptions" className="text-body-sm text-primary hover:underline">
                    Resolve in the exception queue →
                  </Link>
                </div>
              )}
            </div>
          </Panel>

          <Panel title="Document Intelligence (OCR)" icon="document_scanner">
            <div className="p-5">
              <div className="flex items-center justify-between">
                <span className="text-body-md">Overall extraction confidence</span>
                <span className="tnum font-semibold text-primary">
                  {invoice.ocr_confidence !== null
                    ? `${(invoice.ocr_confidence * 100).toFixed(1)}%`
                    : "—"}
                </span>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-container-high">
                <div
                  className="h-full rounded-full bg-primary-container"
                  style={{ width: `${(invoice.ocr_confidence ?? 0) * 100}%` }}
                />
              </div>
              {Object.keys(fieldConfidence).length > 0 ? (
                <div className="mt-4 grid grid-cols-2 gap-2">
                  {Object.entries(fieldConfidence).map(([field, conf]) => (
                    <div key={field} className="rounded-lg bg-surface-container-low p-2">
                      <span className="label">{field.replace(/_/g, " ")}</span>
                      <p className="mt-0.5 flex items-center gap-1 text-body-md font-semibold">
                        <Icon
                          name={conf > 0.9 ? "check_circle" : "warning"}
                          className={`!text-[16px] ${conf > 0.9 ? "text-success" : "text-warning"}`}
                        />
                        {(conf * 100).toFixed(0)}%
                      </p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="mt-3 text-body-sm text-on-surface-variant">
                  Received as a structured data feed — no per-field OCR confidence.
                </p>
              )}
            </div>
          </Panel>

          <Panel title="Settlement Progress" icon="schedule">
            <ol className="flex flex-col gap-0 p-5">
              {steps.map((s, i) => (
                <li key={s.label} className="flex gap-3">
                  <div className="flex flex-col items-center">
                    <Icon
                      name={s.error ? "cancel" : s.done ? "check_circle" : "radio_button_unchecked"}
                      className={
                        s.error ? "text-error" : s.done ? "text-primary-container" : "text-outline"
                      }
                    />
                    {i < steps.length - 1 && <span className="w-px flex-1 bg-outline-variant" />}
                  </div>
                  <div className="pb-4">
                    <p className={`text-body-md font-semibold ${s.error ? "text-error" : ""}`}>
                      {s.label}
                    </p>
                    <p className="text-body-sm text-on-surface-variant">{s.detail}</p>
                  </div>
                </li>
              ))}
            </ol>
            {payment?.status === "APPROVED" && (
              <div className="px-5 pb-5">
                <div className="rounded-lg bg-surface-container-low p-4 text-center border border-success/30">
                  <Icon name="verified" className="!text-[24px] text-success mb-2" />
                  <p className="text-body-lg font-semibold text-success">3-Way Match Verified</p>
                  <p className="text-body-sm text-on-surface-variant">Payment of {money(payment.amount)} is scheduled for autonomous release.</p>
                </div>
              </div>
            )}
          </Panel>
        </div>

        <Panel title="3-Way Match Reconciliation" icon="table_rows">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className="th">Source</th>
                <th className="th">Reference</th>
                <th className="th">Quantity</th>
                <th className="th">Unit price</th>
                <th className="th">Subtotal (ex. tax)</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td className="td font-semibold">Purchase order</td>
                <td className="td mono">{po?.id ?? "—"}</td>
                <td className="td tnum">{po?.qty ?? "—"}</td>
                <td className="td tnum">{po ? money(po.unit_price) : "—"}</td>
                <td className="td tnum">{po ? money(po.expected_total) : "—"}</td>
              </tr>
              <tr>
                <td className="td font-semibold">Goods Receipt Note (GRN)</td>
                <td className="td mono">{gr?.id ?? "—"}</td>
                <td className="td tnum">{gr?.qty_received ?? "—"}</td>
                <td className="td text-outline">n/a</td>
                <td className="td text-outline">n/a</td>
              </tr>
              <tr className={failed ? "bg-error-container/30" : ""}>
                <td className="td font-semibold">Invoice</td>
                <td className="td mono">{invoice.id}</td>
                <td className="td tnum">{invoice.qty_invoiced}</td>
                <td className="td tnum">{money(invoice.unit_price_invoiced)}</td>
                <td className="td tnum font-semibold">{money(invSubtotal)}</td>
              </tr>
              <tr className="bg-surface-container-low border-t border-outline-variant/60">
                <td className="td font-semibold text-right pr-4" colSpan={4}>Invoice Tax (GST)</td>
                <td className="td tnum text-on-surface-variant">{money(invoice.tax)}</td>
              </tr>
              <tr className="bg-surface-container border-t-2 border-outline-variant">
                <td className="td font-semibold text-right pr-4" colSpan={4}>Invoice Grand Total</td>
                <td className="td tnum font-semibold text-primary">{money(invoice.total)}</td>
              </tr>
            </tbody>
          </table>
          <div className="border-t border-outline-variant/60 p-5">
            <p className="text-body-sm text-on-surface-variant">
              Quantity is compared against what was <strong>received</strong>, not ordered — the
              PO authorises, the receipt confirms physical reality, the invoice bills against that
              reality. Price is compared against the PO, since receipts carry no price.
              Tolerances: 2% quantity, 3% price.
            </p>
          </div>
        </Panel>
      </div>
    </div>
  );
}
