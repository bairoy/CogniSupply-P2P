import { useEffect, useRef, useState } from "react";
import { GATEWAY } from "../api";

/**
 * The public customer tracker's live rail -- one socket, one consignment.
 *
 * Deliberately NOT useEventStream(). That hook opens /ws/dashboard, which
 * needs a token the customer does not have and carries the whole cross-domain
 * firehose (POs, invoices, payments, supplier scores) that they must never be
 * sent. /ws/track/{ref} is the public, single-trailer counterpart -- see the
 * TrackHub comment in dashboard_gateway/main.py.
 *
 * Same contract as the dashboard rail, though: this reports WHEN to re-read,
 * never WHAT changed. `onUpdate` refetches GET /track/{ref}, which stays the
 * only thing that decides what a customer is shown.
 */

/** Poll cadence with the socket down -- the tracker's original behaviour. */
const POLL_OFFLINE_MS = 8000;
/**
 * ...and with it up. Not removed: a dropped delta (the gateway sheds events
 * when its inbox fills, by design) would otherwise freeze the page until the
 * next real movement, which on a docked trailer can be many minutes.
 */
const POLL_LIVE_MS = 30000;

const MAX_BACKOFF_MS = 15000;

export function useTrackStream(ref: string | undefined, onUpdate: () => void) {
  const [live, setLive] = useState(false);
  // Held in a ref so a new `onUpdate` identity each render never tears the
  // socket down and reconnects it.
  const handler = useRef(onUpdate);
  handler.current = onUpdate;

  useEffect(() => {
    if (!ref) return;
    let closedByUs = false;
    let retryTimer: number | undefined;
    let attempt = 0;
    let socket: WebSocket | null = null;

    const connect = () => {
      const url =
        GATEWAY.replace(/^http/, "ws") + `/ws/track/${encodeURIComponent(ref)}`;
      const ws = new WebSocket(url);
      socket = ws;

      ws.onopen = () => {
        attempt = 0;
        setLive(true);
      };

      ws.onmessage = (raw) => {
        try {
          const message = JSON.parse(raw.data);
          // "hello" is the handshake; the page already loaded over REST.
          if (message.type === "update") handler.current();
        } catch {
          /* ignore malformed frames rather than tearing down the socket */
        }
      };

      ws.onclose = () => {
        setLive(false);
        if (closedByUs) return;
        const backoff = Math.min(1000 * 2 ** attempt, MAX_BACKOFF_MS);
        attempt += 1;
        retryTimer = window.setTimeout(connect, backoff);
      };

      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      closedByUs = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      socket?.close();
    };
  }, [ref]);

  return { live, pollMs: live ? POLL_LIVE_MS : POLL_OFFLINE_MS };
}
