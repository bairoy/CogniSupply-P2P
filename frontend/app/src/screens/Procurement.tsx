import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { PROCUREMENT, api } from "../api";
import { PERM, RequirePermission } from "../auth";
import { Badge, Icon, Panel, money } from "../components/ui";

/**
 * Autonomous Procure-to-Pay -- requisition intake and supplier award.
 *
 * Two AI touchpoints, both extraction/explanation rather than decision:
 *   - the chat parses free text into a structured requisition,
 *   - the supplier cards show a deterministic score with an LLM-written
 *     rationale beside it.
 * The `ai_available` flag is surfaced honestly so a fallback run is visible
 * rather than passed off as the real thing.
 */

interface Parsed {
  material_id: string | null;
  material_name: string;
  qty: number;
  uom: string;
  required_date: string | null;
  delivery_location_id: string | null;
  confidence: number;
  ambiguities: string[];
  ai_available?: boolean;
}

interface Recommendation {
  supplier_id: string;
  supplier_name: string;
  price_score: number;
  quality_score: number;
  lead_time_score: number;
  reliability_score: number;
  risk_score: number;
  overall_score: number;
  quoted_unit_price: number;
  quoted_lead_time_days: number;
  rank: number;
  recommended: boolean;
  reasoning: string;
}

type ChatResponse =
  | { status: "clarifying"; questions: string[]; draft: Parsed; ai_available: boolean;
      history: { role: string; content: string }[] }
  | { status: "parsed"; id: string; parsed: Parsed; ai_available: boolean };

const EXAMPLE = "We need 500 meters of industrial aluminium tubing delivered to the Bhiwandi plant by next Friday";

export default function Procurement() {
  const [text, setText] = useState(EXAMPLE);
  const [history, setHistory] = useState<{ role: string; content: string }[]>([]);
  const [questions, setQuestions] = useState<string[]>([]);
  const [parsed, setParsed] = useState<Parsed | null>(null);
  const [reqId, setReqId] = useState<string | null>(null);
  const [poId, setPoId] = useState<string | null>(null);
  const [recs, setRecs] = useState<Recommendation[]>([]);
  const [aiAvailable, setAiAvailable] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function parse(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<ChatResponse>(PROCUREMENT, "/requisitions/chat", {
        message: text,
        history,
      });
      setAiAvailable(res.ai_available);
      if (res.status === "clarifying") {
        setQuestions(res.questions);
        setParsed(res.draft);
        setHistory(res.history);
        setReqId(null);
      } else {
        setQuestions([]);
        setParsed(res.parsed);
        setReqId(res.id);
        setHistory([]);
        setPoId(null);
        setRecs([]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not interpret the requirement");
    } finally {
      setBusy(false);
    }
  }

  async function selectSupplier() {
    if (!reqId) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<{ purchase_order_id: string; recommendations: Recommendation[];
        ai_available: boolean }>(PROCUREMENT, `/requisitions/${reqId}/select-supplier`);
      setPoId(res.purchase_order_id);
      setRecs(res.recommendations);
      setAiAvailable(res.ai_available);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Supplier evaluation failed");
    } finally {
      setBusy(false);
    }
  }

  const winner = recs.find((r) => r.recommended);
  const others = recs.filter((r) => !r.recommended);

  return (
    <div className="flex flex-col gap-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-display">Autonomous Procure-to-Pay (P2P)</h1>
          <p className="text-body-lg text-on-surface-variant">
            State the requirement in plain English. AI extracts and scores; the numbers
            decide, and the audit trail records both.
          </p>
        </div>
        {aiAvailable !== null && (
          <Badge tone={aiAvailable ? "success" : "warning"}>
            {aiAvailable ? "AI engine online" : "Deterministic fallback"}
          </Badge>
        )}
      </header>

      <Panel title="Intelligent Requisition Intake" icon="forum">
        <form onSubmit={parse} className="flex flex-col gap-3 p-5">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={3}
            className="w-full resize-none rounded-lg border border-outline-variant bg-surface-container-lowest p-3 text-body-md outline-none focus:border-primary-container focus:ring-2 focus:ring-primary-container/20"
            placeholder="e.g. 500 units of PCB controller boards for the Bhiwandi plant next week"
          />
          <RequirePermission
            permission={PERM.procurementWrite}
            action="raise requisitions (Procurement and Administrators can)"
          >
            <div className="flex items-center justify-between gap-3">
              <p className="text-body-sm text-on-surface-variant">
                No record is committed until the requirement is unambiguous.
              </p>
              <button type="submit" className="btn-primary" disabled={busy}>
                <Icon name="auto_awesome" />
                {busy ? "Analysing…" : "Create requisition"}
              </button>
            </div>
          </RequirePermission>
        </form>

        {error && (
          <p className="mx-5 mb-4 rounded-lg bg-error-container/60 px-3 py-2 text-body-sm text-on-error-container">
            {error}
          </p>
        )}

        {questions.length > 0 && (
          <div className="mx-5 mb-5 rounded-lg border border-warning/30 bg-warning-container/50 p-4">
            <p className="flex items-center gap-2 font-semibold text-warning">
              <Icon name="help" /> Clarification required
            </p>
            <ul className="mt-2 list-disc pl-5 text-body-md text-on-surface-variant">
              {questions.map((q) => (
                <li key={q}>{q}</li>
              ))}
            </ul>
            <p className="mt-2 text-body-sm text-on-surface-variant">
              Answer above and resubmit — the conversation context is retained.
            </p>
          </div>
        )}

        {parsed && (
          <div className="mx-5 mb-5 grid gap-3 rounded-lg bg-surface-container-low p-4 sm:grid-cols-4">
            {[
              { label: "Material", value: parsed.material_name, sub: parsed.material_id, icon: "category" },
              { label: "Quantity", value: `${parsed.qty} ${parsed.uom}`, icon: "tag" },
              { label: "Ship-to", value: parsed.delivery_location_id ?? "—",
                sub: parsed.required_date, icon: "event" },
              { label: "Extraction confidence",
                value: `${(parsed.confidence * 100).toFixed(0)}%`, icon: "verified" },
            ].map((f) => (
              <div key={f.label}>
                <span className="label">{f.label}</span>
                <p className="mt-1 flex items-center gap-1.5 text-body-lg font-medium">
                  <Icon name={f.icon} className="!text-[18px] text-primary" />
                  {f.value}
                </p>
                {f.sub && <p className="mono text-on-surface-variant">{f.sub}</p>}
              </div>
            ))}
          </div>
        )}

        {reqId && !poId && (
          <div className="mx-5 mb-5 flex items-center justify-between gap-3 rounded-lg border border-outline-variant/60 p-4">
            <p className="text-body-md">
              Requisition <span className="mono font-semibold text-primary">{reqId}</span> created.
            </p>
            <RequirePermission
              permission={PERM.procurementWrite}
              action="raise purchase orders (Procurement and Administrators can)"
            >
              <button className="btn-primary" onClick={selectSupplier} disabled={busy}>
                <Icon name="neurology" />
                {busy ? "Evaluating…" : "Evaluate suppliers & issue PO"}
              </button>
            </RequirePermission>
          </div>
        )}
      </Panel>

      {recs.length > 0 && (
        <Panel
          title="Supplier Evaluation & Award"
          icon="workspaces"
          action={<Badge tone="neutral">{recs.length} suppliers evaluated</Badge>}
        >
          <div className="grid gap-4 p-5 lg:grid-cols-[1.4fr_1fr]">
            {winner && (
              <div className="rounded-xl border-2 border-primary-container p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h3 className="text-headline-md">{winner.supplier_name}</h3>
                    <p className="text-body-sm text-on-surface-variant">
                      {winner.supplier_id} · rank #{winner.rank}
                    </p>
                  </div>
                  <div className="text-right">
                    <Badge tone="primary">Awarded</Badge>
                    <p className="mt-1 text-display leading-none text-primary tnum">
                      {(winner.overall_score * 100).toFixed(0)}%
                    </p>
                    <p className="text-body-sm text-on-surface-variant">composite score</p>
                  </div>
                </div>

                <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {[
                    { label: "Unit rate", value: money(winner.quoted_unit_price) },
                    { label: "Quality rating", value: `${(winner.quality_score * 100).toFixed(0)}%` },
                    { label: "Lead time", value: `${winner.quoted_lead_time_days} days` },
                    { label: "Risk exposure", value: winner.risk_score < 0.15 ? "Low" : winner.risk_score < 0.25 ? "Medium" : "High" },
                  ].map((s) => (
                    <div key={s.label} className="rounded-lg bg-surface-container-low p-2.5 text-center">
                      <span className="label">{s.label}</span>
                      <p className="mt-0.5 text-body-lg font-semibold">{s.value}</p>
                    </div>
                  ))}
                </div>

                <div className="mt-4 rounded-lg bg-surface-container-low p-3">
                  <p className="flex items-center gap-1.5 text-body-sm font-semibold text-primary">
                    <Icon name="neurology" className="!text-[18px]" /> Award rationale
                  </p>
                  <p className="mt-1 text-body-md text-on-surface-variant">{winner.reasoning}</p>
                </div>

                {poId && (
                  <Link to={`/traceability/${poId}`} className="btn-primary mt-4 w-full">
                    <Icon name="conversion_path" />
                    {poId} issued — view audit trail
                  </Link>
                )}
              </div>
            )}

            <div className="flex flex-col gap-3">
              {others.map((r) => (
                <div key={r.supplier_id} className="card-pad">
                  <div className="flex items-start justify-between">
                    <div>
                      <h4 className="text-body-lg font-semibold">{r.supplier_name}</h4>
                      <p className="text-body-sm text-on-surface-variant">rank #{r.rank}</p>
                    </div>
                    <span className="text-headline-md tnum text-on-surface-variant">
                      {(r.overall_score * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="mt-2 flex justify-between text-body-sm">
                    <span>
                      Rate: <strong>{money(r.quoted_unit_price)}</strong>
                    </span>
                    <span>
                      Lead time: <strong>{r.quoted_lead_time_days}d</strong>
                    </span>
                  </div>
                  <p className="mt-2 text-body-sm italic text-on-surface-variant">{r.reasoning}</p>
                </div>
              ))}
            </div>
          </div>
        </Panel>
      )}
    </div>
  );
}
