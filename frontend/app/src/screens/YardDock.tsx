import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Dock, ScoreBreakdown, Trailer, YardStatus, api, YARD } from "../api";
import { PERM, useAuth } from "../auth";
import {
  Badge,
  Empty,
  ErrorNote,
  Icon,
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
  BLOCKED: { box: "bg-error-container/60 border-error/30", tone: "danger", label: "Blocked" },
};

export default function YardDock({ events }: { events: LiveEvent[] }) {
  const [yard, setYard] = useState<YardStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ScoreBreakdown | null>(null);
  const [detailReason, setDetailReason] = useState<string | null>(null);
  const { can } = useAuth();
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .yard<YardStatus>("/yard-status")
      .then((d) => {
        setYard(d);
        setError(null);
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(load, [load]);
  useRefetchOn(
    events,
    ["DOCK_ASSIGNED", "DOCK_REASSIGNED", "TRAILER_ARRIVED", "TRAILER_DOCKED",
     "GOODS_RECEIVED", "TRAILER_DEPARTED", "ETA_UPDATED"],
    load,
  );

  /** Pull the score breakdown for the selected trailer's current assignment. */
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setDetailReason(null);
      return;
    }
    api
      .yard<{ dock_assignment_history: { status: string; reason: string | null; score_breakdown: ScoreBreakdown | null }[] }>(
        `/trailers/${selected}`,
      )
      .then((d) => {
        const current =
          d.dock_assignment_history.find((a) => ["ASSIGNED", "CONFIRMED"].includes(a.status)) ??
          d.dock_assignment_history.at(-1);
        setDetail(current?.score_breakdown ?? null);
        setDetailReason(current?.reason ?? null);
      })
      .catch(() => setDetail(null));
  }, [selected]);

  async function act(trailerId: string, action: "arrive" | "dock" | "unload") {
    setBusy(trailerId);
    try {
      const body =
        action === "unload"
          ? { qty_received: 500 }
          : undefined;
      await api.post(YARD, `/trailers/${trailerId}/${action}`, body);
      load();
    } catch (e) {
      alert(e instanceof Error ? e.message : "action failed");
    } finally {
      setBusy(null);
    }
  }

  if (error) return <ErrorNote error={error} />;
  if (!yard) return <Spinner label="Loading yard" />;

  const counts = yard.docks.reduce<Record<string, number>>((acc, d) => {
    acc[d.state] = (acc[d.state] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-display">Yard &amp; Dock</h1>
        <p className="text-body-lg text-on-surface-variant">
          Real-time dock occupancy and the decision behind every assignment.
        </p>
      </header>

      <div className="grid gap-6 xl:grid-cols-[1fr_360px]">
        <Panel
          title="Yard Board Live"
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
                  onClick={() => setSelected(d.current_trailer_id)}
                  disabled={!d.current_trailer_id}
                  title={d.assignment_reason ?? d.compatible_load_types.join(", ")}
                  className={`flex min-h-[124px] flex-col items-center justify-center gap-1.5 rounded-lg border p-3 text-center transition
                    ${style.box}
                    ${d.current_trailer_id ? "cursor-pointer hover:ring-2 hover:ring-primary-container/40" : "cursor-default"}
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
                </button>
              );
            })}
          </div>
        </Panel>

        {/* ---- decision explainability ---- */}
        <Panel title="Dock Decision" icon="account_tree">
          {!selected ? (
            <Empty
              message="Select an occupied dock"
              hint="The scoring behind its assignment appears here."
            />
          ) : !detail ? (
            <Spinner label="Loading decision" />
          ) : (
            <div className="flex flex-col gap-4 p-5">
              <div>
                <span className="label">Trailer</span>
                <p className="mono text-body-lg text-primary">{selected}</p>
              </div>

              <div>
                <span className="label">Hard constraints</span>
                <ul className="mt-1.5 flex flex-col gap-1.5">
                  {[
                    ["Active dock status", true],
                    ["Compatible load type", true],
                    ["No conflicting assignment", true],
                  ].map(([text]) => (
                    <li key={String(text)} className="flex items-center gap-2 text-body-sm">
                      <Icon name="check_circle" className="!text-[18px] text-success" />
                      {text}
                    </li>
                  ))}
                  <li className="text-body-sm text-on-surface-variant">
                    {String(detail.hard_constraints?.eligible_docks ?? "?")} eligible,{" "}
                    {String(detail.hard_constraints?.rejected_docks ?? 0)} rejected
                  </li>
                </ul>
              </div>

              <div>
                <span className="label">Heuristic ranking</span>
                <div className="mt-2 flex flex-col gap-2">
                  {[
                    { label: "Priority (50%)", value: detail.priority_score ?? 0, positive: true },
                    { label: "Specialisation (30%)", value: detail.specialization_score ?? 0, positive: true },
                    { label: "Yard position penalty", value: detail.position_penalty ?? 0, positive: false },
                  ].map((row) => (
                    <div key={row.label}>
                      <div className="flex justify-between text-body-sm">
                        <span>{row.label}</span>
                        <span className={`tnum font-semibold ${row.positive ? "" : "text-error"}`}>
                          {row.value.toFixed(2)}
                        </span>
                      </div>
                      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-container-high">
                        <div
                          className={`h-full rounded-full ${row.positive ? "bg-primary-container" : "bg-error"}`}
                          style={{ width: `${Math.min(100, Math.abs(row.value) * 100)}%` }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="rounded-lg border border-outline-variant/60 bg-surface-container-low p-3">
                <div className="flex items-baseline justify-between">
                  <span className="label">Final score</span>
                  <span className="text-headline-lg tnum text-primary">
                    {(detail.final_score ?? 0).toFixed(2)}
                  </span>
                </div>
                <p className="mono mt-1 text-on-surface-variant">{detail.formula}</p>
                {detailReason && (
                  <p className="mt-2 text-body-sm text-primary">Reason: {detailReason}</p>
                )}
              </div>

              {detail.candidates && detail.candidates.length > 1 && (
                <div>
                  <span className="label">Runners-up</span>
                  <ul className="mt-1.5 flex flex-col gap-1">
                    {detail.candidates.slice(1, 5).map((c) => (
                      <li key={c.dock_id} className="flex justify-between text-body-sm">
                        <span className="mono">{c.dock_id}</span>
                        <span className="tnum text-on-surface-variant">
                          {c.final_score.toFixed(2)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </Panel>
      </div>

      {/* ---- active trailers ---- */}
      <Panel title="Active Trailers" icon="local_shipping">
        {yard.trailers.length === 0 ? (
          <Empty message="No trailers in the yard." />
        ) : (
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className="th">Trailer</th>
                <th className="th">Carrier</th>
                <th className="th">Status</th>
                <th className="th">Priority</th>
                <th className="th">Dock</th>
                <th className="th">ETA</th>
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
                    <Link to={`/track/${t.id}`} className="mono font-semibold text-primary hover:underline">
                      {t.id}
                    </Link>
                    <div className="text-body-sm text-on-surface-variant">{t.load_type}</div>
                  </td>
                  <td className="td text-on-surface-variant">{t.carrier ?? "—"}</td>
                  <td className="td">
                    <Badge tone={statusTone(t.status)}>{t.status.replace("_", " ")}</Badge>
                  </td>
                  <td className="td">
                    <Badge tone={t.priority === "critical" ? "danger" : t.priority === "high" ? "warning" : "neutral"}>
                      {t.priority}
                    </Badge>
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
                      <span className="text-body-sm italic text-outline">Unassigned</span>
                    )}
                  </td>
                  <td className="td tnum text-on-surface-variant">{clock(t.eta)}</td>
                  <td className="td">
                    {/* Yard actions are operator/admin only. A row for other
                        roles shows the state without the levers, rather than
                        buttons that would come back 403. */}
                    <div className="flex gap-2">
                      {!can(PERM.yardWrite) && (
                        <span className="text-body-sm italic text-outline">View only</span>
                      )}
                      {can(PERM.yardWrite) && t.status === "EN_ROUTE" && (
                        <button
                          className="btn-secondary !py-1 !px-2 !text-body-sm"
                          disabled={busy === t.id}
                          onClick={() => act(t.id, "arrive")}
                        >
                          Arrive
                        </button>
                      )}
                      {can(PERM.yardWrite) && t.status === "ARRIVED" && (
                        <button
                          className="btn-primary !py-1 !px-2 !text-body-sm"
                          disabled={busy === t.id || !t.dock_assignment}
                          onClick={() => act(t.id, "dock")}
                        >
                          Dock
                        </button>
                      )}
                      {can(PERM.yardWrite) && t.status === "DOCKED" && (
                        <button
                          className="btn-primary !py-1 !px-2 !text-body-sm"
                          disabled={busy === t.id}
                          onClick={() => act(t.id, "unload")}
                        >
                          Unload
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
