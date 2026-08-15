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

interface Catalogue {
  materials: { id: string; name: string; uom: string; requires_approval: boolean }[];
  locations: { id: string; name: string; type: string }[];
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
  { text: EXAMPLE, icon: "precision_manufacturing", label: "Standard Order" },
  { text: "Urgent: 200 units of PCB controller boards for the Pune line, needed within a week", icon: "priority_high", label: "Urgent Order" },
  { text: "Raise a requisition for hydraulic seals", icon: "help", label: "Ambiguous (triggers clarification)" },
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
  const [showPO, setShowPO] = useState(false);
  const [recs, setRecs] = useState<Recommendation[]>([]);
  const [aiAvailable, setAiAvailable] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [showCatalogue, setShowCatalogue] = useState(false);
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

  useEffect(() => {
    api.procurement<Catalogue>("/catalogue")
      .then(setCatalogue)
      .catch((err) => {
        console.error("Failed to load Master Data", err);
        setCatalogue(null);
      });
  }, []);

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
        setShowPO(false);
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
      setText(message);
    } finally {
      setBusy(false);
    }
  }

  async function selectSupplier() {
    if (!reqId) return;
    setBusy(true);
    setError(null);
    setShowPO(false);
    try {
      const res = await api.post<{ purchase_order_id: string; recommendations: Recommendation[];
        ai_available: boolean }>(PROCUREMENT, `/requisitions/${reqId}/select-supplier`);
      setPoId(res.purchase_order_id);
      setRecs(res.recommendations);
      setAiAvailable(res.ai_available);
      
      setTimeout(() => setShowPO(true), 1500);
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
        <div className="flex items-center gap-3">
          <div className="grid h-11 w-11 place-items-center rounded-xl bg-indigo-50 border border-indigo-100">
            <Icon name="neurology" className="!text-[24px] text-indigo-600" />
          </div>
          <div>
            <h1 className="text-display font-bold tracking-tight text-on-surface">
              Autonomous Procure-to-Pay
            </h1>
            <p className="mt-0.5 text-sm text-on-surface-variant">
              Intelligent requisition intake · Autonomous supplier evaluation · PO issuance
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {aiAvailable !== null && (
            <span className={`flex items-center gap-2 rounded-full px-3.5 py-1.5 text-xs font-semibold border ${
              aiAvailable
                ? "bg-emerald-50 border-emerald-200 text-emerald-700"
                : "bg-amber-50 border-amber-200 text-amber-700"
            }`}>
              <span className={`h-2 w-2 rounded-full ${aiAvailable ? "bg-emerald-500" : "bg-amber-500 animate-pulse"}`} />
              {aiAvailable ? "AI Engine Online" : "Deterministic Fallback"}
            </span>
          )}
        </div>
      </header>

      {/* ---- intelligent requisition intake: conversation | extraction ---- */}
      <div className="grid gap-6 xl:grid-cols-2">
        {/* -- the conversation -- */}
        <Panel
          title="Conversational Intake"
          icon="forum"
          className="border-slate-200/80 bg-white shadow-[0_4px_24px_rgb(0,0,0,0.06)]"
          action={
            <div className="flex items-center gap-3">
              <span className="flex items-center gap-1.5 rounded-md bg-slate-50 px-2.5 py-1 text-[11px] font-semibold text-slate-600 ring-1 ring-slate-200">
                <Icon name="smart_toy" className="!text-[14px] text-indigo-500" />
                NLP Engine
              </span>
              <span className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11px] font-semibold ring-1 ${
                aiAvailable === false
                  ? "bg-amber-50 text-amber-700 ring-amber-200"
                  : "bg-emerald-50 text-emerald-700 ring-emerald-200"
              }`}>
                <span className={`h-1.5 w-1.5 rounded-full ${aiAvailable === false ? "bg-amber-500 animate-pulse" : "bg-emerald-500"}`} />
                {aiAvailable === false ? "Fallback" : "Online"}
              </span>
            </div>
          }
        >
          <div className="flex h-full min-h-[460px] flex-col">
            <div ref={transcript} className="flex-1 overflow-auto p-5">
              <div className="flex flex-col gap-3">
                {messages.map((m) => {
                  const mine = m.role === "user";
                  const tone =
                    m.kind === "error"
                      ? "bg-red-50 text-red-700 border border-red-200"
                      : m.kind === "question"
                        ? "bg-amber-50 text-amber-900 border border-amber-200"
                        : m.kind === "result"
                          ? "bg-emerald-50 text-emerald-900 border border-emerald-200 shadow-sm"
                          : mine
                            ? "bg-gradient-to-r from-indigo-500 to-violet-500 text-white shadow-md border border-indigo-400/50"
                            : "bg-white text-gray-800 border border-gray-200 shadow-sm";
                  return (
                    <div
                      key={m.id}
                      className={`flex items-end gap-3 toast-in ${mine ? "flex-row-reverse" : ""}`}
                    >
                      <span
                        className={`grid h-8 w-8 shrink-0 place-items-center rounded-full text-[11px] font-semibold shadow-sm ${
                          mine
                            ? "bg-gradient-to-br from-indigo-400 to-purple-500 text-white"
                            : "bg-gradient-to-br from-blue-500 to-cyan-400 text-white"
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
                          className={`inline-block rounded-2xl px-4 py-2.5 text-left text-body-md ${
                            mine ? "rounded-br-sm" : "rounded-bl-sm"
                          } ${tone}`}
                        >
                          {m.kind === "question" && (
                            <span className="mb-1 flex items-center gap-1.5 text-label-md uppercase text-amber-600">
                              <Icon name="help" className="!text-[14px]" /> Clarification required
                            </span>
                          )}
                          {m.content}
                        </div>
                        <p className="mt-1 px-2 text-[10px] font-medium text-gray-400">{clock(m.at)}</p>
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

            <div className="border-t border-slate-100 bg-slate-50/50 p-4">
              {messages.length <= 1 && (
                <>
                  <p className="mb-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">Quick Start Templates</p>
                  <div className="mb-3 flex flex-wrap gap-2">
                    {PROMPTS.map((p) => (
                      <button
                        key={p.text}
                        type="button"
                        onClick={() => setText(p.text)}
                        className="group flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-left text-[12px] shadow-sm transition-all hover:border-indigo-300 hover:bg-indigo-50/50 hover:shadow-md"
                      >
                        <Icon name={p.icon} className="!text-[16px] text-slate-400 group-hover:text-indigo-500 transition-colors" />
                        <div>
                          <span className="font-semibold text-slate-700 group-hover:text-indigo-700">{p.label}</span>
                          <span className="block text-[11px] text-slate-400 line-clamp-1">{p.text.slice(0, 55)}…</span>
                        </div>
                      </button>
                    ))}
                  </div>
                </>
              )}
              <RequirePermission
                permission={PERM.procurementWrite}
                action="raise requisitions (Procurement and Administrators can)"
              >
                <form onSubmit={parse} className="flex items-end gap-3">
                  <textarea
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        parse(e);
                      }
                    }}
                    rows={2}
                    className="flex-1 resize-none rounded-2xl border border-indigo-200 bg-white p-3.5 text-body-md text-gray-800 shadow-inner outline-none transition-all placeholder:text-gray-400 focus:border-indigo-400 focus:ring-4 focus:ring-indigo-400/20"
                    placeholder="e.g. 500 units of PCB controller boards for the Bhiwandi plant next week"
                  />
                  <button
                    type="submit"
                    className="flex h-[52px] items-center gap-2 rounded-xl bg-gradient-to-r from-indigo-500 to-purple-600 px-5 text-white shadow-md transition-all hover:scale-[1.02] hover:shadow-lg disabled:cursor-not-allowed disabled:opacity-70 disabled:hover:scale-100"
                    disabled={busy}
                  >
                    <Icon name={busy ? "hourglass_top" : "send"} />
                    <span className="font-semibold">{busy ? "Analysing…" : "Send"}</span>
                  </button>
                </form>
              </RequirePermission>
              <div className="mt-3 flex items-center justify-between">
                <p className="text-body-sm text-on-surface-variant">
                  No record is committed until the requirement is unambiguous
                  {questions.length > 0 && (
                    <span className="text-amber-600 font-semibold">
                      {" "}
                      — {questions.length} open question{questions.length === 1 ? "" : "s"}
                    </span>
                  )}
                  .
                </p>
                <button
                  onClick={() => setShowCatalogue(!showCatalogue)}
                  className="flex items-center gap-1 text-sm font-semibold text-indigo-600 hover:text-indigo-800 transition"
                >
                  <Icon name={showCatalogue ? "visibility_off" : "menu_book"} className="!text-[16px]" />
                  {showCatalogue ? "Hide Master Data" : "View Master Data"}
                </button>
              </div>

              {showCatalogue && catalogue && (
                <div className="mt-4 rounded-xl border border-indigo-100 bg-white/60 p-4 shadow-sm backdrop-blur-sm toast-in">
                  <h4 className="mb-2 text-[11px] font-bold uppercase tracking-wider text-gray-500">Available Materials</h4>
                  <div className="mb-4 flex flex-wrap gap-2">
                    {catalogue.materials.map((m) => (
                      <span key={m.id} className="rounded border border-indigo-50 bg-white px-2 py-1 text-xs text-gray-700 shadow-sm">
                        <strong className="text-indigo-700">{m.id}</strong>: {m.name} <span className="text-gray-400">({m.uom})</span>
                      </span>
                    ))}
                  </div>
                  <h4 className="mb-2 text-[11px] font-bold uppercase tracking-wider text-gray-500">Available Locations</h4>
                  <div className="flex flex-wrap gap-2">
                    {catalogue.locations.map((l) => (
                      <span key={l.id} className="rounded border border-indigo-50 bg-white px-2 py-1 text-xs text-gray-700 shadow-sm">
                        <strong className="text-indigo-700">{l.id}</strong>: {l.name}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </Panel>

        {/* -- what the machine understood -- */}
        <Panel
          title="Extracted Requisition"
          icon="data_object"
          className="border-slate-200/80 bg-white shadow-[0_4px_24px_rgb(0,0,0,0.06)]"
          action={
            parsed ? (
              <span className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11px] font-semibold ring-1 ${
                reqId ? "bg-emerald-50 text-emerald-700 ring-emerald-200" : "bg-amber-50 text-amber-700 ring-amber-200"
              }`}>
                <span className={`h-1.5 w-1.5 rounded-full ${reqId ? "bg-emerald-500" : "bg-amber-500 animate-pulse"}`} />
                {reqId ? "Committed" : "Draft — pending"}
              </span>
            ) : (
              <span className="flex items-center gap-1.5 rounded-md bg-slate-50 px-2.5 py-1 text-[11px] font-semibold text-slate-500 ring-1 ring-slate-200">
                <span className="h-1.5 w-1.5 rounded-full bg-slate-300 animate-pulse" />
                Awaiting Input
              </span>
            )
          }
        >
          <div className="flex h-full min-h-[460px] flex-col gap-4 p-5">
            {!parsed ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-5 text-center">
                <div className="grid grid-cols-2 gap-3 w-full max-w-sm opacity-40">
                  {[
                    { label: "Material", icon: "category" },
                    { label: "Quantity", icon: "tag" },
                    { label: "Location", icon: "location_on" },
                    { label: "Confidence", icon: "verified" },
                  ].map((f) => (
                    <div key={f.label} className="rounded-xl border border-dashed border-slate-300 bg-slate-50/50 p-4">
                      <Icon name={f.icon} className="!text-[20px] text-slate-300" />
                      <p className="mt-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-400">{f.label}</p>
                      <div className="mt-2 h-2.5 w-3/4 rounded-full bg-slate-200" />
                    </div>
                  ))}
                </div>
                <div>
                  <p className="text-body-md font-semibold text-slate-500">
                    Describe your requirement in the chat
                  </p>
                  <p className="mt-1 max-w-xs text-body-sm text-slate-400">
                    The AI will extract material, quantity, location, and delivery date into structured fields here.
                  </p>
                </div>
              </div>
            ) : (
              <>
                <div className="grid gap-3 sm:grid-cols-2">
                  {[
                    { label: "Material", value: parsed.material_name, sub: parsed.material_id, icon: "category", color: "text-blue-500", bg: "bg-blue-50/60", border: "border-blue-100/60" },
                    { label: "Quantity", value: `${parsed.qty} ${parsed.uom}`, icon: "tag", color: "text-emerald-500", bg: "bg-emerald-50/60", border: "border-emerald-100/60" },
                    { label: "Ship-to", value: parsed.delivery_location_id ?? "—",
                      sub: parsed.required_date, icon: "event", color: "text-purple-500", bg: "bg-purple-50/60", border: "border-purple-100/60" },
                    { label: "Extraction confidence",
                      value: `${(parsed.confidence * 100).toFixed(0)}%`, icon: "verified", color: "text-amber-500", bg: "bg-amber-50/60", border: "border-amber-100/60" },
                  ].map((f, i) => (
                    <div key={f.label} className={`toast-in rounded-xl border ${f.border} ${f.bg} p-4 shadow-sm transition-all hover:shadow-md`} style={{animationDelay: `${i * 100}ms`}}>
                      <span className="text-[11px] font-bold uppercase tracking-wider text-gray-500">{f.label}</span>
                      <p className="mt-1 flex items-center gap-2 text-body-lg font-semibold text-gray-800">
                        <Icon name={f.icon} className={`!text-[20px] ${f.color}`} />
                        {f.value}
                      </p>
                      {f.sub && <p className="mono mt-1 text-[12px] text-gray-500">{f.sub}</p>}
                    </div>
                  ))}
                </div>

                <div className="toast-in" style={{animationDelay: "400ms"}}>
                  <div className="mb-1.5 flex items-baseline justify-between">
                    <span className="text-[11px] font-bold uppercase tracking-wider text-gray-500">Confidence Match</span>
                    <span className="tnum text-body-sm font-bold text-gray-700">
                      {(parsed.confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-gray-100 shadow-inner">
                    <div
                      className={`h-full rounded-full transition-all duration-1000 ease-out ${parsed.confidence >= 0.8 ? "bg-gradient-to-r from-emerald-400 to-emerald-500" : "bg-gradient-to-r from-amber-400 to-orange-500"}`}
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
                  <div className="mt-auto flex flex-col gap-3 rounded-xl border border-indigo-200 bg-indigo-50/50 p-5 shadow-sm toast-in" style={{animationDelay: "500ms"}}>
                    <p className="text-body-md text-indigo-900">
                      Requisition <span className="mono font-bold text-indigo-600">{reqId}</span>{" "}
                      created and ready for autonomous sourcing.
                    </p>
                    <RequirePermission
                      permission={PERM.procurementWrite}
                      action="raise purchase orders (Procurement and Administrators can)"
                    >
                      <button className="flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-indigo-500 to-purple-600 px-4 py-2.5 font-medium text-white shadow-md transition-all hover:scale-[1.02] hover:shadow-lg disabled:cursor-not-allowed disabled:opacity-70 disabled:hover:scale-100" onClick={selectSupplier} disabled={busy}>
                        <Icon name="neurology" />
                        {busy ? "Evaluating Suppliers…" : "Evaluate Suppliers & Issue PO"}
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
          icon="emoji_events"
          className="toast-in mt-2 border-emerald-200/50 bg-gradient-to-br from-emerald-50/40 via-white to-teal-50/40 shadow-[0_8px_30px_rgb(0,0,0,0.06)]"
          action={<Badge tone="success">{recs.length} suppliers evaluated</Badge>}
        >
          <div className="grid gap-4 p-5 lg:grid-cols-[1.4fr_1fr]">
            {winner && (
              <div className="relative flex flex-col justify-between overflow-hidden rounded-2xl border border-emerald-200 bg-white/60 p-6 shadow-xl backdrop-blur-md">
                <div className="absolute -right-12 -top-12 h-40 w-40 rounded-full bg-gradient-to-br from-emerald-300 to-teal-400 opacity-20 blur-3xl"></div>
                <div className="relative z-10 flex items-start justify-between gap-3">
                  <div>
                    <h3 className="text-3xl font-extrabold tracking-tight text-gray-900">{winner.supplier_name}</h3>
                    <p className="mt-1 font-medium text-emerald-700/80">
                      {winner.supplier_id} · ranked #{winner.rank}
                    </p>
                  </div>
                  <div className="text-right">
                    <span className="inline-block rounded-full bg-emerald-100 px-3 py-1 text-[11px] font-bold uppercase tracking-wider text-emerald-800 shadow-sm">
                      Awarded
                    </span>
                    <p className="tnum mt-2 leading-none text-transparent bg-clip-text bg-gradient-to-r from-emerald-600 to-teal-600 text-5xl font-black">
                      {(winner.overall_score * 100).toFixed(0)}%
                    </p>
                    <p className="mt-1 text-xs font-semibold uppercase tracking-wider text-gray-500">Composite Score</p>
                  </div>
                </div>

                <div className="relative z-10 mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {[
                    { label: "Unit Rate", value: money(winner.quoted_unit_price), color: "text-indigo-600" },
                    { label: "Quality Rating", value: `${(winner.quality_score * 100).toFixed(0)}%`, color: "text-emerald-600" },
                    { label: "Lead Time", value: `${winner.quoted_lead_time_days} days`, color: "text-amber-600" },
                    { label: "Risk Exposure", value: winner.risk_score < 0.15 ? "Low" : winner.risk_score < 0.25 ? "Medium" : "High", color: "text-rose-500" },
                  ].map((s) => (
                    <div key={s.label} className="rounded-xl border border-white bg-white/80 p-3 text-center shadow-sm backdrop-blur-sm transition-all hover:-translate-y-1 hover:shadow-md">
                      <span className="text-[10px] font-bold uppercase tracking-wider text-gray-500">{s.label}</span>
                      <p className={`mt-1 text-xl font-bold ${s.color}`}>{s.value}</p>
                    </div>
                  ))}
                </div>

                <div className="relative z-10 mt-5 rounded-xl border border-emerald-100 bg-emerald-50/50 p-4 shadow-sm">
                  <p className="flex items-center gap-2 text-sm font-bold text-emerald-800">
                    <Icon name="neurology" className="!text-[20px]" /> Award Rationale
                  </p>
                  <p className="mt-2 text-body-md leading-relaxed text-emerald-900/80">{winner.reasoning}</p>
                </div>

                {poId && (
                  <Link to={`/traceability/${poId}`} className="relative z-10 mt-5 flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-emerald-500 to-teal-600 px-5 py-3 font-semibold text-white shadow-md transition-all hover:scale-[1.01] hover:shadow-lg">
                    <Icon name="conversion_path" />
                    {poId} Issued — View Audit Trail
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

      {showPO && poId && winner && parsed && (
        <div className="toast-in mt-6 overflow-hidden rounded-2xl border border-indigo-100 bg-white shadow-2xl transition-all duration-700 ease-out">
          {/* PO Header Strip */}
          <div className="bg-gradient-to-r from-gray-900 to-indigo-900 px-8 py-5 text-white flex justify-between items-center">
            <div>
              <h2 className="text-2xl font-black tracking-widest text-white/90">PURCHASE ORDER</h2>
              <p className="text-indigo-200 mt-1 flex items-center gap-2 text-sm font-medium">
                <Icon name="verified" className="!text-[16px] text-emerald-400" />
                Autonomously Issued
              </p>
            </div>
            <div className="text-right">
              <p className="text-sm font-bold uppercase tracking-wider text-indigo-300">PO Number</p>
              <p className="text-2xl font-mono font-bold text-white mt-0.5">{poId}</p>
            </div>
          </div>

          <div className="p-8">
            <div className="grid grid-cols-2 gap-12 border-b border-gray-100 pb-8">
              <div>
                <p className="text-xs font-bold uppercase tracking-widest text-gray-400 mb-3">Vendor</p>
                <h4 className="text-lg font-bold text-gray-900">{winner.supplier_name}</h4>
                <p className="text-sm font-medium text-gray-500 mt-1">Vendor ID: {winner.supplier_id}</p>
                <p className="text-sm text-gray-500 mt-1">Expected Lead Time: {winner.quoted_lead_time_days} Days</p>
              </div>
              <div>
                <p className="text-xs font-bold uppercase tracking-widest text-gray-400 mb-3">Ship To</p>
                <h4 className="text-lg font-bold text-gray-900">{parsed.delivery_location_id}</h4>
                <p className="text-sm text-gray-500 mt-1">Required by: {parsed.required_date || "Standard Schedule"}</p>
              </div>
            </div>

            <div className="mt-8">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b-2 border-gray-100">
                    <th className="py-3 font-bold text-xs uppercase tracking-wider text-gray-500">Item & Description</th>
                    <th className="py-3 text-right font-bold text-xs uppercase tracking-wider text-gray-500">Qty</th>
                    <th className="py-3 text-right font-bold text-xs uppercase tracking-wider text-gray-500">Unit Price</th>
                    <th className="py-3 text-right font-bold text-xs uppercase tracking-wider text-gray-500">Line Total</th>
                  </tr>
                </thead>
                <tbody className="text-sm">
                  <tr className="border-b border-gray-50">
                    <td className="py-5">
                      <p className="font-bold text-gray-900 text-base">{parsed.material_name}</p>
                      <p className="text-gray-500 mt-1 text-xs">SKU: {parsed.material_id}</p>
                    </td>
                    <td className="py-5 text-right font-medium text-gray-700">{parsed.qty} {parsed.uom}</td>
                    <td className="py-5 text-right font-medium text-gray-700">{money(winner.quoted_unit_price)}</td>
                    <td className="py-5 text-right font-bold text-gray-900 text-base">{money(parsed.qty * winner.quoted_unit_price)}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div className="mt-8 flex justify-end">
              <div className="w-1/3 min-w-[250px] bg-gray-50 rounded-xl p-5 border border-gray-100">
                <div className="flex justify-between items-center mb-2">
                  <span className="text-sm text-gray-500 font-medium">Subtotal</span>
                  <span className="text-sm font-semibold text-gray-800">{money(parsed.qty * winner.quoted_unit_price)}</span>
                </div>
                <div className="flex justify-between items-center mb-4">
                  <span className="text-sm text-gray-500 font-medium">Tax</span>
                  <span className="text-sm font-semibold text-gray-800">—</span>
                </div>
                <div className="flex justify-between items-center pt-4 border-t border-gray-200">
                  <span className="text-base font-bold text-gray-900 uppercase">Total</span>
                  <span className="text-xl font-black text-indigo-700">{money(parsed.qty * winner.quoted_unit_price)}</span>
                </div>
              </div>
            </div>
            
            <div className="mt-8 flex items-center justify-between border-t border-gray-100 pt-6">
              <p className="text-xs font-medium text-gray-400 max-w-md">
                This document was generated autonomously by the CogniSupply Agent using advanced NLP & dynamic supplier evaluation logic.
              </p>
              <Link to={`/traceability/${poId}`} className="flex items-center gap-2 rounded-lg bg-gray-900 px-4 py-2 text-sm font-bold text-white transition hover:bg-gray-800">
                View Audit Trail <Icon name="arrow_forward" className="!text-[16px]" />
              </Link>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
