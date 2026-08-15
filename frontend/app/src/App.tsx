import { FormEvent, useEffect, useState } from "react";
import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { api, Overview } from "./api";
import { Badge, Icon, ago } from "./components/ui";
import { useEventStream, useRefetchOn } from "./hooks/useEventStream";
import ControlTower from "./screens/ControlTower";
import Exceptions from "./screens/Exceptions";
import MatchPay from "./screens/MatchPay";
import Procurement from "./screens/Procurement";
import Traceability from "./screens/Traceability";
import Track from "./screens/Track";
import YardDock from "./screens/YardDock";

const NAV = [
  { to: "/", label: "Control Tower", icon: "dashboard", end: true },
  { to: "/yard", label: "Yard & Dock", icon: "local_shipping" },
  { to: "/procurement", label: "Procurement / Sourcing AI", icon: "neurology" },
  { to: "/match-pay", label: "Match & Pay", icon: "payments" },
  { to: "/exceptions", label: "Exceptions", icon: "error" },
  { to: "/traceability", label: "Traceability", icon: "conversion_path" },
];

/** Colour-code the event rail the way the design mockup does. */
function eventTone(type: string) {
  if (type.includes("EXCEPTION") || type.includes("DELAYED") || type.includes("CONFLICT"))
    return "text-error";
  if (type.includes("MATCH") || type.includes("PAYMENT") || type.includes("RECEIVED"))
    return "text-success";
  if (type.includes("DOCK")) return "text-primary";
  return "text-on-surface-variant";
}

export default function App() {
  const { events, state, lastEventAt } = useEventStream();
  const [openExceptions, setOpenExceptions] = useState(0);
  const [query, setQuery] = useState("");
  const [searchError, setSearchError] = useState<string | null>(null);
  const navigate = useNavigate();

  const loadBadge = () =>
    api
      .gateway<Overview>("/dashboard/overview")
      .then((d) => setOpenExceptions(d.open_exceptions))
      .catch(() => undefined);

  useEffect(() => {
    loadBadge();
  }, []);
  useRefetchOn(events, ["EXCEPTION_CREATED", "EXCEPTION_RESOLVED", "MATCH_COMPLETED"], loadBadge);

  /** Cmd+K focuses global search, as the design's shortcut chip advertises. */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        document.getElementById("global-search")?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function onSearch(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setSearchError(null);
    try {
      const res = await api.gateway<{ exact: { entity_type: string; entity_id: string } | null }>(
        `/search?q=${encodeURIComponent(q)}`,
      );
      const hit = res.exact;
      if (!hit) {
        setSearchError(`Nothing matched "${q}"`);
        return;
      }
      const routes: Record<string, string> = {
        trailer: `/track/${hit.entity_id}`,
        shipment: `/track/${hit.entity_id}`,
        purchase_order: `/traceability/${hit.entity_id}`,
        invoice: `/match-pay/${hit.entity_id}`,
        exception: `/exceptions`,
      };
      navigate(routes[hit.entity_type] ?? "/");
      setQuery("");
    } catch (err) {
      setSearchError(err instanceof Error ? err.message : "search failed");
    }
  }

  const connection = {
    live: { tone: "success" as const, dot: "bg-success", label: "Live" },
    connecting: { tone: "warning" as const, dot: "bg-warning animate-pulse", label: "Connecting" },
    offline: { tone: "danger" as const, dot: "bg-error", label: "Offline" },
  }[state];

  return (
    <div className="flex h-full">
      {/* ---- sidebar (fixed 240px per DESIGN.md layout model) ---- */}
      <aside className="hidden md:flex w-[260px] shrink-0 flex-col border-r border-outline-variant/60 bg-surface-container-low">
        <div className="px-5 py-5">
          <div className="flex items-center gap-3">
            <div className="grid h-10 w-10 place-items-center rounded-lg bg-primary-container text-on-primary font-bold">
              IP
            </div>
            <div>
              <h1 className="text-headline-md text-primary leading-tight">Inbound → Pay</h1>
              <p className="text-body-sm text-on-surface-variant">Enterprise Control Tower</p>
            </div>
          </div>
        </div>

        <nav className="flex flex-col gap-1 px-3">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `nav-item ${isActive ? "nav-item-active" : ""}`}
            >
              <Icon name={item.icon} />
              <span className="flex-1">{item.label}</span>
              {item.to === "/exceptions" && openExceptions > 0 && (
                <span className="badge bg-error-container text-on-error-container">
                  {openExceptions}
                </span>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="mt-auto border-t border-outline-variant/60 px-5 py-4 text-body-sm text-on-surface-variant">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${connection.dot}`} />
            <span>WebSocket: {connection.label}</span>
          </div>
          <div className="mt-1 flex items-center gap-2">
            <Icon name="sync" className="!text-[16px]" />
            <span>Last event: {lastEventAt ? ago(lastEventAt.toISOString()) : "—"}</span>
          </div>
        </div>
      </aside>

      {/* ---- main column ---- */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-4 border-b border-outline-variant/60 bg-surface-container-lowest px-6 py-3">
          <form onSubmit={onSearch} className="relative flex-1 max-w-xl">
            <Icon
              name="search"
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-outline"
            />
            <input
              id="global-search"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setSearchError(null);
              }}
              placeholder="Search by ID (PO-, INV-, TRL-, SHP-, tracking no.)"
              className="w-full rounded-lg border border-outline-variant bg-surface-container-lowest py-2 pl-10 pr-16 text-body-md outline-none transition focus:border-primary-container focus:ring-2 focus:ring-primary-container/20"
            />
            <kbd className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 rounded border border-outline-variant px-1.5 py-0.5 text-[11px] text-on-surface-variant">
              ⌘K
            </kbd>
          </form>
          {searchError && (
            <span className="text-body-sm text-error">{searchError}</span>
          )}
          <div className="ml-auto flex items-center gap-3">
            <Badge tone={connection.tone}>{connection.label}</Badge>
          </div>
        </header>

        <div className="flex min-h-0 flex-1">
          <main className="min-w-0 flex-1 overflow-auto p-6">
            <Routes>
              <Route path="/" element={<ControlTower events={events} />} />
              <Route path="/yard" element={<YardDock events={events} />} />
              <Route path="/procurement" element={<Procurement />} />
              <Route path="/match-pay" element={<MatchPay />} />
              <Route path="/match-pay/:invoiceId" element={<MatchPay />} />
              <Route path="/exceptions" element={<Exceptions events={events} />} />
              <Route path="/traceability" element={<Traceability />} />
              <Route path="/traceability/:poId" element={<Traceability />} />
              <Route path="/track/:ref" element={<Track />} />
            </Routes>
          </main>

          {/* ---- live event rail ---- */}
          <aside className="hidden xl:flex w-[340px] shrink-0 flex-col border-l border-outline-variant/60 bg-surface-container-lowest">
            <header className="flex items-center justify-between border-b border-outline-variant/60 px-4 py-3">
              <h2 className="flex items-center gap-2 text-body-lg font-semibold">
                <Icon name="bolt" className="text-primary" />
                Live Event Stream
              </h2>
              <span className={`h-2 w-2 rounded-full ${connection.dot}`} />
            </header>
            <div className="flex-1 overflow-auto">
              {events.length === 0 ? (
                <p className="px-4 py-6 text-body-sm text-on-surface-variant">
                  Waiting for events. Run the simulator, or act in the UI, to see live
                  traffic.
                </p>
              ) : (
                <ul>
                  {events.map((e) => {
                    const payload =
                      typeof e.payload === "object" && e.payload
                        ? (e.payload as Record<string, unknown>)
                        : {};
                    return (
                      <li
                        key={e.event_id}
                        className="border-b border-outline-variant/40 px-4 py-2.5 hover:bg-surface-container-low"
                      >
                        <div className="flex items-baseline gap-2">
                          <span className="mono text-outline">
                            {new Date(e.timestamp).toLocaleTimeString([], {
                              hour: "2-digit",
                              minute: "2-digit",
                              second: "2-digit",
                            })}
                          </span>
                          <span className={`mono font-semibold ${eventTone(e.event_type)}`}>
                            {e.event_type}
                          </span>
                        </div>
                        <p className="mt-0.5 text-body-sm text-on-surface-variant">
                          {(payload.summary as string) ?? `${e.entity_type} ${e.entity_id}`}
                        </p>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </aside>
        </div>
      </div>
    </div>
  );
}
