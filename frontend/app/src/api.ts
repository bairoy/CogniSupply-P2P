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
  /** v6 — the planned service window, and how long the trailer queues for it. */
  planned_start?: string | null;
  planned_end?: string | null;
  planned_wait_minutes?: number | null;
}

/** One line of the objective, itemised so the UI can show the arithmetic. */
export interface CostTerm {
  cost: number;
  [key: string]: unknown;
}

/**
 * v6 score_breakdown. The pre-v6 weighted-score fields are kept optional
 * because history rows written by the old engine still carry them, and the
 * Yard & Dock screen renders whichever shape it is handed.
 */
export interface ScoreBreakdown {
  engine?: "cp-sat" | "greedy" | string;
  solver_status?: string;
  objective?: string;
  hard_constraints?: Record<string, unknown>;
  planned_start?: string;
  planned_end?: string;
  wait_minutes?: number;
  service_minutes?: number;
  cost?: number;
  cost_terms?: {
    wait?: CostTerm & { minutes?: number; weight_per_minute?: number; priority?: string };
    flexibility?: CostTerm & { handles_load_types?: number; note?: string };
    position?: CostTerm & { yard_position?: number };
    utilisation?: CostTerm & { committed_minutes?: number };
    churn?: CostTerm & { moved_from?: string | null };
    total?: number;
  };
  alternatives?: {
    dock_id: string;
    wait_minutes: number;
    cost: number;
    delta_vs_chosen: number;
    available_at: string;
  }[];
  rejected?: { dock_id: string; reason: string }[];
  plan?: {
    trailers_planned?: number;
    total_cost?: number;
    greedy_baseline_cost?: number;
    improvement_vs_greedy?: number;
  };
  ready_at?: string;
  scored_at?: string;
  source?: string;
  /** Operator override rows carry these instead of a full plan. */
  overridden_from?: string;
  note?: string;
  /** Pre-v6 rows only. */
  final_score?: number;
  formula?: string;
  candidates?: { dock_id: string; final_score: number }[];
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
  /** v6 — when it cleared the gate, and how long it has queued since. */
  arrived_at?: string | null;
  waiting_minutes?: number | null;
  /**
   * v7 — which way this trailer is moving. An inbound trailer is identified by
   * the PO it fulfils, an outbound one by the customer order it collects, so
   * exactly one of `po_id` / `outbound_order_id` is set.
   */
  direction?: Direction;
  outbound_order_id?: string | null;
  customer_name?: string | null;
}

export type Direction = "INBOUND" | "OUTBOUND";

// ---- v7 outbound ----

export interface LoadPlanLine {
  load_plan_id: string;
  material_id: string;
  material_name?: string | null;
  qty_ordered: number;
  qty_staged: number;
  qty_loaded: number;
  status: "PLANNED" | "PICKING" | "STAGED" | "LOADED" | "SHORT" | string;
}

export interface OutboundOrderSummary {
  id: string;
  customer_name: string;
  status: string;
  priority: string;
  requested_ship_date: string | null;
  created_at: string;
  destination: string | null;
  line_count: number;
  qty_ordered: number;
  qty_staged: number;
  qty_loaded: number;
  /** Derived server-side from qty_staged/qty_ordered — never stored. */
  staged_pct: number;
  trailer: { id: string; status: string; eta: string | null } | null;
  shipment_id: string | null;
  carrier: string | null;
  tracking_number: string | null;
  dock_assignment: { dock_id: string; status: string; planned_start: string | null } | null;
}

export interface OutboundOrderDetail {
  id: string;
  customer_name: string;
  status: string;
  priority: string;
  requested_ship_date: string | null;
  created_at: string;
  updated_at: string;
  destination: { id: string | null; name: string | null; latitude: number | null; longitude: number | null };
  lines: LoadPlanLine[];
  shipment: { id: string; carrier: string | null; tracking_number: string | null; status: string; expected_arrival: string | null } | null;
  trailer: { id: string; status: string; eta: string | null; load_type: string | null; priority: string | null } | null;
  dock_assignments: DockAssignment[];
  goods_issue: { id: string; trailer_id: string; qty_issued: number; lines: unknown; issued_at: string; verified_by: string } | null;
  timeline: { event_id: number; entity_type: string; entity_id: string; event_type: string; payload: Record<string, unknown> | null; timestamp: string }[];
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
  /** v7 adds LOADING — the same occupancy as UNLOADING, opposite direction. */
  state: "EMPTY" | "RESERVED" | "UNLOADING" | "LOADING" | "BLOCKED";
  unload_progress_pct: number | null;
  service_minutes?: number;
  window_start?: string | null;
  window_end?: string | null;
  next_trailer_id?: string | null;
  next_start?: string | null;
  /** v7 — which way the truck currently at (or next at) this door is going. */
  direction?: Direction | null;
  next_direction?: Direction | null;
}

/** The movement picture: what is inbound, at a door, and waiting to leave. */
export interface YardSummary {
  inbound: number;
  in_yard_waiting: number;
  at_door: number;
  awaiting_exit: number;
  unassigned: number;
  docks_total: number;
  docks_active: number;
  docks_busy: number;
  dock_occupancy_pct: number;
  avg_wait_minutes: number;
  longest_wait_minutes: number;
  /** v7 — direction split. The keys above keep their original meaning. */
  outbound_en_route?: number;
  outbound_in_yard?: number;
  outbound_at_door?: number;
  outbound_loaded?: number;
  inbound_at_door?: number;
  trailers_on_site?: number;
  docks_loading?: number;
  docks_unloading?: number;
}

export interface YardStatus {
  trailers: Trailer[];
  docks: Dock[];
  summary: YardSummary;
}

export interface DockBooking {
  assignment_id: string;
  trailer_id: string;
  assignment_status: string;
  reason: string | null;
  start: string;
  end: string;
  in_progress: boolean;
  priority: string | null;
  load_type: string | null;
  trailer_status: string | null;
  po_id: string | null;
  carrier: string | null;
  wait_minutes: number | null;
  source: string;
}

export interface DockLane {
  id: string;
  yard_position: number;
  compatible_load_types: string[];
  is_active: boolean;
  service_minutes: number;
  committed_minutes: number;
  utilisation_pct: number;
  bookings: DockBooking[];
}

export interface DockSchedule {
  generated_at: string;
  horizon_hours: number;
  docks: DockLane[];
  summary: {
    docks_total: number;
    docks_active: number;
    booked_windows: number;
    utilisation_pct: number;
  };
}

export interface Overview {
  active_trailers: number;
  open_exceptions: number;
  critical_exceptions: number;
  pending_invoices: number;
  docks_occupied: number;
  docks_total: number;
  open_alerts: number;
  /** v7 — outbound is counted separately; it has no invoice behind it. */
  outbound_open_orders?: number;
  outbound_delivered?: number;
  outbound_trailers?: number;
  goods_issues?: number;
  kpis: {
    first_pass_match_rate: number;
    touchless_rate: number;
    dock_utilisation: number;
    avg_turnaround_minutes: number | null;
    avg_p2p_cycle_hours: number | null;
    human_interventions: number;
    avg_outbound_turnaround_minutes?: number | null;
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
