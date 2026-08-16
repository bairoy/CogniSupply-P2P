/**
 * Sign in / create account.
 *
 * One screen, two modes, because a hackathon demo should never make a judge
 * hunt for a second page. The role picker is only shown when creating an
 * account -- on sign-in the role comes from the account, not from a dropdown.
 *
 * No seeded-account panel, deliberately: this screen is the product's front
 * door, and a printed list of logins and their shared password is the one thing
 * on it that would read as a test fixture. The accounts still exist -- whoever
 * is driving the demo knows them -- they are just not advertised to whoever
 * else is looking at the screen.
 */

import { FormEvent, useEffect, useState } from "react";
import { GATEWAY } from "../api";
import { useAuth } from "../auth";
import { Icon } from "../components/ui";

interface RoleOption {
  role: string;
  label: string;
  description: string;
}

export default function Login() {
  const { login, signup } = useAuth();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("operator");
  const [roles, setRoles] = useState<RoleOption[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // GET /auth/roles is public precisely so this dropdown can be populated
  // before anyone has a token. The roles a user may self-assign are the
  // server's decision, not a list hardcoded here that could drift from it.
  useEffect(() => {
    fetch(`${GATEWAY}/auth/roles`)
      .then((r) => r.json())
      .then((d) => setRoles(d.signup_roles ?? []))
      .catch(() => setRoles([]));
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "login") await login(email, password);
      else await signup(name, email, password, role);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  const field =
    "w-full rounded-lg border border-outline-variant bg-surface-container-lowest px-3 py-2 " +
    "text-body-md outline-none transition focus:border-primary-container " +
    "focus:ring-2 focus:ring-primary-container/20";

  return (
    <div className="flex min-h-full items-center justify-center bg-surface p-6">
      <div className="w-full max-w-md">
        {/* ---- form ---- */}
        <div className="card-pad flex flex-col gap-5">
          <div className="flex items-center gap-3">
            <div className="grid h-11 w-11 place-items-center rounded-lg bg-primary-container text-on-primary font-bold">
              CS
            </div>
            <div>
              <h1 className="text-headline-lg text-primary leading-tight">CogniSupply P2P</h1>
              <p className="text-body-sm text-on-surface-variant">Enterprise Control Tower</p>
            </div>
          </div>

          {/* mode switch */}
          <div className="flex rounded-lg border border-outline-variant p-1">
            {(["login", "signup"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => {
                  setMode(m);
                  setError(null);
                }}
                className={`flex-1 rounded-md px-3 py-1.5 text-body-md font-medium transition-colors ${
                  mode === m
                    ? "bg-surface-container-high text-on-surface"
                    : "text-on-surface-variant hover:bg-surface-container-low"
                }`}
              >
                {m === "login" ? "Sign in" : "Create account"}
              </button>
            ))}
          </div>

          <form onSubmit={onSubmit} className="flex flex-col gap-4">
            {mode === "signup" && (
              <label className="flex flex-col gap-1.5">
                <span className="label">Full name</span>
                <input
                  className={field}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Aarav Sharma"
                  required
                  autoComplete="name"
                />
              </label>
            )}

            <label className="flex flex-col gap-1.5">
              <span className="label">Work email</span>
              <input
                className={field}
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@cognisupply.in"
                required
                autoComplete="email"
              />
            </label>

            <label className="flex flex-col gap-1.5">
              <span className="label">Password</span>
              <input
                className={field}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
                minLength={mode === "signup" ? 8 : undefined}
                autoComplete={mode === "signup" ? "new-password" : "current-password"}
              />
              {mode === "signup" && (
                <span className="text-body-sm text-on-surface-variant">
                  At least 8 characters.
                </span>
              )}
            </label>

            {mode === "signup" && (
              <label className="flex flex-col gap-1.5">
                <span className="label">Role</span>
                <select
                  className={field}
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                >
                  {roles.map((r) => (
                    <option key={r.role} value={r.role}>
                      {r.label}
                    </option>
                  ))}
                </select>
                <span className="text-body-sm text-on-surface-variant">
                  {roles.find((r) => r.role === role)?.description ??
                    "Determines what you can act on."}{" "}
                  Administrator access is granted by an existing admin.
                </span>
              </label>
            )}

            {error && (
              <div className="rounded-lg border border-error/30 bg-error-container/50 px-3 py-2">
                <p className="text-body-sm text-on-error-container">{error}</p>
              </div>
            )}

            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
                  Working…
                </>
              ) : (
                <>
                  <Icon name={mode === "login" ? "login" : "person_add"} />
                  {mode === "login" ? "Sign in" : "Create account"}
                </>
              )}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
