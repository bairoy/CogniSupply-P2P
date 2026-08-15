/**
 * Authentication state for the whole app.
 *
 * The token lives in localStorage, not in React state alone, so a page reload
 * during a demo does not drop you back at the sign-in screen. It is read by
 * api.ts on every request (see setToken below) and by useEventStream for the
 * WebSocket's ?token= parameter.
 *
 * WHAT THIS IS NOT: security. Every permission check here is about what the UI
 * renders. The backend re-checks the caller's role on every single write
 * (shared/auth.py), so hiding a button is a courtesy that saves the user a
 * 403, never the thing that stops them.
 *
 * localStorage is chosen over an httpOnly cookie deliberately: three separate
 * API origins (:8001/:8002/:8003) would each need their own cookie domain and
 * CSRF handling, whereas one bearer token works across all three unchanged.
 */

import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { api, ApiError, setToken } from "./api";

export interface AuthUser {
  id: string;
  name: string;
  email?: string;
  role: string;
  permissions: string[];
}

interface Session {
  token: string;
  user: AuthUser;
}

interface AuthContextValue {
  user: AuthUser | null;
  /** True until the stored token has been checked against the server. */
  loading: boolean;
  login(email: string, password: string): Promise<void>;
  signup(name: string, email: string, password: string, role: string): Promise<void>;
  logout(): void;
  /** Does this user's role carry a capability, e.g. can("yard:write")? */
  can(permission: string): boolean;
}

const STORAGE_KEY = "inbound.auth";

// Capability names, mirroring shared/auth.py. Imported by screens so a typo is
// a compile error rather than a button that silently never renders.
export const PERM = {
  yardWrite: "yard:write",
  procurementWrite: "procurement:write",
  invoiceWrite: "invoice:write",
  exceptionAssign: "exception:assign",
  exceptionResolve: "exception:resolve",
  paymentWrite: "payment:write",
  alertAck: "alert:ack",
  adminUsers: "admin:users",
} as const;

export const ROLE_LABEL: Record<string, string> = {
  operator: "Yard Operator",
  procurement: "Procurement",
  finance: "Finance",
  admin: "Administrator",
  system: "Service Account",
};

/** Read the persisted session synchronously so the first render is not a flash. */
function readStored(): Session | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Session;
    return parsed?.token && parsed?.user ? parsed : null;
  } catch {
    return null;
  }
}

function writeStored(session: Session | null) {
  if (session) localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  else localStorage.removeItem(STORAGE_KEY);
  setToken(session?.token ?? null);
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const stored = readStored();
  // Seed api.ts before the first render so an immediate fetch is authenticated.
  if (stored) setToken(stored.token);

  const [user, setUser] = useState<AuthUser | null>(stored?.user ?? null);
  const [loading, setLoading] = useState(Boolean(stored));

  const logout = useCallback(() => {
    writeStored(null);
    setUser(null);
  }, []);

  /**
   * Revalidate a token that survived a reload. The token may have expired, or
   * an admin may have changed this user's role or deactivated them since it
   * was issued -- trusting localStorage alone would show a stale set of
   * controls that then 403 on first use.
   */
  useEffect(() => {
    if (!stored) return;
    let cancelled = false;

    api
      .gateway<AuthUser & { role_changed: boolean }>("/auth/me")
      .then((me) => {
        if (cancelled) return;
        if (me.role_changed) {
          // The signed token still carries the OLD role and that is what the
          // services enforce on. A fresh token is the only way to pick up the
          // new one, so send them back through sign-in rather than showing
          // controls their token cannot actually use.
          logout();
          return;
        }
        const next: AuthUser = {
          id: me.id, name: me.name, email: me.email,
          role: me.role, permissions: me.permissions,
        };
        setUser(next);
        writeStored({ token: stored.token, user: next });
      })
      .catch((err) => {
        if (cancelled) return;
        // 401/403 means the token is dead. Any other failure is the gateway
        // being down -- keep the session so a restart does not sign everyone
        // out, and let the screens show their own connection errors.
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) logout();
      })
      .finally(() => !cancelled && setLoading(false));

    return () => {
      cancelled = true;
    };
    // Runs once on mount; `stored` is read synchronously above and never changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Any 401 from anywhere in the app ends the session exactly once. */
  useEffect(() => {
    const onUnauthorized = () => logout();
    window.addEventListener("inbound:unauthorized", onUnauthorized);
    return () => window.removeEventListener("inbound:unauthorized", onUnauthorized);
  }, [logout]);

  const accept = useCallback((session: Session) => {
    writeStored(session);
    setUser(session.user);
    setLoading(false);
  }, []);

  const login = useCallback(
    async (email: string, password: string) => {
      const res = await api.post<Session>(
        import.meta.env.VITE_GATEWAY_API ?? "http://127.0.0.1:8003",
        "/auth/login",
        { email, password },
      );
      accept(res);
    },
    [accept],
  );

  const signup = useCallback(
    async (name: string, email: string, password: string, role: string) => {
      const res = await api.post<Session>(
        import.meta.env.VITE_GATEWAY_API ?? "http://127.0.0.1:8003",
        "/auth/signup",
        { name, email, password, role },
      );
      accept(res);
    },
    [accept],
  );

  const can = useCallback(
    (permission: string) => Boolean(user?.permissions?.includes(permission)),
    [user],
  );

  return (
    <AuthContext.Provider value={{ user, loading, login, signup, logout, can }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

/** The raw token, for the WebSocket URL (browsers cannot set WS headers). */
export function storedToken(): string | null {
  return readStored()?.token ?? null;
}

/**
 * Render an action only for roles that may perform it.
 *
 * It EXPLAINS the refusal rather than silently omitting the control. A button
 * that quietly disappears reads as a broken page; a line saying which role
 * owns this action reads as a system with a deliberate separation of duties --
 * which is the actual design, and worth showing off in a demo.
 */
export function RequirePermission({
  permission,
  children,
  action,
}: {
  permission: string;
  children: ReactNode;
  /** Named in the refusal text, e.g. "approve payments". */
  action?: string;
}) {
  const { user, can } = useAuth();
  if (can(permission)) return <>{children}</>;

  return (
    <div className="rounded-lg border border-outline-variant bg-surface-container-low px-3 py-2.5">
      <p className="flex items-center gap-2 text-body-sm text-on-surface-variant">
        <span className="material-symbols-outlined !text-[16px]">lock</span>
        <span>
          You are signed in as <strong>{ROLE_LABEL[user?.role ?? ""] ?? user?.role}</strong>,
          which cannot {action ?? "perform this action"}.
        </span>
      </p>
    </div>
  );
}
