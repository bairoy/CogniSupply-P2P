/**
 * API client.
 *
 * Three services, three base URLs -- the split mirrors the backend's ownership
 * boundaries rather than hiding them behind one gateway, so it stays obvious
 * which domain a screen is talking to.
 */

export const YARD = import.meta.env.VITE_YARD_API ?? "http://127.0.0.1:8001";
export const PROCUREMENT = import.meta.env.VITE_PROCUREMENT_API ?? "http://127.0.0.1:8002";
export const GATEWAY = import.meta.env.VITE_GATEWAY_API ?? "http://127.0.0.1:8003";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
  /** The caller is signed in but their role is not allowed to do this. */
  get forbidden() {
    return this.status === 403;
  }
}

/**
 * The bearer token, held in a module variable rather than read from
 * localStorage on every call. auth.tsx owns it and calls setToken() whenever
 * the session changes; keeping it here means api.ts has no opinion about how
 * sessions are stored.
 */
let authToken: string | null = null;

export function setToken(token: string | null) {
  authToken = token;
}

export function getToken() {
  return authToken;
}

async function request<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  // FormData sets its own multipart boundary -- forcing application/json here
  // would corrupt the invoice OCR upload.
  if (!(init?.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (authToken) headers.set("Authorization", `Bearer ${authToken}`);

  const res = await fetch(`${base}${path}`, { ...init, headers });

  if (!res.ok) {
    // Every service returns a JSON error envelope (see shared/api.py), but a
    // proxy or a crash can still yield HTML -- fall back to text so the UI
    // shows the real problem rather than a JSON parse error.
    let detail: string;
    try {
      const body = await res.json();
      detail = body.detail ?? body.error ?? JSON.stringify(body);
    } catch {
      detail = await res.text();
    }

    // An expired or revoked token can surface from any of the three services
    // and from any screen. Announcing it once here lets AuthProvider end the
    // session in one place, instead of every screen having to handle 401.
    // 403 is deliberately NOT included: that is a live session hitting a
    // permission wall, and signing the user out for it would be wrong.
    if (res.status === 401) {
      window.dispatchEvent(new CustomEvent("inbound:unauthorized"));
    }

    throw new ApiError(detail || res.statusText, res.status);
  }
  return res.json() as Promise<T>;
}

export const api = {
  yard: <T>(path: string, init?: RequestInit) => request<T>(YARD, path, init),
  procurement: <T>(path: string, init?: RequestInit) => request<T>(PROCUREMENT, path, init),
  gateway: <T>(path: string, init?: RequestInit) => request<T>(GATEWAY, path, init),
  post: <T>(base: string, path: string, body?: unknown) =>
    request<T>(base, path, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
};

// ---- shared response shapes (only the fields the UI actually reads) ----

export interface DockAssignment {
  id: string;
  dock_id: string;
  status: string;
  reason: string | null;
  docked_at: string | null;
  unload_progress_pct: number | null;
  score_breakdown?: ScoreBreakdown | null;
  assigned_at?: string;
}

export interface ScoreBreakdown {
  hard_constraints?: Record<string, unknown>;
  priority_score?: number;
  specialization_score?: number;
  position_penalty?: number;
  final_score?: number;
  formula?: string;
  candidates?: { dock_id: string; final_score: number }[];
  rejected?: { dock_id: string; reason: string }[];
}

export interface Trailer {
  id: string;
  status: string;
  eta: string | null;
  priority: string;
  load_type: string;
  po_id: string | null;
  carrier: string | null;
  tracking_number: string | null;
  latitude: number | null;
  longitude: number | null;
  dock_assignment: DockAssignment | null;
}

export interface Dock {
  id: string;
  yard_position: number;
  compatible_load_types: string[];
  is_active: boolean;
  occupied: boolean;
  current_trailer_id: string | null;
  assignment_status: string | null;
  assignment_reason: string | null;
  state: "EMPTY" | "RESERVED" | "UNLOADING" | "BLOCKED";
  unload_progress_pct: number | null;
}

export interface YardStatus {
  trailers: Trailer[];
  docks: Dock[];
}

export interface Overview {
  active_trailers: number;
  open_exceptions: number;
  critical_exceptions: number;
  pending_invoices: number;
  docks_occupied: number;
  docks_total: number;
  open_alerts: number;
  kpis: {
    first_pass_match_rate: number;
    touchless_rate: number;
    dock_utilisation: number;
    avg_turnaround_minutes: number | null;
    avg_p2p_cycle_hours: number | null;
    human_interventions: number;
  };
}

export interface PipelineStage {
  key: string;
  label: string;
  count: number;
  delayed?: number;
  exceptions?: number;
  in_progress?: number;
}

export interface QueueItem {
  id: string;
  source: "exception" | "alert";
  type: string;
  severity: string;
  status: string;
  impact_amount: number | null;
  created_at: string;
  entity_id: string | null;
  detail: string | null;
  owner: string;
  owner_id: string | null;
  resolvable: boolean;
}

export interface AtRiskItem {
  reference_id: string;
  entity_id: string;
  kind: string;
  issue_type: string;
  severity: string;
  value: number | null;
  created_at: string;
  supplier: string | null;
  owner: string;
  message?: string;
}

export interface TimelineEvent {
  entity_type: string;
  entity_id: string;
  event_type: string;
  summary: string | null;
  payload: Record<string, unknown> | null;
  at: string;
}
