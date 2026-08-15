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

const MAX_BUFFER = 60;

export function useEventStream() {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [state, setState] = useState<ConnectionState>("connecting");
  const [lastEventAt, setLastEventAt] = useState<Date | null>(null);
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
          setEvents((prev) => {
            const event = message as LiveEvent;
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

  return { events, state, lastEventAt };
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
