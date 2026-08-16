import { useEffect, useRef, useState } from "react";
import { GATEWAY } from "../api";
import { storedToken, useAuth } from "../auth";

/**
 * The single WebSocket connection to /ws/dashboard.
 *
 * README §5's rule, enforced here: this stream carries CHANGES only. Screens
 * load their current state over REST on mount and use these events as deltas.
 * Nothing reconstructs state from the stream alone, so a client that connects
 * five minutes into a demo is never looking at a half-built picture.
 *
 * Reconnects with backoff, because a dashboard that silently stops updating is
 * worse than one that says it is disconnected.
 */

export interface LiveEvent {
  event_id: string;
  event_type: string;
  entity_type: string;
  entity_id: string;
  timestamp: string;
  payload: Record<string, unknown> | string;
}

export type ConnectionState = "connecting" | "live" | "offline";

/**
 * Telemetry, not facts.
 *
 * A GPS ping is a sensor sampling a continuous quantity, not something that
 * happened. It belongs on the map -- where 600 points are a smooth line -- and
 * never in the rail, where it is 600 rows saying nothing and a 60-slot buffer
 * that a single driving truck can fill on its own, pushing out the received,
 * matched and paid events the rail exists to show.
 *
 * Nothing subscribes to these for data: both maps poll (TrailerMap every 10s,
 * Track on its own timer) and every useRefetchOn() list below is facts only.
 * So they are dropped from the buffer and counted instead -- see `pulse`.
 */
export const TELEMETRY_EVENT_TYPES = new Set(["TRAILER_LOCATION_UPDATED"]);

/** The one thing a stream of pings genuinely tells you: the pipe is alive. */
export interface TelemetryPulse {
  /** Pings seen since this page loaded, across reconnects. */
  total: number;
  /** Distinct vehicles that have reported in the last PULSE_WINDOW_MS. */
  reporting: number;
  lastAt: Date | null;
}

const MAX_BUFFER = 60;
/** A truck that has not pinged for this long is no longer "reporting". */
const PULSE_WINDOW_MS = 90_000;

export function useEventStream() {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [state, setState] = useState<ConnectionState>("connecting");
  const [lastEventAt, setLastEventAt] = useState<Date | null>(null);
  const [pulse, setPulse] = useState<TelemetryPulse>({
    total: 0,
    reporting: 0,
    lastAt: null,
  });
  // Pings arrive several times a second across the fleet. Accumulating them in
  // a ref and publishing to state on a 1s timer keeps that from re-rendering
  // the whole app on every GPS sample.
  const pulseRef = useRef({ total: 0, seen: new Map<string, number>(), lastAt: 0 });
  const socketRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const closedByUs = useRef(false);
  // Reconnect under the new identity when the signed-in user changes, so a
  // logout followed by a different sign-in never leaves the old socket open.
  const { user } = useAuth();

  useEffect(() => {
    if (!user) return;
    closedByUs.current = false;
    let retryTimer: number | undefined;

    const connect = () => {
      // The token goes in the query string because the browser WebSocket API
      // cannot set an Authorization header. The gateway validates it before
      // accept() and closes with 1008 if it is bad -- see ws_dashboard().
      const token = storedToken();
      if (!token) return;
      const url =
        GATEWAY.replace(/^http/, "ws") +
        `/ws/dashboard?token=${encodeURIComponent(token)}`;
      setState("connecting");
      const ws = new WebSocket(url);
      socketRef.current = ws;

      ws.onopen = () => {
        attemptRef.current = 0;
        setState("live");
      };

      ws.onmessage = (raw) => {
        try {
          const message = JSON.parse(raw.data);
          if (message.type === "hello") return;
          // The gateway forwards `payload` as the JSON string it was stored
          // as; parse it once here so every consumer gets an object.
          if (typeof message.payload === "string") {
            try {
              message.payload = JSON.parse(message.payload);
            } catch {
              /* leave as string if it is not valid JSON */
            }
          }
          // Redis delivery is at-least-once by design (redis-contract.md §8):
          // reconcile_unpublished() can XADD an event a second time if the
          // publisher died between the XADD and flipping redis_published, and
          // the reconciler's 1s sweep can race a just-published row. Backend
          // consumers dedupe in Postgres via processed_events; this rail is a
          // consumer too, and without the same guard the same event shows up
          // twice on screen. event_id is the canonical event_log id, so it is
          // exactly the right key.
          const event = message as LiveEvent;
          if (TELEMETRY_EVENT_TYPES.has(event.event_type)) {
            // Counted, not buffered. The dedupe below is unnecessary here: a
            // duplicate ping changes a count by one and a "last seen" by
            // nothing, which is not worth a scan of the buffer.
            const p = pulseRef.current;
            p.total += 1;
            p.lastAt = Date.now();
            p.seen.set(event.entity_id, p.lastAt);
            return;
          }
          setEvents((prev) => {
            if (prev.some((e) => e.event_id === event.event_id)) return prev;
            return [event, ...prev].slice(0, MAX_BUFFER);
          });
          setLastEventAt(new Date());
        } catch {
          /* ignore malformed frames rather than tearing down the socket */
        }
      };

      ws.onclose = () => {
        if (closedByUs.current) return;
        setState("offline");
        const backoff = Math.min(1000 * 2 ** attemptRef.current, 15000);
        attemptRef.current += 1;
        retryTimer = window.setTimeout(connect, backoff);
      };

      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      closedByUs.current = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      socketRef.current?.close();
    };
  }, [user?.id]);

  /* Publish the accumulated pulse once a second, ageing out vehicles that have
     gone quiet so "reporting" means now, not ever. */
  useEffect(() => {
    const timer = window.setInterval(() => {
      const p = pulseRef.current;
      const cutoff = Date.now() - PULSE_WINDOW_MS;
      for (const [id, at] of p.seen) if (at < cutoff) p.seen.delete(id);
      setPulse((prev) => {
        const next = {
          total: p.total,
          reporting: p.seen.size,
          lastAt: p.lastAt ? new Date(p.lastAt) : null,
        };
        // Re-rendering every consumer once a second to change nothing is the
        // cost this whole hook exists to avoid.
        const same =
          prev.total === next.total &&
          prev.reporting === next.reporting &&
          prev.lastAt?.getTime() === next.lastAt?.getTime();
        return same ? prev : next;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  return { events, state, lastEventAt, pulse };
}

/**
 * Re-run `onChange` when an event of interest arrives.
 *
 * Screens use this to refetch just the slice they own. Refetching on a
 * relevant delta keeps the code honest -- the REST read stays the source of
 * truth and the socket only decides WHEN to re-read.
 */
export function useRefetchOn(
  events: LiveEvent[],
  eventTypes: string[],
  onChange: () => void,
) {
  const seen = useRef<string | null>(null);
  useEffect(() => {
    const hit = events.find((e) => eventTypes.includes(e.event_type));
    if (!hit || hit.event_id === seen.current) return;
    seen.current = hit.event_id;
    onChange();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events]);
}
