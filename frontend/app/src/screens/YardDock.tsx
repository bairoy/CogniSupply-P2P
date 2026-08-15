import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Dock,
  DockLane,
  DockSchedule,
  ScoreBreakdown,
  Trailer,
  YardStatus,
  api,
  YARD,
} from "../api";
import { PERM, useAuth } from "../auth";
import {
  Badge,
  Empty,
  ErrorNote,
  Icon,
  KpiTile,
  Panel,
  Spinner,
  Tone,
  clock,
  statusTone,
} from "../components/ui";
import { LiveEvent, useRefetchOn } from "../hooks/useEventStream";

const STATE_STYLE: Record<Dock["state"], { box: string; tone: Tone; label: string }> = {
  EMPTY: { box: "bg-surface-container-low border-outline-variant/60", tone: "neutral", label: "Empty" },
  RESERVED: { box: "bg-info-container/60 border-info/30", tone: "info", label: "Reserved" },
  UNLOADING: { box: "bg-success-container/70 border-success/30", tone: "success", label: "Unloading" },
  // v7. Same occupancy as UNLOADING, opposite direction -- given its own colour
  // because "DOCK-07 busy" is not actionable and "DOCK-07 loading" is.
  LOADING: { box: "bg-[#e2dfff] border-primary-container/50", tone: "primary", label: "Loading" },
  BLOCKED: { box: "bg-error-container/60 border-error/30", tone: "danger", label: "Blocked" },
};

/** Events that change which door is free when, and therefore the plan. */
const SCHEDULE_EVENTS = [
  "DOCK_ASSIGNED",
  "DOCK_REASSIGNED",
  "DOCK_DELAYED",
  "TRAILER_ARRIVED",
  "TRAILER_DOCKED",
  "TRAILER_DEPARTED",
  "TRAILER_EXITED",
  "GOODS_RECEIVED",
  "GOODS_ISSUED",   // v7 -- the outbound dock-release signal
  "ETA_UPDATED",
];

const SCHEDULE_HOURS = 8;

/**
 * What the dock scanner reports, and therefore what the goods receipt is
 * posted with. One constant so the number on the screen and the number in the
 * POST body can never drift apart -- a receipt that disagrees with the count
 * the operator watched being taken is exactly the kind of quiet lie the 3-way
 * match then has to argue with.
 */
const SCANNED_QTY = 500;

const AXIS_LEAD_MINUTES = 30; // show a little of the recent past, so in-progress
                              // unloads are visible rather than clipped at zero

function minutes(value?: number | null) {
  if (value === null || value === undefined) return "—";
  if (value < 60) return `${value}m`;
  return `${Math.floor(value / 60)}h ${value % 60}m`;
}

function priorityTone(priority?: string | null): Tone {
  if (priority === "critical") return "danger";
  if (priority === "high") return "warning";
  return "neutral";
}

export default function YardDock({ events }: { events: LiveEvent[] }) {
  const [yard, setYard] = useState<YardStatus | null>(null);
  const [schedule, setSchedule] = useState<DockSchedule | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ScoreBreakdown | null>(null);
  const [detailReason, setDetailReason] = useState<string | null>(null);
  const { can } = useAuth();
  const [busy, setBusy] = useState<string | null>(null);
  /** Which vehicle the dock scanner is pointed at. */
  const [scanTarget, setScanTarget] = useState<string | null>(null);
  const scannerRef = useRef<HTMLDivElement>(null);

  const load = useCallback(() => {
    Promise.all([
      api.yard<YardStatus>("/yard-status"),
      api.yard<DockSchedule>(`/dock-schedule?hours=${SCHEDULE_HOURS}`),
    ])
      .then(([status, plan]) => {
        setYard(status);
        setSchedule(plan);
        setError(null);
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(load, [load]);
  useRefetchOn(events, SCHEDULE_EVENTS, load);

  /** Pull the decision behind the selected trailer's current assignment. */
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setDetailReason(null);
      return;
    }
    api
      .yard<{
        dock_assignment_history: {
          status: string;
          reason: string | null;
          score_breakdown: ScoreBreakdown | null;
        }[];
      }>(`/trailers/${selected}`)
      .then((d) => {
        const current =
          d.dock_assignment_history.find((a) => ["ASSIGNED", "CONFIRMED"].includes(a.status)) ??
          d.dock_assignment_history.at(-1);
        setDetail(current?.score_breakdown ?? null);
        setDetailReason(current?.reason ?? null);
      })
      .catch(() => setDetail(null));
  }, [selected]);

  async function act(trailerId: string, action: "arrive" | "dock" | "unload" | "depart") {
    setBusy(trailerId);
    try {
      const body = action === "unload" ? { qty_received: SCANNED_QTY } : undefined;
      await api.post(YARD, `/trailers/${trailerId}/${action}`, body);
      load();
    } catch (e) {
      alert(e instanceof Error ? e.message : "Yard action failed");
    } finally {
      setBusy(null);
    }
  }

  /**
   * The scanner's unload. Same endpoint, same body as the register's action --
   * it just runs after the vision pass instead of instead of it. Errors are
   * thrown on so the widget can show the failure inside the readout rather
   * than in a browser alert over the top of it.
   */
  async function unloadScanned(trailerId: string) {
    await api.post(YARD, `/trailers/${trailerId}/unload`, { qty_received: SCANNED_QTY });
    load();
  }

  /** Point the scanner at a vehicle and bring it into view. */
  function aimScanner(trailerId: string) {
    setScanTarget(trailerId);
    setSelected(trailerId);
    window.requestAnimationFrame(() =>
      scannerRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }),
    );
  }

  if (error) return <ErrorNote error={error} />;
  if (!yard || !schedule) return <Spinner label="Loading yard operations" />;

  const summary = yard.summary;
  const counts = yard.docks.reduce<Record<string, number>>((acc, d) => {
    acc[d.state] = (acc[d.state] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-display">Logistics Command Center</h1>
        <p className="text-body-lg text-on-surface-variant">
          Automated dock scheduling, yard occupancy and gate movements in one view,
          with the decision behind every door assignment. Inbound and outbound
          vehicles are planned jointly against a single set of dock doors.
        </p>
      </header>

      {/* ---- movement in both directions, at a glance ---- */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-6">
        <KpiTile
          label="Inbound"
          value={summary.inbound}
          icon="local_shipping"
          sub={
            <span className="text-body-sm text-on-surface-variant">
              {summary.unassigned > 0 ? `${summary.unassigned} awaiting slot` : "all slotted"}
            </span>
          }
        />
        <KpiTile
          label="Outbound"
          value={summary.outbound_en_route ?? 0}
          icon="outbound"
          tone="primary"
          sub={
            <span className="text-body-sm text-on-surface-variant">
              {summary.outbound_at_door ?? 0} at a door
            </span>
          }
        />
        <KpiTile
          label="In yard, awaiting door"
          value={summary.in_yard_waiting}
          icon="hourglass_top"
          tone={summary.longest_wait_minutes > 45 ? "warning" : "neutral"}
          sub={
            <span className="text-body-sm text-on-surface-variant">
              longest {minutes(summary.longest_wait_minutes)}
            </span>
          }
        />
        <KpiTile label="At a door" value={summary.at_door} icon="dock" tone="success" />
        <KpiTile
          label="Awaiting gate-out"
          value={summary.awaiting_exit}
          icon="logout"
          sub={<span className="text-body-sm text-on-surface-variant">unloaded, in yard</span>}
        />
        <KpiTile
          label="Dock occupancy"
          value={`${summary.dock_occupancy_pct}%`}
          icon="donut_large"
          tone="info"
          progress={summary.dock_occupancy_pct}
          sub={
            <span className="text-body-sm text-on-surface-variant">
              {summary.docks_busy}/{summary.docks_active} doors
            </span>
          }
        />
      </div>

      {/* ---- the door schedule: what each door is doing, and when ---- */}
      <ScheduleTimeline
        schedule={schedule}
        selected={selected}
        onSelect={setSelected}
      />

      <div className="grid gap-6 xl:grid-cols-[1fr_400px]">
        <Panel
          title="Live Dock Door Status"
          icon="grid_view"
          action={
            <div className="flex flex-wrap gap-2">
              {(Object.keys(STATE_STYLE) as Dock["state"][]).map((s) => (
                <Badge key={s} tone={STATE_STYLE[s].tone}>
                  {STATE_STYLE[s].label} ({counts[s] ?? 0})
                </Badge>
              ))}
            </div>
          }
        >
          <div className="grid grid-cols-2 gap-3 p-5 sm:grid-cols-4 lg:grid-cols-7">
            {yard.docks.map((d) => {
              const style = STATE_STYLE[d.state];
              const isSelected = d.current_trailer_id === selected;
              return (
                <button
                  key={d.id}
                  onClick={() => setSelected(d.current_trailer_id ?? d.next_trailer_id ?? null)}
                  disabled={!d.current_trailer_id && !d.next_trailer_id}
                  title={d.assignment_reason ?? d.compatible_load_types.join(", ")}
                  className={`flex min-h-[130px] flex-col items-center justify-center gap-1.5 rounded-lg border p-3 text-center transition
                    ${style.box}
                    ${d.current_trailer_id || d.next_trailer_id ? "cursor-pointer hover:ring-2 hover:ring-primary-container/40" : "cursor-default"}
                    ${isSelected ? "ring-2 ring-primary-container" : ""}`}
                >
                  <span className="text-body-md font-semibold">{d.id.replace("DOCK-", "D-")}</span>
                  <Icon
                    name={
                      d.state === "BLOCKED"
                        ? "block"
                        : d.state === "UNLOADING"
                          ? "downloading"
                          : d.state === "RESERVED"
                            ? "local_shipping"
                            : "dock"
                    }
                    className="text-on-surface-variant"
                  />
                  {d.current_trailer_id ? (
                    <span className="mono text-primary">{d.current_trailer_id}</span>
                  ) : (
                    <span className="text-body-sm text-outline">{style.label}</span>
                  )}
                  {d.unload_progress_pct !== null && (
                    <>
                      <div className="h-1 w-full overflow-hidden rounded-full bg-surface-container-highest">
                        <div
                          className="h-full rounded-full bg-success"
                          style={{ width: `${d.unload_progress_pct}%` }}
                        />
                      </div>
                      <span className="text-body-sm tnum text-success">
                        {d.unload_progress_pct}%
                      </span>
                    </>
                  )}
                  {/* What is coming next matters as much as what is there now --
                      it is the difference between a board and a schedule. */}
                  {!d.current_trailer_id && d.next_trailer_id && (
                    <span className="text-body-sm text-on-surface-variant">
                      next {clock(d.next_start)}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </Panel>

        <DecisionPanel trailerId={selected} detail={detail} reason={detailReason} />
      </div>

      {/* ---- simulated dock vision: the count behind the goods receipt ---- */}
      <div ref={scannerRef}>
        <IoTScannerSim
          docked={yard.trailers.filter((t) => t.status === "DOCKED")}
          docks={yard.docks}
          target={scanTarget}
          onTarget={setScanTarget}
          onUnload={unloadScanned}
          canWrite={can(PERM.yardWrite)}
        />
      </div>

      {/* ---- active trailers ---- */}
      <Panel title="Active Vehicle Register" icon="local_shipping">
        {yard.trailers.length === 0 ? (
          <Empty message="No vehicles on site." />
        ) : (
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className="th">Vehicle</th>
                <th className="th">Carrier</th>
                <th className="th">Status</th>
                <th className="th">Priority</th>
                <th className="th">Dock door</th>
                <th className="th">ETA</th>
                <th className="th">Scheduled slot</th>
                <th className="th">Dwell time</th>
                <th className="th">Action</th>
              </tr>
            </thead>
            <tbody>
              {yard.trailers.map((t: Trailer) => (
                <tr
                  key={t.id}
                  className={`hover:bg-surface-container-low ${selected === t.id ? "bg-surface-container-low" : ""}`}
                >
                  <td className="td">
                    <Link
                      to={`/track/${t.id}`}
                      className="mono font-semibold text-primary hover:underline"
                    >
                      {t.id}
                    </Link>
                    {/* v7. The arrow is doing real work on this board: an
                        operator scanning the queue needs to know which way each
                        truck is pointing, because it changes what happens at the
                        door -- and the two are interleaved by design. */}
                    <span
                      className="ml-1.5 align-middle"
                      title={t.direction === "OUTBOUND" ? "Outbound — collecting" : "Inbound — delivering"}
                    >
                      <Icon
                        name={t.direction === "OUTBOUND" ? "arrow_outward" : "arrow_downward"}
                        className={`!text-[14px] ${t.direction === "OUTBOUND" ? "text-primary" : "text-on-surface-variant"}`}
                      />
                    </span>
                    <div className="text-body-sm text-on-surface-variant">{t.load_type}</div>
                  </td>
                  <td className="td text-on-surface-variant">
                    {t.carrier ?? "—"}
                    {t.customer_name && (
                      <div className="text-body-sm text-primary">{t.customer_name}</div>
                    )}
                  </td>
                  <td className="td">
                    <Badge tone={statusTone(t.status)}>{t.status.replace("_", " ")}</Badge>
                  </td>
                  <td className="td">
                    <Badge tone={priorityTone(t.priority)}>{t.priority}</Badge>
                  </td>
                  <td className="td">
                    {t.dock_assignment ? (
                      <button
                        onClick={() => setSelected(t.id)}
                        className="mono font-semibold text-primary hover:underline"
                      >
                        {t.dock_assignment.dock_id}
                      </button>
                    ) : (
                      <span className="text-body-sm italic text-outline">Awaiting slot</span>
                    )}
                  </td>
                  <td className="td tnum text-on-surface-variant">{clock(t.eta)}</td>
                  <td className="td tnum text-on-surface-variant">
                    {t.dock_assignment?.planned_start ? (
                      <>
                        {clock(t.dock_assignment.planned_start)}–
                        {clock(t.dock_assignment.planned_end)}
                        {!!t.dock_assignment.planned_wait_minutes && (
                          <span className="ml-1 text-warning">
                            +{t.dock_assignment.planned_wait_minutes}m
                          </span>
                        )}
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="td tnum">
                    {t.waiting_minutes === null || t.waiting_minutes === undefined ? (
                      <span className="text-outline">—</span>
                    ) : (
                      <span className={t.waiting_minutes >= 45 ? "text-error" : "text-on-surface-variant"}>
                        {minutes(t.waiting_minutes)}
                      </span>
                    )}
                  </td>
                  <td className="td">
                    {/* Yard actions are operator/admin only. A row for other
                        roles shows the state without the levers, rather than
                        buttons that would come back 403. */}
                    <div className="flex gap-2">
                      {!can(PERM.yardWrite) && (
                        <span className="text-body-sm italic text-outline">Read-only</span>
                      )}
                      {can(PERM.yardWrite) && t.status === "EN_ROUTE" && (
                        <button
                          className="btn-secondary !py-1 !px-2 !text-body-sm"
                          disabled={busy === t.id}
                          onClick={() => act(t.id, "arrive")}
                        >
                          Gate in
                        </button>
                      )}
                      {can(PERM.yardWrite) && t.status === "ARRIVED" && (
                        <button
                          className="btn-primary !py-1 !px-2 !text-body-sm"
                          disabled={busy === t.id || !t.dock_assignment}
                          onClick={() => act(t.id, "dock")}
                        >
                          Berth at door
                        </button>
                      )}
                      {/* Unloading now runs through the dock scanner: the GRN
                          quantity should come from a count of what was
                          physically on the trailer, not from a button that
                          asserts it. Same endpoint, one step earlier. */}
                      {can(PERM.yardWrite) && t.status === "DOCKED" && (
                        <button
                          className="btn-primary !py-1 !px-2 !text-body-sm"
                          disabled={busy === t.id}
                          onClick={() => aimScanner(t.id)}
                        >
                          <Icon name="qr_code_scanner" className="!text-[16px]" />
                          Scan &amp; post GRN
                        </button>
                      )}
                      {can(PERM.yardWrite) && t.status === "UNLOADED" && (
                        <button
                          className="btn-secondary !py-1 !px-2 !text-body-sm"
                          disabled={busy === t.id}
                          onClick={() => act(t.id, "depart")}
                        >
                          Gate out
                        </button>
                      )}
                    </div>
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

/* ─────────────────────────────────────────────
   Simulated dock IoT / computer vision
   ───────────────────────────────────────────── */

type ScanPhase = "idle" | "scanning" | "scanned" | "posting" | "posted" | "failed";

const SCAN_DURATION_MS = 3000;
const PALLETS = 6;

/**
 * Deterministic per-pallet detection confidence.
 *
 * Derived from the trailer id rather than Math.random() so the same vehicle
 * reads the same on every pass -- a "camera" whose confidences reshuffle on
 * each render is obviously a toy, and the same seed also keeps re-renders from
 * jittering the numbers mid-scan.
 */
function confidences(trailerId: string): number[] {
  let seed = 0;
  for (let i = 0; i < trailerId.length; i += 1) seed = (seed * 31 + trailerId.charCodeAt(i)) % 9973;
  return Array.from({ length: PALLETS }, (_, i) => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return 0.93 + ((seed >> 7) % 70) / 1000 + i * 0.0001; // 0.93 – 0.999
  });
}

/**
 * Goods receipt by camera, not by clipboard.
 *
 * PR2 asks for the receiving step to be evidenced rather than asserted. This
 * stands in for the dock camera an installed system would have: it runs a
 * vision pass over the load, counts pallets, and only then posts the goods
 * receipt through the SAME `POST /trailers/{id}/unload` the register always
 * used. The scan is simulated -- it is labelled as such on the widget, because
 * a demo that quietly implies a camera it does not have is a demo that fails
 * its first question -- but everything downstream of it is real: the GRN row,
 * the GOODS_RECEIVED event, the 3-way match it unblocks.
 */
function IoTScannerSim({
  docked,
  docks,
  target,
  onTarget,
  onUnload,
  canWrite,
}: {
  docked: Trailer[];
  docks: Dock[];
  target: string | null;
  onTarget: (id: string | null) => void;
  onUnload: (trailerId: string) => Promise<void>;
  canWrite: boolean;
}) {
  const [phase, setPhase] = useState<ScanPhase>("idle");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<{ trailerId: string; qty: number } | null>(null);
  /** The vehicle a run is about, held for the run's duration. */
  const [subject, setSubject] = useState<Trailer | null>(null);
  /** …and the door it was at. The unload frees the door immediately, so this
      has to be remembered or the HUD reads "unassigned" over the result. */
  const [subjectDock, setSubjectDock] = useState<string | null>(null);
  const raf = useRef<number>();
  const timer = useRef<number>();

  // Idle: whatever the operator aimed at, else the first at a door, so the
  // widget is never a dead box while there is something to scan. Mid-run: the
  // vehicle the run is about. A successful unload takes the trailer out of the
  // DOCKED list within the same second, and without this hold the confirmation
  // would be replaced by the next truck's empty camera before it could be read.
  const candidate = docked.find((t) => t.id === target) ?? docked[0] ?? null;
  const active = phase === "idle" ? candidate : subject;
  const liveDock = active ? docks.find((d) => d.current_trailer_id === active.id) ?? null : null;
  const dockId = phase === "idle" ? liveDock?.id ?? null : subjectDock ?? liveDock?.id ?? null;

  function reset() {
    if (raf.current) cancelAnimationFrame(raf.current);
    if (timer.current) window.clearTimeout(timer.current);
    setPhase("idle");
    setProgress(0);
    setError(null);
    setReceipt(null);
    setSubject(null);
    setSubjectDock(null);
  }

  /* The yard moved on under an idle camera -- follow it. A run in progress is
     never interrupted by a refresh; see `active` above. */
  useEffect(() => {
    if (phase !== "idle") return;
    setProgress(0);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidate?.id]);

  /* The operator aimed somewhere else. That IS an instruction to drop the last
     result -- a 100% from the previous trailer over a new one would be the
     worst possible thing to show. */
  useEffect(() => {
    if (target && target !== subject?.id) reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  useEffect(
    () => () => {
      if (raf.current) cancelAnimationFrame(raf.current);
      if (timer.current) window.clearTimeout(timer.current);
    },
    [],
  );

  function runScan() {
    if (!candidate || phase !== "idle") return;
    const vehicle = candidate;
    setSubject(vehicle);
    setSubjectDock(docks.find((d) => d.current_trailer_id === vehicle.id)?.id ?? null);
    setError(null);
    setReceipt(null);
    setPhase("scanning");
    setProgress(0);

    const started = performance.now();
    const tick = (now: number) => {
      const elapsed = now - started;
      const value = Math.min(100, (elapsed / SCAN_DURATION_MS) * 100);
      setProgress(value);
      if (elapsed < SCAN_DURATION_MS) {
        raf.current = requestAnimationFrame(tick);
        return;
      }
      // 100% Scanned holds on screen for a beat before the write, so the
      // operator sees the count that is about to be committed.
      setProgress(100);
      setPhase("scanned");
      timer.current = window.setTimeout(async () => {
        setPhase("posting");
        try {
          await onUnload(vehicle.id);
          setReceipt({ trailerId: vehicle.id, qty: SCANNED_QTY });
          setPhase("posted");
        } catch (e) {
          setError(e instanceof Error ? e.message : "Goods receipt was rejected");
          setPhase("failed");
        }
      }, 700);
    };
    raf.current = requestAnimationFrame(tick);
  }

  const scanning = phase === "scanning";
  const detected = phase === "idle" ? 0 : Math.min(PALLETS, Math.ceil((progress / 100) * PALLETS));
  const conf = confidences(active?.id ?? "TRL-000");
  const avgConfidence = conf.slice(0, Math.max(detected, 1)).reduce((a, b) => a + b, 0) /
    Math.max(detected, 1);

  const statusLine: Record<ScanPhase, string> = {
    idle: active ? "Camera armed — awaiting operator trigger" : "No vehicle at a door",
    scanning: `Scanning load… ${progress.toFixed(0)}%`,
    scanned: "100% Scanned — committing goods receipt",
    posting: "Posting goods receipt…",
    posted: "Goods receipt posted from scan",
    failed: "Scan complete — goods receipt rejected",
  };

  return (
    <Panel
      title="Dock Vision — Automated Goods Receipt"
      icon="photo_camera"
      action={
        <div className="flex items-center gap-2">
          <Badge tone="neutral">Simulated sensor feed</Badge>
          {docked.length > 1 && (
            <select
              value={active?.id ?? ""}
              onChange={(e) => onTarget(e.target.value)}
              className="rounded-lg border border-outline-variant bg-surface-container-lowest px-2 py-1 text-body-sm outline-none focus:border-primary-container"
            >
              {docked.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.id}
                </option>
              ))}
            </select>
          )}
        </div>
      }
    >
      <div className="grid gap-5 p-5 lg:grid-cols-[1.1fr_1fr]">
        {/* ---- the "camera" ---- */}
        <div className="relative aspect-[16/9] overflow-hidden rounded-xl border border-outline-variant bg-inverse-surface">
          {/* HUD */}
          <div className="absolute inset-x-0 top-0 z-20 flex items-center justify-between px-3 py-2 text-[11px] font-semibold tracking-wide text-inverse-on-surface/80">
            <span className="flex items-center gap-1.5">
              <span
                className={`h-2 w-2 rounded-full bg-error ${active ? "scan-pulse" : "opacity-40"}`}
              />
              CAM-{(dockId ?? "DOCK-00").replace("DOCK-", "")} · {dockId ?? "unassigned door"}
            </span>
            <span className="mono">{active?.id ?? "NO SIGNAL"}</span>
          </div>

          {/* faint sensor grid */}
          <div
            className="absolute inset-0 opacity-[0.12]"
            style={{
              backgroundImage:
                "linear-gradient(#ebf1ff 1px, transparent 1px), linear-gradient(90deg, #ebf1ff 1px, transparent 1px)",
              backgroundSize: "28px 28px",
            }}
          />

          {/* pallets, with a bounding box appearing as each is recognised */}
          <div className="absolute inset-0 grid grid-cols-3 grid-rows-2 gap-2 p-8 pt-10">
            {Array.from({ length: PALLETS }, (_, i) => {
              const found = i < detected;
              return (
                <div key={i} className="relative grid place-items-center">
                  <Icon
                    name="pallet"
                    className={`!text-[40px] transition-colors duration-300 ${
                      found ? "text-success-container" : "text-inverse-on-surface/25"
                    }`}
                  />
                  {found && (
                    <span
                      key={`box-${i}-${phase}`}
                      className="scan-box absolute inset-1 rounded border-2 border-success-container/80"
                    >
                      <span className="absolute -top-[15px] left-0 rounded-sm bg-success-container px-1 text-[10px] font-bold text-success">
                        PLT-{String(i + 1).padStart(2, "0")} {(conf[i] * 100).toFixed(1)}%
                      </span>
                    </span>
                  )}
                </div>
              );
            })}
          </div>

          {/* the laser */}
          {scanning && (
            <div className="scan-sweep absolute inset-x-0 z-10 h-[2px] bg-[#4ade80] shadow-[0_0_14px_4px_rgba(74,222,128,0.6)]" />
          )}

          {/* corner brackets */}
          {(["left-2 top-8 border-l-2 border-t-2", "right-2 top-8 border-r-2 border-t-2",
             "left-2 bottom-2 border-l-2 border-b-2", "right-2 bottom-2 border-r-2 border-b-2"]
          ).map((c) => (
            <span key={c} className={`absolute h-5 w-5 border-success-container/70 ${c}`} />
          ))}

          {/* result stamp */}
          {(phase === "scanned" || phase === "posting" || phase === "posted") && (
            <div className="absolute inset-0 z-20 grid place-items-center bg-inverse-surface/80">
              <div className="scan-box rounded-lg border-2 border-success-container bg-inverse-surface/90 px-6 py-4 text-center">
                <p className="text-display leading-none text-success-container">100% Scanned</p>
                <p className="mono mt-1.5 text-inverse-on-surface">
                  {PALLETS} pallets · {SCANNED_QTY} units · {active?.id}
                </p>
                <p className="mt-1 text-body-sm text-inverse-on-surface/70">
                  {phase === "posted" ? "Goods receipt committed" : "Committing goods receipt…"}
                </p>
              </div>
            </div>
          )}

          {!active && (
            <div className="absolute inset-0 z-20 grid place-items-center">
              <p className="text-body-sm text-inverse-on-surface/70">
                No vehicle berthed — feed idle
              </p>
            </div>
          )}
        </div>

        {/* ---- readout ---- */}
        <div className="flex flex-col gap-4">
          <div>
            <span className="label">Vehicle under scan</span>
            <p className="mono text-body-lg text-primary">{active?.id ?? "—"}</p>
            <p className="text-body-sm text-on-surface-variant">
              {active
                ? `${active.load_type} · ${active.carrier ?? "carrier unknown"}${active.po_id ? ` · ${active.po_id}` : ""}`
                : "Berth a vehicle at a door to arm the camera."}
            </p>
          </div>

          <div>
            <div className="flex items-baseline justify-between">
              <span className="label">Scan progress</span>
              <span
                className={`tnum text-body-lg font-semibold ${progress >= 100 ? "text-success" : "text-primary"}`}
              >
                {progress.toFixed(0)}%
              </span>
            </div>
            <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-surface-container-high">
              <div
                className={`h-full rounded-full transition-[width] duration-100 ${progress >= 100 ? "bg-success" : "bg-primary-container"}`}
                style={{ width: `${progress}%` }}
              />
            </div>
            <p className="mt-1.5 text-body-sm text-on-surface-variant">{statusLine[phase]}</p>
          </div>

          <div className="grid grid-cols-3 gap-2">
            {[
              { label: "Pallets detected", value: `${detected}/${PALLETS}` },
              { label: "Units counted", value: phase === "idle" ? "—" : String(SCANNED_QTY) },
              {
                label: "Mean confidence",
                value: detected === 0 ? "—" : `${(avgConfidence * 100).toFixed(1)}%`,
              },
            ].map((f) => (
              <div key={f.label} className="rounded-lg bg-surface-container-low p-2.5 text-center">
                <span className="label">{f.label}</span>
                <p className="mt-0.5 text-body-lg font-semibold tnum">{f.value}</p>
              </div>
            ))}
          </div>

          {phase === "posted" && receipt && (
            <div className="rounded-lg border border-success/30 bg-success-container/60 p-3">
              <p className="flex items-center gap-2 text-body-md font-semibold text-success">
                <Icon name="check_circle" className="!text-[18px]" />
                Goods receipt posted for {receipt.trailerId}
              </p>
              <p className="mt-1 text-body-sm text-on-surface-variant">
                {receipt.qty} units written to the GRN and released to the 3-way match. The
                door is now free for the next booking.
              </p>
            </div>
          )}

          {phase === "failed" && error && (
            <div className="rounded-lg border border-error/30 bg-error-container/60 p-3">
              <p className="text-body-md font-semibold text-on-error-container">
                Goods receipt rejected
              </p>
              <p className="mt-1 text-body-sm text-on-error-container/90">{error}</p>
            </div>
          )}

          {canWrite ? (
            phase === "posted" || phase === "failed" ? (
              <button type="button" className="btn-secondary" onClick={reset}>
                <Icon name="restart_alt" />
                {docked.length > 0 ? "Scan the next vehicle" : "Reset camera"}
              </button>
            ) : (
              <button
                type="button"
                className="btn-primary"
                disabled={!candidate || phase !== "idle"}
                onClick={runScan}
              >
                <Icon name="qr_code_scanner" />
                {scanning
                  ? "Scanning…"
                  : phase === "scanned"
                    ? "100% scanned"
                    : phase === "posting"
                      ? "Posting GRN…"
                      : "Trigger unload scan"}
              </button>
            )
          ) : (
            <p className="text-body-sm italic text-outline">
              Read-only — yard operators and administrators can trigger a scan.
            </p>
          )}

          <p className="text-body-sm text-on-surface-variant">
            The vision pass is simulated for the demonstration. What follows it is not: the
            scan calls <span className="mono">POST /trailers/{"{id}"}/unload</span>, which is
            the only writer of <span className="mono">goods_receipts</span> in the platform.
          </p>
        </div>
      </div>
    </Panel>
  );
}

/**
 * The door schedule as a timeline.
 *
 * A yard board answers "what is happening now"; this answers "what is each
 * door doing for the rest of the shift", which is what dock-door availability
 * actually means and what makes a queue visible before it becomes a delay.
 */
function ScheduleTimeline({
  schedule,
  selected,
  onSelect,
}: {
  schedule: DockSchedule;
  selected: string | null;
  onSelect: (trailerId: string) => void;
}) {
  const axis = useMemo(() => {
    const now = new Date(schedule.generated_at).getTime();
    const start = now - AXIS_LEAD_MINUTES * 60_000;
    const span = (schedule.horizon_hours * 60 + AXIS_LEAD_MINUTES) * 60_000;
    const ticks: { label: string; left: number }[] = [];
    // One tick per hour, on the hour, so the axis reads like a clock.
    const first = new Date(start);
    first.setMinutes(0, 0, 0);
    for (let t = first.getTime(); t <= start + span; t += 3_600_000) {
      if (t < start) continue;
      ticks.push({
        label: new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
        left: ((t - start) / span) * 100,
      });
    }
    return { start, span, ticks, nowLeft: ((now - start) / span) * 100 };
  }, [schedule]);

  const place = (iso: string) => ((new Date(iso).getTime() - axis.start) / axis.span) * 100;

  const lanes = [...schedule.docks].sort((a, b) => a.yard_position - b.yard_position);

  return (
    <Panel
      title={`Automated Dock Scheduling — next ${schedule.horizon_hours}h`}
      icon="calendar_view_week"
      action={
        <div className="flex items-center gap-3 text-body-sm text-on-surface-variant">
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-4 rounded-sm bg-success" /> in service
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-4 rounded-sm bg-primary-container" /> slotted
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-4 rounded-sm bg-warning" /> queued
          </span>
          <Badge tone="info">{schedule.summary.utilisation_pct}% utilised</Badge>
        </div>
      }
    >
      <div className="overflow-x-auto">
        <div className="min-w-[720px] p-5">
          {/* hour axis */}
          <div className="relative mb-2 ml-[132px] h-5 border-b border-outline-variant/60">
            {axis.ticks.map((tick) => (
              <span
                key={tick.label}
                className="absolute -translate-x-1/2 text-body-sm tnum text-on-surface-variant"
                style={{ left: `${tick.left}%` }}
              >
                {tick.label}
              </span>
            ))}
          </div>

          <div className="flex flex-col gap-1.5">
            {lanes.map((lane) => (
              <Lane
                key={lane.id}
                lane={lane}
                axis={axis}
                place={place}
                selected={selected}
                onSelect={onSelect}
              />
            ))}
          </div>
        </div>
      </div>
    </Panel>
  );
}

function Lane({
  lane,
  axis,
  place,
  selected,
  onSelect,
}: {
  lane: DockLane;
  axis: { ticks: { label: string; left: number }[]; nowLeft: number };
  place: (iso: string) => number;
  selected: string | null;
  onSelect: (trailerId: string) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <div className="flex w-[124px] shrink-0 items-center justify-between gap-2">
        <span className={`mono text-body-sm ${lane.is_active ? "" : "text-outline line-through"}`}>
          {lane.id.replace("DOCK-", "D-")}
        </span>
        <span className="text-body-sm tnum text-on-surface-variant">
          {lane.is_active ? `${lane.utilisation_pct}%` : "out"}
        </span>
      </div>

      <div
        className={`relative h-8 flex-1 overflow-hidden rounded-md border ${
          lane.is_active
            ? "border-outline-variant/50 bg-surface-container-low"
            : "border-error/30 bg-error-container/30"
        }`}
      >
        {/* hour gridlines */}
        {axis.ticks.map((tick) => (
          <span
            key={tick.label}
            className="absolute inset-y-0 w-px bg-outline-variant/40"
            style={{ left: `${tick.left}%` }}
          />
        ))}
        {/* now */}
        <span
          className="absolute inset-y-0 z-10 w-0.5 bg-error/70"
          style={{ left: `${axis.nowLeft}%` }}
        />

        {lane.bookings.map((booking) => {
          const left = Math.max(0, place(booking.start));
          const right = Math.min(100, place(booking.end));
          if (right <= 0 || left >= 100) return null;
          const queued = (booking.wait_minutes ?? 0) > 0;
          const tone = booking.in_progress
            ? "bg-success text-on-success"
            : queued
              ? "bg-warning"
              : "bg-primary-container";
          return (
            <button
              key={booking.assignment_id}
              onClick={() => onSelect(booking.trailer_id)}
              title={
                `${booking.trailer_id} · ${booking.load_type ?? ""} · ${booking.priority ?? ""}\n` +
                `${clock(booking.start)}–${clock(booking.end)}` +
                (queued ? ` · waited ${booking.wait_minutes}m` : "") +
                (booking.source === "manual_override" ? "\noperator override (pinned)" : "") +
                (booking.reason ? `\n${booking.reason}` : "")
              }
              className={`absolute inset-y-1 flex items-center overflow-hidden rounded px-1.5 text-body-sm transition
                ${tone}
                ${selected === booking.trailer_id ? "ring-2 ring-primary" : "hover:brightness-95"}`}
              style={{ left: `${left}%`, width: `${Math.max(right - left, 1.2)}%` }}
            >
              <span className="mono truncate">{booking.trailer_id.replace("TRL-", "")}</span>
              {booking.source === "manual_override" && (
                <Icon name="push_pin" className="!text-[13px] ml-0.5 shrink-0" />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/**
 * Why this door, in the same arithmetic that chose it.
 *
 * Renders the v6 cost model when it is present, and falls back to the pre-v6
 * weighted score for history rows written by the old engine -- the board must
 * stay readable for assignments that predate the current logic rather than
 * showing them as blank.
 */
function DecisionPanel({
  trailerId,
  detail,
  reason,
}: {
  trailerId: string | null;
  detail: ScoreBreakdown | null;
  reason: string | null;
}) {
  if (!trailerId) {
    return (
      <Panel title="Dock Assignment Rationale" icon="account_tree">
        <Empty
          message="Select a dock door or a vehicle"
          hint="The optimiser's justification for its slot appears here."
        />
      </Panel>
    );
  }
  if (!detail) {
    return (
      <Panel title="Dock Assignment Rationale" icon="account_tree">
        <Spinner label="Loading rationale" />
      </Panel>
    );
  }

  const terms = detail.cost_terms;
  const isOverride = detail.source === "manual_override";
  const rows = terms
    ? [
        {
          label: `Waiting (${terms.wait?.minutes ?? 0} min × ${terms.wait?.weight_per_minute ?? 0}/min, ${terms.wait?.priority ?? "normal"})`,
          value: terms.wait?.cost ?? 0,
        },
        {
          label: `Flexibility (door handles ${terms.flexibility?.handles_load_types ?? 1})`,
          value: terms.flexibility?.cost ?? 0,
        },
        {
          label: `Yard position (${terms.position?.yard_position ?? "—"})`,
          value: terms.position?.cost ?? 0,
        },
        {
          label: `Utilisation (${terms.utilisation?.committed_minutes ?? 0} min booked)`,
          value: terms.utilisation?.cost ?? 0,
        },
        ...(terms.churn?.cost
          ? [{ label: `Move from ${terms.churn.moved_from}`, value: terms.churn.cost }]
          : []),
      ]
    : [];
  const worst = Math.max(1, ...rows.map((r) => r.value));

  return (
    <Panel
      title="Dock Assignment Rationale"
      icon="account_tree"
      action={
        detail.engine ? (
          <Badge tone={detail.engine === "cp-sat" ? "primary" : "neutral"}>
            {detail.engine === "cp-sat" ? "CP-SAT optimal" : detail.engine}
          </Badge>
        ) : isOverride ? (
          <Badge tone="warning">Operator override</Badge>
        ) : null
      }
    >
      <div className="flex flex-col gap-4 p-5">
        <div>
          <span className="label">Vehicle</span>
          <p className="mono text-body-lg text-primary">{trailerId}</p>
        </div>

        {(detail.planned_start || detail.wait_minutes !== undefined) && (
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-lg border border-outline-variant/60 bg-surface-container-low p-3">
              <span className="label">Service window</span>
              <p className="tnum text-body-lg">
                {clock(detail.planned_start)}–{clock(detail.planned_end)}
              </p>
            </div>
            <div className="rounded-lg border border-outline-variant/60 bg-surface-container-low p-3">
              <span className="label">Queue time</span>
              <p
                className={`tnum text-body-lg ${(detail.wait_minutes ?? 0) >= 45 ? "text-error" : "text-success"}`}
              >
                {minutes(detail.wait_minutes ?? 0)}
              </p>
            </div>
          </div>
        )}

        {isOverride && (
          <p className="rounded-lg border border-warning/30 bg-warning-container/50 p-3 text-body-sm">
            Manually overridden by an operator
            {detail.overridden_from ? ` (was ${detail.overridden_from})` : ""}. Pinned — the
            optimiser plans every other vehicle around it rather than reversing it.
          </p>
        )}

        <div>
          <span className="label">Feasibility checks</span>
          <ul className="mt-1.5 flex flex-col gap-1.5">
            {["Door in service", "Load type compatible", "Window free of conflicts"].map((text) => (
              <li key={text} className="flex items-center gap-2 text-body-sm">
                <Icon name="check_circle" className="!text-[18px] text-success" />
                {text}
              </li>
            ))}
            <li className="text-body-sm text-on-surface-variant">
              {String(detail.hard_constraints?.eligible_docks ?? "?")} eligible,{" "}
              {String(detail.hard_constraints?.rejected_docks ?? detail.rejected?.length ?? 0)}{" "}
              rejected
            </li>
          </ul>
        </div>

        {rows.length > 0 && (
          <div>
            <span className="label">Cost drivers for this door (lower is better)</span>
            <div className="mt-2 flex flex-col gap-2">
              {rows.map((row) => (
                <div key={row.label}>
                  <div className="flex justify-between gap-2 text-body-sm">
                    <span className="truncate">{row.label}</span>
                    <span className="tnum shrink-0 font-semibold">{row.value}</span>
                  </div>
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-container-high">
                    <div
                      className="h-full rounded-full bg-primary-container"
                      style={{ width: `${(row.value / worst) * 100}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Pre-v6 rows: show what that engine actually computed, not an empty box. */}
        {!terms && detail.final_score !== undefined && (
          <div className="rounded-lg border border-outline-variant/60 bg-surface-container-low p-3">
            <div className="flex items-baseline justify-between">
              <span className="label">Score (legacy engine)</span>
              <span className="text-headline-lg tnum text-primary">
                {detail.final_score.toFixed(2)}
              </span>
            </div>
            {detail.formula && <p className="mono mt-1 text-on-surface-variant">{detail.formula}</p>}
          </div>
        )}

        {detail.cost !== undefined && (
          <div className="rounded-lg border border-outline-variant/60 bg-surface-container-low p-3">
            <div className="flex items-baseline justify-between">
              <span className="label">Total cost</span>
              <span className="text-headline-lg tnum text-primary">{detail.cost}</span>
            </div>
            {detail.objective && (
              <p className="mono mt-1 text-body-sm text-on-surface-variant">{detail.objective}</p>
            )}
          </div>
        )}

        {reason && (
          <p className="text-body-sm text-primary">
            <span className="label block">Reason</span>
            {reason}
          </p>
        )}

        {!!detail.alternatives?.length && (
          <div>
            <span className="label">Alternatives evaluated</span>
            <table className="mt-1.5 w-full border-collapse text-body-sm">
              <thead>
                <tr className="text-on-surface-variant">
                  <th className="py-1 text-left font-normal">Door</th>
                  <th className="py-1 text-right font-normal">Wait</th>
                  <th className="py-1 text-right font-normal">Cost</th>
                </tr>
              </thead>
              <tbody>
                {detail.alternatives.slice(0, 5).map((alt) => (
                  <tr key={alt.dock_id}>
                    <td className="mono py-1">{alt.dock_id}</td>
                    <td className="tnum py-1 text-right text-on-surface-variant">
                      {alt.wait_minutes}m
                    </td>
                    <td className="tnum py-1 text-right">
                      {alt.cost}
                      <span className="ml-1 text-error">+{alt.delta_vs_chosen}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {!!detail.rejected?.length && (
          <details>
            <summary className="label cursor-pointer">
              Doors ruled out ({detail.rejected.length})
            </summary>
            <ul className="mt-1.5 flex flex-col gap-1">
              {detail.rejected.slice(0, 8).map((r) => (
                <li key={r.dock_id} className="flex gap-2 text-body-sm text-on-surface-variant">
                  <span className="mono shrink-0">{r.dock_id}</span>
                  <span className="truncate">{r.reason}</span>
                </li>
              ))}
            </ul>
          </details>
        )}

        {detail.plan && (
          <div className="rounded-lg border border-outline-variant/60 bg-surface-container-low p-3 text-body-sm">
            <span className="label">Optimisation summary</span>
            <p className="mt-1 text-on-surface-variant">
              {detail.plan.trailers_planned} vehicles scheduled jointly, total cost{" "}
              <span className="tnum font-semibold text-on-surface">{detail.plan.total_cost}</span>{" "}
              vs <span className="tnum">{detail.plan.greedy_baseline_cost}</span> for first-fit
              {(detail.plan.improvement_vs_greedy ?? 0) > 0 ? (
                <span className="text-success">
                  {" "}
                  — {detail.plan.improvement_vs_greedy} better
                </span>
              ) : (
                " — no gain available here"
              )}
              .
            </p>
          </div>
        )}
      </div>
    </Panel>
  );
}
