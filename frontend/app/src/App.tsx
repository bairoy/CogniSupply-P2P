import { FormEvent, useEffect, useRef, useState } from "react";
import { NavLink, Route, Routes, useNavigate, Link } from "react-router-dom";
import { api, Overview, SIMULATOR, SimStatus } from "./api";
import { PERM, ROLE_LABEL, useAuth } from "./auth";
import EventToaster from "./components/EventToaster";
import { Badge, Icon, Spinner, ago } from "./components/ui";
import {
  LiveEvent,
  TelemetryPulse,
  useEventStream,
  useRefetchOn,
} from "./hooks/useEventStream";
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

function enrichEvent(e: LiveEvent, payload: Record<string, unknown>) {
  const type = e.event_type;
  let title = type.replace(/_/g, " ");
  let icon = "info";
  let tone = "text-on-surface-variant";
  let bg = "bg-surface-container-lowest hover:bg-surface-container-low";
  let link = undefined;
  let impact = null;

  if (e.entity_type === "trailer") {
    link = `/track/${e.entity_id}`;
    if (type === "TRAILER_ARRIVED") { title = "Vehicle Arrived"; icon = "local_shipping"; tone = "text-info"; }
    else if (type === "DOCK_ASSIGNED") { title = "Dock Assigned"; icon = "login"; tone = "text-primary"; }
    else if (type === "TRAILER_DOCKED") { title = "Vehicle Docked"; icon = "warehouse"; tone = "text-success"; }
    else if (type === "DOCK_DELAYED") { title = "Dock Delayed"; icon = "hourglass_top"; tone = "text-warning"; bg = "bg-warning-container/20 hover:bg-warning-container/40"; impact = `${payload.wait_minutes} min wait`; }
    else if (type === "ETA_UPDATED") { title = "ETA Updated"; icon = "schedule"; tone = "text-warning"; if (payload.direction === "later") { bg = "bg-warning-container/20 hover:bg-warning-container/40"; impact = `${Math.round(Number(payload.delta_minutes) ?? 0)} min late`; } }
    else if (type === "TRAILER_DEPARTED" || type === "TRAILER_EXITED") { title = "Vehicle Departed"; icon = "logout"; tone = "text-on-surface-variant"; }
  } else if (e.entity_type === "purchase_order") {
    link = `/traceability/${e.entity_id}`;
    if (type === "PO_ISSUED") { title = "PO Issued"; icon = "shopping_cart"; tone = "text-primary"; }
    else if (type === "GOODS_RECEIVED") { title = "Goods Received"; icon = "inventory_2"; tone = "text-success"; }
    else if (type === "REQUISITION_CONVERTED") { title = "Req Converted"; icon = "description"; tone = "text-info"; }
  } else if (e.entity_type === "invoice") {
    link = `/match-pay/${e.entity_id}`;
    if (type === "INVOICE_RECEIVED") { title = "Invoice Received"; icon = "receipt"; tone = "text-info"; }
    else if (type === "MATCH_COMPLETED") { title = "3-Way Match Passed"; icon = "verified"; tone = "text-success"; bg = "bg-success-container/20 hover:bg-success-container/40"; }
    else if (type === "PAYMENT_SCHEDULED") { title = "Payment Scheduled"; icon = "payments"; tone = "text-success"; }
  } else if (e.entity_type === "exception") {
    link = `/exceptions`;
    if (type === "EXCEPTION_CREATED") {
      title = "Discrepancy Detected";
      icon = "error";
      tone = "text-error";
      bg = "bg-error-container/20 hover:bg-error-container/40";
      if (payload.variance) impact = `₹${Math.abs(Number(payload.variance)).toLocaleString()}`;
    } else if (type === "EXCEPTION_RESOLVED") {
      title = "Exception Resolved";
      icon = "check_circle";
      tone = "text-success";
    }
  }

  // Fallbacks
  if (icon === "info" && tone === "text-on-surface-variant") {
    if (type.includes("EXCEPTION") || type.includes("DELAYED") || type.includes("CONFLICT")) { tone = "text-error"; icon = "error"; bg = "bg-error-container/20 hover:bg-error-container/40"; }
    else if (type.includes("MATCH") || type.includes("PAYMENT") || type.includes("RECEIVED")) { tone = "text-success"; icon = "check_circle"; }
    else if (type.includes("DOCK")) { tone = "text-primary"; icon = "warehouse"; }
  }

  return { title, icon, tone, bg, link, impact };
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

/**
 * Start and pause the inbound WMS / telematics feed.
 *
 * In this build that feed is the simulator: it drives trucks, arrivals,
 * unloads, invoices and payments by calling the same public endpoints a real
 * WMS would, so nothing downstream can tell the difference. Swap in a customer
 * WMS and this control is the one thing that goes away.
 *
 * It exists because the feed boots PAUSED (SimState.running = False) and
 * nothing flips it. Every service reports healthy, the socket says "Live", and
 * the screen sits perfectly still — indistinguishable from a broken system at a
 * glance. That is a demo failure with no bug behind it, and it should not
 * require a terminal to avoid.
 *
 * Pausing is worth having in its own right: the simulator unwinds nothing on
 * stop (see its /sim/stop docstring), so a run can be frozen mid-story while
 * somebody asks a question, then resumed.
 */
function WmsFeedControl() {
  const { can } = useAuth();
  const [status, setStatus] = useState<SimStatus | null>(null);
  const [reachable, setReachable] = useState(true);
  const [busy, setBusy] = useState(false);

  const read = () =>
    api
      .simulator<SimStatus>("/sim/status")
      .then((s) => {
        setStatus(s);
        setReachable(true);
      })
      .catch(() => setReachable(false));

  useEffect(() => {
    read();
    // Polled, not pushed: the feed's own liveness is exactly the thing that
    // cannot be inferred from the event stream, because a stopped feed emits
    // nothing at all. It also keeps two open browsers agreeing about the state.
    const timer = window.setInterval(read, 5000);
    return () => window.clearInterval(timer);
  }, []);

  async function toggle() {
    if (!status || busy) return;
    setBusy(true);
    try {
      const next = await api.post<SimStatus>(
        SIMULATOR,
        status.running ? "/sim/stop" : "/sim/start",
      );
      setStatus(next);
    } catch {
      /* leave the last known state on screen; the poll will correct it */
    } finally {
      setBusy(false);
    }
  }

  // A missing simulator is a legitimate deployment (a real WMS feeding us), not
  // an error worth shouting about — so this simply disappears.
  if (!reachable) return null;

  const running = status?.running ?? false;
  const allowed = can(PERM.yardWrite);

  return (
    <div className="mb-2 rounded-lg border border-outline-variant/60 bg-surface-container-lowest px-3 py-2.5">
      <div className="flex items-center gap-2">
        <span className="relative flex h-2 w-2 shrink-0">
          {running && (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60" />
          )}
          <span
            className={`relative inline-flex h-2 w-2 rounded-full ${running ? "bg-success" : "bg-outline"}`}
          />
        </span>
        <span className="text-body-sm font-semibold text-on-surface">WMS Feed</span>
        <span
          className={`ml-auto text-body-sm font-medium ${running ? "text-success" : "text-on-surface-variant"}`}
        >
          {running ? "Running" : "Paused"}
        </span>
      </div>

      <p className="mt-1 text-body-sm text-outline">
        {running
          ? `${status?.ticks.toLocaleString() ?? 0} ticks · every ${status?.tick_seconds ?? 3}s`
          : "Inbound telemetry is not being received."}
      </p>

      {allowed ? (
        <button
          type="button"
          onClick={toggle}
          disabled={busy || !status}
          className={`mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg px-2 py-1.5 text-body-sm font-semibold transition-colors disabled:opacity-50 ${
            running
              ? "border border-outline-variant/60 text-on-surface-variant hover:bg-surface-container-high"
              : "bg-primary text-on-primary hover:opacity-90"
          }`}
        >
          <Icon name={running ? "pause" : "play_arrow"} className="!text-[16px]" />
          {busy ? "Working…" : running ? "Pause feed" : "Start feed"}
        </button>
      ) : (
        <p className="mt-1.5 text-body-sm text-outline">
          Operator access required to control the feed.
        </p>
      )}
    </div>
  );
}

/**
 * The GPS feed, as one line instead of hundreds.
 *
 * Reads as an instrument rather than a log entry, because that is what it is:
 * position is a state being sampled, not a sequence of things that happened.
 * Silent until the first ping, so it never claims a fleet that is not moving.
 */
function TelemetryPulseRow({ pulse }: { pulse: TelemetryPulse }) {
  if (!pulse.lastAt) return null;
  // A ping is expected every few seconds per vehicle. Nothing for a minute
  // means the GPS feed has stopped, which is worth showing amber rather than
  // leaving a stale "live" reading on screen.
  const stale = Date.now() - pulse.lastAt.getTime() > 60_000;
  return (
    <div className="flex items-center gap-2 border-b border-outline-variant/60 bg-surface-container-low px-4 py-2">
      <span className="relative flex h-2 w-2 shrink-0">
        {!stale && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-60" />
        )}
        <span
          className={`relative inline-flex h-2 w-2 rounded-full ${stale ? "bg-warning" : "bg-primary"}`}
        />
      </span>
      <Icon name="my_location" className="!text-[15px] text-outline" />
      <span className="text-body-sm text-on-surface-variant">
        GPS ·{" "}
        <span className="font-semibold text-on-surface">
          {pulse.reporting} {pulse.reporting === 1 ? "vehicle" : "vehicles"}
        </span>{" "}
        reporting
      </span>
      <span className="mono ml-auto shrink-0 text-[11px] text-outline">
        {pulse.total.toLocaleString()} · {ago(pulse.lastAt.toISOString())}
      </span>
    </div>
  );
}

function Shell() {
  const { user } = useAuth();
  const { events, state, lastEventAt, pulse } = useEventStream();
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
          <WmsFeedControl />
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

            {/* GPS telemetry is deliberately not in the list below: a single
                driving truck emits a ping every few seconds and would fill the
                60-slot buffer on its own, burying the events that matter. The
                pings do carry one real signal though -- the fleet is reporting
                -- so they are counted here instead of listed there. */}
            <TelemetryPulseRow pulse={pulse} />

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
                    const { title, icon, tone, bg, link, impact } = enrichEvent(e, payload);
                    const Inner = () => (
                      <>
                        <div className="flex items-center justify-between gap-2">
                          <div className={`flex items-center gap-1.5 font-semibold ${tone}`}>
                            <Icon name={icon} className="!text-[16px]" />
                            <span className="text-body-md leading-none">{title}</span>
                          </div>
                          <span className="mono shrink-0 text-[11px] text-outline">
                            {new Date(e.timestamp).toLocaleTimeString([], {
                              hour: "2-digit",
                              minute: "2-digit",
                            })}
                          </span>
                        </div>
                        <p className="mt-1 text-body-sm text-on-surface-variant line-clamp-2">
                          {(payload.summary as string) ?? `${e.entity_type} ${e.entity_id}`}
                        </p>
                        {impact && (
                          <div className={`mt-2 inline-block rounded px-1.5 py-0.5 text-body-sm font-semibold border border-current ${tone}`}>
                            {impact}
                          </div>
                        )}
                      </>
                    );
                    
                    return (
                      <li key={e.event_id} className={`border-b border-outline-variant/40 transition-colors ${bg}`}>
                        {link ? (
                          <Link to={link} className="block px-4 py-3 h-full w-full">
                            <Inner />
                          </Link>
                        ) : (
                          <div className="px-4 py-3">
                            <Inner />
                          </div>
                        )}
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
