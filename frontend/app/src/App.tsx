import { FormEvent, useEffect, useRef, useState } from "react";
import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { api, Overview } from "./api";
import { ROLE_LABEL, useAuth } from "./auth";
import EventToaster from "./components/EventToaster";
import { Badge, Icon, Spinner, ago } from "./components/ui";
import { useEventStream, useRefetchOn } from "./hooks/useEventStream";
import ControlTower from "./screens/ControlTower";
import Exceptions from "./screens/Exceptions";
import Login from "./screens/Login";
import MatchPay from "./screens/MatchPay";
import Outbound from "./screens/Outbound";
import Procurement from "./screens/Procurement";
import Traceability from "./screens/Traceability";
import Track from "./screens/Track";
import YardDock from "./screens/YardDock";

/**
 * `primary` marks the screen a role works in all day -- it is pinned to the
 * top of that role's sidebar. Every role can still reach every screen: reads
 * are open to any signed-in user, and hiding a supply chain from the people
 * upstream of it is exactly the silo this project exists to remove.
 */
const NAV = [
  { to: "/", label: "Control Tower", icon: "dashboard", end: true, primary: [] as string[] },
  { to: "/yard", label: "Dock & Yard Control", icon: "local_shipping", primary: ["operator"] },
  { to: "/outbound", label: "Outbound Fulfilment", icon: "outbound", primary: ["operator"] },
  { to: "/procurement", label: "Autonomous P2P", icon: "neurology", primary: ["procurement"] },
  { to: "/match-pay", label: "Invoice Settlement", icon: "payments", primary: ["finance"] },
  { to: "/exceptions", label: "Exception Queue", icon: "error", primary: ["finance", "procurement"] },
  { to: "/traceability", label: "Traceability", icon: "conversion_path", primary: [] },
];

function navForRole(role: string) {
  const [home, ...rest] = NAV;
  const primary = rest.filter((n) => n.primary.includes(role));
  const secondary = rest.filter((n) => !n.primary.includes(role));
  return [home, ...primary, ...secondary];
}

/** Sidebar avatar + sign-out. */
function UserMenu({ collapsed = false }: { collapsed?: boolean }) {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  if (!user) return null;
  const initials = user.name
    .split(" ")
    .map((p) => p[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left transition-colors hover:bg-surface-container"
      >
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-primary-container text-body-sm font-semibold text-on-primary">
          {initials}
        </span>
        {!collapsed && (
          <span className="min-w-0 flex-1">
            <span className="block truncate text-body-md font-medium">{user.name}</span>
            <span className="block truncate text-body-sm text-on-surface-variant">
              {ROLE_LABEL[user.role] ?? user.role}
            </span>
          </span>
        )}
        <Icon name="expand_more" className="text-on-surface-variant" />
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-20 mb-1 w-full min-w-[200px] rounded-lg border border-outline-variant bg-surface-container-lowest p-1 shadow-lg">
          <div className="px-3 py-2">
            <p className="mono truncate text-outline">{user.email ?? user.id}</p>
            <p className="mt-1 text-body-sm text-on-surface-variant">
              {user.permissions.length} permission{user.permissions.length === 1 ? "" : "s"}
            </p>
          </div>
          <button
            type="button"
            onClick={logout}
            className="nav-item w-full text-error hover:bg-error-container/40"
          >
            <Icon name="logout" />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

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
  const { user, loading } = useAuth();

  // Revalidating a token that survived a page reload. Showing the sign-in
  // screen during this flash would make a reload look like a logout.
  if (loading) {
    return (
      <div className="grid h-full place-items-center">
        <Spinner label="Restoring your session" />
      </div>
    );
  }

  return (
    <Routes>
      {/*
        The customer-facing tracker is deliberately public -- the backend leaves
        GET /track/{ref} unauthenticated (BUILD_PLAN §161), so a supplier with a
        tracking number must not be forced through a sign-in screen to use it.

        It also renders OUTSIDE the Shell for everyone, signed in or not. It is
        the one screen an external party sees, and wrapping it in our sidebar,
        global search and internal event rail would show a customer the inside
        of a warehouse they have no business seeing. Staff who follow a link
        into it get a way back, but not the chrome.
      */}
      <Route path="/track/:ref" element={<Track />} />
      <Route path="*" element={user ? <Shell /> : <Login />} />
    </Routes>
  );
}

function Shell() {
  const { user } = useAuth();
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
        // v7: an outbound order goes to the public tracker for the same reason a
        // trailer does -- it is the "where is my delivery" view, and it is the
        // one a customer would be quoting the reference from.
        outbound_order: `/track/${hit.entity_id}`,
      };
      navigate(routes[hit.entity_type] ?? "/");
      setQuery("");
    } catch (err) {
      setSearchError(err instanceof Error ? err.message : "Search failed");
    }
  }

  const connection = {
    live: { tone: "success" as const, dot: "bg-success", label: "Live" },
    connecting: { tone: "warning" as const, dot: "bg-warning animate-pulse", label: "Connecting" },
    offline: { tone: "danger" as const, dot: "bg-error", label: "Offline" },
  }[state];

  return (
    <div className="flex h-full">
      {/* Disruptions push themselves at the operator from any screen, off the
          one socket this Shell already holds. */}
      <EventToaster events={events} />

      {/* ---- sidebar (fixed 240px per DESIGN.md layout model) ---- */}
      <aside className="hidden md:flex w-[260px] shrink-0 flex-col border-r border-outline-variant/60 bg-surface-container-low">
        <div className="px-5 py-5">
          <div className="flex items-center gap-3">
            <div className="grid h-10 w-10 place-items-center rounded-lg bg-primary-container text-on-primary font-bold">
              CS
            </div>
            <div>
              <h1 className="text-headline-md text-primary leading-tight">CogniSupply P2P</h1>
              <p className="text-body-sm text-on-surface-variant">Enterprise Control Tower</p>
            </div>
          </div>
        </div>

        <nav className="flex flex-col gap-1 px-3">
          {navForRole(user?.role ?? "").map((item) => (
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

        <div className="mt-auto border-t border-outline-variant/60 px-3 py-3">
          <div className="px-2 pb-3 text-body-sm text-on-surface-variant">
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${connection.dot}`} />
              <span>Live data feed: {connection.label}</span>
            </div>
            <div className="mt-1 flex items-center gap-2">
              <Icon name="sync" className="!text-[16px]" />
              <span>Last event: {lastEventAt ? ago(lastEventAt.toISOString()) : "—"}</span>
            </div>
          </div>
          <UserMenu />
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
              placeholder="Search by ID (PO-, INV-, TRL-, SHP-, OBO-, tracking no.)"
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
            {user && (
              <span
                className="badge bg-[#e2dfff] text-primary"
                title={`Permissions: ${user.permissions.join(", ") || "read-only"}`}
              >
                <Icon name="badge" className="!text-[14px]" />
                {ROLE_LABEL[user.role] ?? user.role}
              </span>
            )}
            <Badge tone={connection.tone}>{connection.label}</Badge>
          </div>
        </header>

        <div className="flex min-h-0 flex-1">
          <main className="min-w-0 flex-1 overflow-auto p-6">
            <Routes>
              <Route path="/" element={<ControlTower events={events} />} />
              <Route path="/yard" element={<YardDock events={events} />} />
              <Route path="/outbound" element={<Outbound events={events} />} />
              <Route path="/procurement" element={<Procurement />} />
              <Route path="/match-pay" element={<MatchPay />} />
              <Route path="/match-pay/:invoiceId" element={<MatchPay />} />
              <Route path="/exceptions" element={<Exceptions events={events} />} />
              <Route path="/traceability" element={<Traceability />} />
              <Route path="/traceability/:poId" element={<Traceability />} />
            </Routes>
          </main>

          {/* ---- live event rail ---- */}
          <aside className="hidden xl:flex w-[340px] shrink-0 flex-col border-l border-outline-variant/60 bg-surface-container-lowest">
            <header className="flex items-center justify-between border-b border-outline-variant/60 px-4 py-3">
              <h2 className="flex items-center gap-2 text-body-lg font-semibold">
                <Icon name="bolt" className="text-primary" />
                Real-Time Event Stream
              </h2>
              <span className={`h-2 w-2 rounded-full ${connection.dot}`} />
            </header>
            <div className="flex-1 overflow-auto">
              {events.length === 0 ? (
                <p className="px-4 py-6 text-body-sm text-on-surface-variant">
                  No activity yet. Events appear here the moment any transaction moves
                  through the platform.
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
