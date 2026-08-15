import { useEffect, useRef, useState } from "react";
import { LiveEvent } from "../hooks/useEventStream";
import { Icon } from "./ui";

/**
 * Real-time push alerts (E2 requirement #4).
 *
 * The operator cannot be expected to be looking at the yard board when a truck
 * slips: the disruption has to come to them. This rides the SAME WebSocket
 * every screen already uses -- it takes the `events` array as a prop rather
 * than opening a second socket, so there is still exactly one connection and
 * the event rail and the toasts can never disagree about what happened.
 *
 * Only genuinely disruptive events raise a toast. Everything else stays in the
 * rail: a notification that fires on every event is a notification nobody
 * reads.
 */

type ToastTone = "danger" | "warning" | "info";

interface Toast {
  id: string;
  tone: ToastTone;
  title: string;
  body: string;
  icon: string;
  at: string;
}

const DISMISS_AFTER_MS = 9000;
const MAX_VISIBLE = 4;

const TONE_STYLE: Record<ToastTone, { box: string; icon: string; bar: string }> = {
  danger: {
    box: "border-error/40 bg-error-container",
    icon: "text-error",
    bar: "bg-error",
  },
  warning: {
    box: "border-warning/40 bg-warning-container",
    icon: "text-warning",
    bar: "bg-warning",
  },
  info: {
    box: "border-primary-container/50 bg-[#e2dfff]",
    icon: "text-primary",
    bar: "bg-primary-container",
  },
};

function num(value: unknown): number | null {
  const n = typeof value === "string" ? Number(value) : typeof value === "number" ? value : NaN;
  return Number.isFinite(n) ? n : null;
}

/**
 * Decide whether an event deserves to interrupt, and how it should read.
 *
 * Returns null for everything that does not. The payload's own `summary` is
 * preferred over anything composed here -- the backend already writes one on
 * every event and it is the phrasing the audit trail will show.
 */
function toastFor(event: LiveEvent): Toast | null {
  const payload =
    typeof event.payload === "object" && event.payload
      ? (event.payload as Record<string, unknown>)
      : {};
  const summary = typeof payload.summary === "string" ? payload.summary : null;
  const base = { id: event.event_id, at: event.timestamp };

  switch (event.event_type) {
    /* A delay alert raised by dock-worker -- either a long queue for a door or
       a trailer whose ETA slipped past the re-plan threshold. */
    case "ALERT_CREATED": {
      if (payload.alert_type !== "DELAY") return null;
      const delta = num(payload.delta_minutes);
      const trailer = payload.trailer_id;
      // The stored summary carries the new ETA as a raw ISO string, which is
      // the right thing in an audit record and the wrong thing in a toast.
      // Compose from the same fields when they are there; fall back otherwise.
      return {
        ...base,
        tone: "danger",
        icon: "warning",
        title: "Delay alert",
        body:
          trailer && delta
            ? `${trailer} delayed by ${Math.round(delta)} min — the yard is being re-planned`
            : summary ?? `${event.entity_id} is delayed`,
      };
    }

    /* The door itself is the bottleneck: the truck is on site and queuing. */
    case "DOCK_DELAYED": {
      const wait = num(payload.wait_minutes);
      return {
        ...base,
        tone: "warning",
        icon: "hourglass_top",
        title: "Dock queue building",
        body:
          summary ??
          `${payload.trailer_id ?? event.entity_id} waits ${wait ?? "?"} min for ${payload.dock_id ?? "a door"}`,
      };
    }

    /* Negative drift only. An ETA that moves EARLIER is good news and does not
       get to interrupt anyone -- Yard API stamps the direction for us. */
    case "ETA_UPDATED": {
      if (payload.direction !== "later") return null;
      const delta = num(payload.delta_minutes);
      return {
        ...base,
        tone: "warning",
        icon: "schedule",
        title: "ETA slipped",
        body: summary ?? `${event.entity_id} is running ${Math.round(delta ?? 0)} min late`,
      };
    }

    /* The plan changed under the operator's feet -- which door to expect the
       truck at is exactly the thing they need told, not left to discover. */
    case "DOCK_REASSIGNED": {
      const from = payload.old_dock_id ?? "—";
      const to = payload.new_dock_id ?? "—";
      return {
        ...base,
        tone: "info",
        icon: "swap_horiz",
        title: "Dock reassigned",
        body: summary ?? `${payload.trailer_id ?? event.entity_id} moved ${from} → ${to}`,
      };
    }

    default:
      return null;
  }
}

export default function EventToaster({ events }: { events: LiveEvent[] }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  // Every event_id this component has already judged. The stream is
  // at-least-once (redis-contract.md §8) and the buffer is re-rendered on every
  // frame, so without this the same delay would pop repeatedly.
  const seen = useRef<Set<string>>(new Set());
  const primed = useRef(false);

  useEffect(() => {
    // First pass swallows whatever was already buffered. Landing on a screen
    // should not replay a backlog of alerts that are minutes old.
    if (!primed.current) {
      primed.current = true;
      events.forEach((e) => seen.current.add(e.event_id));
      return;
    }

    const fresh: Toast[] = [];
    // The buffer is newest-first; walk it oldest-first so a burst stacks in the
    // order it actually happened.
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const event = events[i];
      if (seen.current.has(event.event_id)) continue;
      seen.current.add(event.event_id);
      const toast = toastFor(event);
      if (toast) fresh.push(toast);
    }
    if (fresh.length === 0) return;
    setToasts((prev) => [...prev, ...fresh].slice(-MAX_VISIBLE));
  }, [events]);

  /* One timer per toast, cleared on unmount or manual dismissal. */
  useEffect(() => {
    if (toasts.length === 0) return;
    const timers = toasts.map((t) =>
      window.setTimeout(
        () => setToasts((prev) => prev.filter((p) => p.id !== t.id)),
        DISMISS_AFTER_MS,
      ),
    );
    return () => timers.forEach(window.clearTimeout);
  }, [toasts]);

  if (toasts.length === 0) return null;

  return (
    <div
      className="pointer-events-none fixed bottom-5 right-5 z-50 flex w-[360px] max-w-[calc(100vw-2.5rem)] flex-col gap-2"
      role="status"
      aria-live="polite"
    >
      {toasts.map((t) => {
        const style = TONE_STYLE[t.tone];
        return (
          <div
            key={t.id}
            className={`toast-in pointer-events-auto overflow-hidden rounded-lg border shadow-overlay ${style.box}`}
          >
            <div className="flex items-start gap-3 p-3.5">
              <Icon name={t.icon} className={style.icon} />
              <div className="min-w-0 flex-1">
                <p className="text-body-md font-semibold text-on-surface">{t.title}</p>
                <p className="mt-0.5 text-body-sm text-on-surface-variant">{t.body}</p>
              </div>
              <button
                type="button"
                onClick={() => setToasts((prev) => prev.filter((p) => p.id !== t.id))}
                className="shrink-0 rounded text-on-surface-variant transition hover:text-on-surface"
                aria-label="Dismiss notification"
              >
                <Icon name="close" className="!text-[18px]" />
              </button>
            </div>
            <div className="h-0.5 w-full bg-black/5">
              <div className={`toast-timer h-full ${style.bar}`} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
