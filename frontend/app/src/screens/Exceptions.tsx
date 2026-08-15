import { useCallback, useEffect, useState } from "react";
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
  moneyCompact,
  severityTone,
} from "../components/ui";
import { LiveEvent, useRefetchOn } from "../hooks/useEventStream";

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
