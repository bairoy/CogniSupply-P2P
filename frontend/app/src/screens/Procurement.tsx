import { SyntheticEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { PROCUREMENT, api } from "../api";
import { PERM, RequirePermission, useAuth } from "../auth";
import { Badge, Icon, Panel, clock, money } from "../components/ui";

/**
 * Autonomous Procure-to-Pay -- requisition intake and supplier award.
 *
 * Two AI touchpoints, both extraction/explanation rather than decision:
 *   - the chat parses free text into a structured requisition,
 *   - the supplier cards show a deterministic score with an LLM-written
 *     rationale beside it.
 * The `ai_available` flag is surfaced honestly so a fallback run is visible
 * rather than passed off as the real thing.
 *
 * Intake is a conversation on the left and its structured result on the right,
 * side by side on purpose. The brief asks for a conversational NLP intake, but
 * a procurement officer's job is not to chat -- it is to see what the machine
 * understood before it commits a record. Turning the whole screen into a chat
 * window would hide the extraction; putting them beside each other means every
 * sentence typed has its consequence visible in the same glance.
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

const PROMPTS = [
  EXAMPLE,
  "Urgent: 200 units of PCB controller boards for the Pune line, needed within a week",
  "Raise a requisition for hydraulic seals",
];

/** One rendered turn of the intake conversation. */
interface Bubble {
  id: number;
  role: "user" | "assistant";
  content: string;
  /** Assistant turns are styled by what they are, not just who said them. */
  kind?: "question" | "result" | "error";
  at: string;
}

let bubbleSeq = 0;
function bubble(role: Bubble["role"], content: string, kind?: Bubble["kind"]): Bubble {
  bubbleSeq += 1;
  return { id: bubbleSeq, role, content, kind, at: new Date().toISOString() };
}

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
  const [messages, setMessages] = useState<Bubble[]>([
    bubble(
      "assistant",
      "State what you need in plain English — material, quantity, destination and date. " +
        "I will extract it, and ask if anything is ambiguous. Nothing is committed until it is not.",
    ),
  ]);
  const { user } = useAuth();
  const transcript = useRef<HTMLDivElement>(null);

  /* Keep the newest turn in view, the way any chat client does. */
  useEffect(() => {
    transcript.current?.scrollTo({ top: transcript.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  /** Submitted from the form or from Enter in the composer -- either way, one path. */
  async function parse(e: SyntheticEvent) {
    e.preventDefault();
    const message = text.trim();
    if (!message) return;
    setBusy(true);
    setError(null);
    setMessages((prev) => [...prev, bubble("user", message)]);
    setText("");
    try {
      // `history` is the server's own conversation state, threaded back exactly
      // as it was handed to us -- the model needs the earlier turns to resolve
      // "make it 600 instead" against what was already understood.
      const res = await api.post<ChatResponse>(PROCUREMENT, "/requisitions/chat", {
        message,
        history,
      });
      setAiAvailable(res.ai_available);
      if (res.status === "clarifying") {
        setQuestions(res.questions);
        setParsed(res.draft);
        setHistory(res.history);
        setReqId(null);
        setMessages((prev) => [
          ...prev,
          ...res.questions.map((q) => bubble("assistant", q, "question")),
        ]);
      } else {
        setQuestions([]);
        setParsed(res.parsed);
        setReqId(res.id);
        setHistory([]);
        setPoId(null);
        setRecs([]);
        setMessages((prev) => [
          ...prev,
          bubble(
            "assistant",
            `Understood. Requisition ${res.id} raised for ${res.parsed.qty} ${res.parsed.uom} of ` +
              `${res.parsed.material_name}${res.parsed.required_date ? `, required by ${res.parsed.required_date}` : ""}. ` +
              `Extraction confidence ${(res.parsed.confidence * 100).toFixed(0)}%. ` +
              "Evaluate suppliers when you are ready.",
            "result",
          ),
        ]);
      }
    } catch (err) {
      const detail = err instanceof Error ? err.message : "Could not interpret the requirement";
      setError(detail);
      setMessages((prev) => [...prev, bubble("assistant", detail, "error")]);
      // The turn never reached the model, so it is not part of the thread.
      setText(message);
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

      {/* ---- intelligent requisition intake: conversation | extraction ---- */}
      <div className="grid gap-6 xl:grid-cols-2">
        {/* -- the conversation -- */}
        <Panel
          title="Enterprise AI Assistant"
          icon="forum"
          action={
            <span className="flex items-center gap-1.5 text-body-sm text-on-surface-variant">
              <span className={`h-2 w-2 rounded-full ${aiAvailable === false ? "bg-warning" : "bg-success"}`} />
              {aiAvailable === false ? "Rule-based fallback" : "Conversational NLP"}
            </span>
          }
        >
          <div className="flex h-full min-h-[460px] flex-col">
            <div ref={transcript} className="flex-1 overflow-auto p-5">
              <div className="flex flex-col gap-3">
                {messages.map((m) => {
                  const mine = m.role === "user";
                  const tone =
                    m.kind === "error"
                      ? "bg-error-container text-on-error-container"
                      : m.kind === "question"
                        ? "bg-warning-container text-on-surface"
                        : m.kind === "result"
                          ? "bg-success-container text-on-surface"
                          : "bg-surface-container-low text-on-surface";
                  return (
                    <div
                      key={m.id}
                      className={`flex items-end gap-2 ${mine ? "flex-row-reverse" : ""}`}
                    >
                      <span
                        className={`grid h-8 w-8 shrink-0 place-items-center rounded-full text-[11px] font-semibold ${
                          mine
                            ? "bg-surface-container-high text-on-surface-variant"
                            : "bg-primary-container text-on-primary"
                        }`}
                        title={mine ? user?.name ?? "You" : "Procurement AI"}
                      >
                        {mine ? (
                          (user?.name ?? "You").slice(0, 2).toUpperCase()
                        ) : (
                          <Icon name="neurology" className="!text-[18px]" />
                        )}
                      </span>
                      <div className={`max-w-[80%] ${mine ? "text-right" : ""}`}>
                        <div
                          className={`inline-block rounded-xl px-3.5 py-2.5 text-left text-body-md ${
                            mine
                              ? "rounded-br-sm bg-primary-container text-on-primary"
                              : `rounded-bl-sm ${tone}`
                          }`}
                        >
                          {m.kind === "question" && (
                            <span className="mb-1 flex items-center gap-1.5 text-label-md uppercase text-warning">
                              <Icon name="help" className="!text-[14px]" /> Clarification required
                            </span>
                          )}
                          {m.content}
                        </div>
                        <p className="mt-0.5 px-1 text-[11px] text-outline">{clock(m.at)}</p>
                      </div>
                    </div>
                  );
                })}

                {busy && (
                  <div className="flex items-end gap-2">
                    <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-primary-container text-on-primary">
                      <Icon name="neurology" className="!text-[18px]" />
                    </span>
                    <div className="rounded-xl rounded-bl-sm bg-surface-container-low px-4 py-3">
                      <span className="flex gap-1">
                        {[0, 1, 2].map((i) => (
                          <span
                            key={i}
                            className="scan-pulse h-1.5 w-1.5 rounded-full bg-on-surface-variant"
                            style={{ animationDelay: `${i * 160}ms` }}
                          />
                        ))}
                      </span>
                    </div>
                  </div>
                )}
              </div>
            </div>

            <div className="border-t border-outline-variant/60 p-4">
              <div className="mb-2 flex flex-wrap gap-1.5">
                {PROMPTS.map((p) => (
                  <button
                    key={p}
                    type="button"
                    onClick={() => setText(p)}
                    className="rounded-full border border-outline-variant px-2.5 py-1 text-body-sm text-on-surface-variant transition hover:bg-surface-container-low"
                  >
                    {p.length > 46 ? `${p.slice(0, 46)}…` : p}
                  </button>
                ))}
              </div>
              <RequirePermission
                permission={PERM.procurementWrite}
                action="raise requisitions (Procurement and Administrators can)"
              >
                <form onSubmit={parse} className="flex items-end gap-2">
                  <textarea
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => {
                      // Enter sends, Shift+Enter breaks the line -- the
                      // convention every chat client already taught the user.
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        parse(e);
                      }
                    }}
                    rows={2}
                    className="flex-1 resize-none rounded-lg border border-outline-variant bg-surface-container-lowest p-3 text-body-md outline-none focus:border-primary-container focus:ring-2 focus:ring-primary-container/20"
                    placeholder="e.g. 500 units of PCB controller boards for the Bhiwandi plant next week"
                  />
                  <button type="submit" className="btn-primary h-[46px]" disabled={busy}>
                    <Icon name={busy ? "hourglass_top" : "send"} />
                    {busy ? "Analysing…" : "Send"}
                  </button>
                </form>
              </RequirePermission>
              <p className="mt-2 text-body-sm text-on-surface-variant">
                No record is committed until the requirement is unambiguous
                {questions.length > 0 && (
                  <span className="text-warning">
                    {" "}
                    — {questions.length} open question{questions.length === 1 ? "" : "s"}
                  </span>
                )}
                .
              </p>
            </div>
          </div>
        </Panel>

        {/* -- what the machine understood -- */}
        <Panel
          title="Extracted Requisition"
          icon="data_object"
          action={
            parsed ? (
              <Badge tone={reqId ? "success" : "warning"}>
                {reqId ? "Committed" : "Draft — not yet committed"}
              </Badge>
            ) : undefined
          }
        >
          <div className="flex h-full min-h-[460px] flex-col gap-4 p-5">
            {!parsed ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 text-center">
                <Icon name="data_object" className="!text-[32px] text-outline" />
                <p className="text-body-md text-on-surface-variant">
                  Nothing extracted yet.
                </p>
                <p className="text-body-sm text-outline">
                  The structured fields the assistant pulls out of your sentence appear here,
                  field by field, before anything is written.
                </p>
              </div>
            ) : (
              <>
                <div className="grid gap-3 rounded-lg bg-surface-container-low p-4 sm:grid-cols-2">
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

                <div>
                  <div className="flex items-baseline justify-between">
                    <span className="label">Confidence</span>
                    <span className="tnum text-body-sm font-semibold text-primary">
                      {(parsed.confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-container-high">
                    <div
                      className={`h-full rounded-full ${parsed.confidence >= 0.8 ? "bg-success" : "bg-warning"}`}
                      style={{ width: `${parsed.confidence * 100}%` }}
                    />
                  </div>
                </div>

                {questions.length > 0 && (
                  <div className="rounded-lg border border-warning/30 bg-warning-container/50 p-3">
                    <p className="flex items-center gap-2 text-body-md font-semibold text-warning">
                      <Icon name="help" className="!text-[18px]" /> Held pending clarification
                    </p>
                    <ul className="mt-1.5 list-disc pl-5 text-body-sm text-on-surface-variant">
                      {questions.map((q) => (
                        <li key={q}>{q}</li>
                      ))}
                    </ul>
                    <p className="mt-2 text-body-sm text-on-surface-variant">
                      Answer in the conversation — the thread is retained, so you only have to
                      supply what is missing.
                    </p>
                  </div>
                )}

                {error && (
                  <p className="rounded-lg bg-error-container/60 px-3 py-2 text-body-sm text-on-error-container">
                    {error}
                  </p>
                )}

                {reqId && !poId && (
                  <div className="mt-auto flex flex-col gap-2 rounded-lg border border-outline-variant/60 p-4">
                    <p className="text-body-md">
                      Requisition <span className="mono font-semibold text-primary">{reqId}</span>{" "}
                      created and ready for sourcing.
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

                {/* Gated on reqId as well as poId: a clarifying turn replaces
                    the draft with a different requirement, and leaving "PO-1063
                    issued from this requisition" sitting under it would credit
                    the new draft with the previous requisition's order. */}
                {reqId && poId && (
                  <div className="mt-auto rounded-lg border border-success/30 bg-success-container/50 p-3">
                    <p className="text-body-md font-semibold text-success">
                      {poId} issued from this requisition.
                    </p>
                  </div>
                )}
              </>
            )}
          </div>
        </Panel>
      </div>

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
