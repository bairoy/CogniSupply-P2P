"""
Auth endpoints (signup, login, whoami, role administration).

Mounted on the Dashboard Gateway (:8003) only. There is exactly ONE issuer of
tokens; Yard API and Procurement API verify signatures locally with the same
JWT_SECRET and never call this service. That is the whole point of the
stateless-token choice in shared/auth.py -- auth is not a network hop.

Why the gateway and not a fourth service: the gateway already exists purely to
serve the frontend across both domains, and login belongs to neither Yard nor
Procurement. A separate auth service would be one more port, one more process
in run.sh, and one more thing to be down during a demo, for no isolation
benefit when the secret is shared anyway.

These routes write to `users` and `audit_logs` only. They emit NO events:
event_type/entity_type are a locked vocabulary (redis-contract.md §3/§4) with
no auth entries, and CLAUDE.md forbids inventing one in code. A login is not a
supply-chain domain event -- audit_logs is the correct home for it, and is
exactly what that table's free-TEXT `action` column is for.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from shared.auth import (  # noqa: E402
    LOGIN_ROLES,
    PERM_ADMIN_USERS,
    SELF_SIGNUP_ROLES,
    AuthUser,
    current_user,
    hash_password,
    issue_token,
    normalize_email,
    permissions_for,
    require,
    validate_password,
    verify_password,
)
from shared.db import get_conn  # noqa: E402

log = logging.getLogger("auth")

router = APIRouter(prefix="/auth", tags=["auth"])


# ─────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────

class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["Priya Raghavan"])
    email: str = Field(examples=["priya@cognisupply.in"])
    password: str = Field(examples=["yard-ops-2026"])
    role: str = Field(default="operator", examples=["operator"])


class LoginRequest(BaseModel):
    email: str = Field(examples=["priya@cognisupply.in"])
    password: str = Field(examples=["yard-ops-2026"])


class RoleChangeRequest(BaseModel):
    role: str = Field(examples=["finance"])


def _audit(cur, user_id: Optional[str], action: str, entity_id: str, new_value: dict) -> None:
    """
    audit_logs, not event_log. See the module docstring -- no auth event type
    exists in the locked Redis vocabulary, and inventing one is forbidden.
    """
    cur.execute(
        """INSERT INTO audit_logs (user_id, action, entity_type, entity_id, new_value)
           VALUES (%s, %s, 'user', %s, %s::jsonb)""",
        (user_id, action, entity_id, json.dumps(new_value)),
    )


def _session(user_id: str, name: str, role: str, email: Optional[str] = None) -> dict:
    """
    The one response shape the frontend's AuthProvider consumes. It matches
    GET /auth/me field-for-field on purpose: the UI renders the same user chip
    whether the session came from a fresh login or from revalidating a stored
    token, and a field present in one but not the other shows up as the chip
    changing on reload.
    """
    token, expires_in = issue_token(user_id, name, role)
    return {
        "token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "user": {
            "id": user_id,
            "name": name,
            "email": email,
            "role": role,
            # Sent so the UI can hide actions it would only get a 403 for.
            # The server re-checks every one of these on every request -- this
            # list is a convenience for rendering, never the enforcement point.
            "permissions": sorted(permissions_for(role)),
        },
    }


# ─────────────────────────────────────────────
# POST /auth/signup   (public)
# ─────────────────────────────────────────────

@router.post("/signup", status_code=201)
def signup(body: SignupRequest):
    """
    Self-registration. The new user picks operator / procurement / finance;
    'admin' and 'system' are not self-assignable (shared/auth.SELF_SIGNUP_ROLES).

    Returns a token, so signing up logs you straight in -- one fewer step
    between a judge opening the app and seeing a role-specific dashboard.
    """
    if body.role not in SELF_SIGNUP_ROLES:
        raise HTTPException(
            422,
            f"role must be one of {', '.join(SELF_SIGNUP_ROLES)} "
            "('admin' is granted by an existing admin, not chosen at signup)",
        )
    email = normalize_email(body.email)
    validate_password(body.password)
    name = body.name.strip()
    password_hash = hash_password(body.password)

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Checked here for a clean 409, and guaranteed by the
            # uq_users_email_lower unique index for the concurrent case --
            # two simultaneous signups cannot both win.
            cur.execute("SELECT 1 FROM users WHERE lower(email) = %s", (email,))
            if cur.fetchone():
                raise HTTPException(409, f"an account already exists for {email}")

            cur.execute("SELECT nextval('user_id_seq')")
            user_id = f"USR-{cur.fetchone()[0]}"

            try:
                cur.execute(
                    """INSERT INTO users (id, name, role, email, password_hash, is_active)
                       VALUES (%s, %s, %s, %s, %s, TRUE)""",
                    (user_id, name, body.role, email, password_hash),
                )
            except Exception as exc:  # unique index tripped by a concurrent signup
                conn.rollback()
                if "uq_users_email_lower" in str(exc):
                    raise HTTPException(409, f"an account already exists for {email}")
                raise

            _audit(cur, user_id, "user_signup", user_id, {"role": body.role, "email": email})
        conn.commit()

    log.info("signup %s (%s) as %s", user_id, email, body.role)
    return _session(user_id, name, body.role, email)


# ─────────────────────────────────────────────
# POST /auth/login   (public)
# ─────────────────────────────────────────────

@router.post("/login")
def login(body: LoginRequest):
    """
    Every failure path returns the SAME 401 message. Distinguishing "no such
    email" from "wrong password" hands an attacker a way to enumerate who has
    an account -- and a demo that talks about auditability should not fail
    that on its own login screen.
    """
    invalid = HTTPException(401, "invalid email or password")
    try:
        email = normalize_email(body.email)
    except HTTPException:
        raise invalid

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, name, role, password_hash, is_active
                   FROM users WHERE lower(email) = %s""",
                (email,),
            )
            row = cur.fetchone()

            # verify_password() is still called on a miss so a nonexistent
            # email costs the same ~100ms of bcrypt as a real one; returning
            # instantly would leak account existence through response timing.
            stored_hash = row[3] if row else None
            if not verify_password(body.password, stored_hash):
                conn.rollback()
                raise invalid

            user_id, name, role, _, is_active = row
            if not is_active:
                conn.rollback()
                raise HTTPException(403, "this account has been deactivated")
            if role not in LOGIN_ROLES:
                # The USR-000 service account. It legitimately owns rows in
                # payments.approved_by, but nobody signs in as it.
                conn.rollback()
                raise HTTPException(403, f"'{role}' accounts cannot sign in")

            cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))
            _audit(cur, user_id, "user_login", user_id, {"role": role})
        conn.commit()

    log.info("login %s (%s)", user_id, role)
    return _session(user_id, name, role, email)


# ─────────────────────────────────────────────
# GET /auth/me
# ─────────────────────────────────────────────

@router.get("/me")
def me(request: Request, user: AuthUser = Depends(current_user)):
    """
    Who the bearer token says I am, re-checked against the database.

    The frontend calls this on page load to decide whether a token surviving
    in localStorage is still good. It re-reads `users` rather than trusting
    the token's claims alone, so an account deactivated or re-roled since the
    token was issued is caught here instead of at the next write attempt.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, role, is_active, email FROM users WHERE id = %s", (user.id,)
            )
            row = cur.fetchone()
        conn.rollback()

    if row is None:
        raise HTTPException(401, "user no longer exists")
    name, role, is_active, email = row
    if not is_active:
        raise HTTPException(403, "this account has been deactivated")

    return {
        "id": user.id,
        "name": name,
        "email": email,
        "role": role,
        "permissions": sorted(permissions_for(role)),
        # True when an admin changed this user's role after the token was
        # issued. The token still carries the OLD role and is what the other
        # services enforce on, so the UI must prompt a re-login rather than
        # quietly showing controls that will 403.
        "role_changed": role != user.role,
    }


# ─────────────────────────────────────────────
# GET /auth/roles   (public — populates the signup dropdown)
# ─────────────────────────────────────────────

@router.get("/roles")
def roles():
    return {
        "signup_roles": [
            {"role": "operator",
             "label": "Yard Operator",
             "description": "Move trailers and dock doors, record goods receipts"},
            {"role": "procurement",
             "label": "Procurement",
             "description": "Raise requisitions, select suppliers, take invoices in"},
            {"role": "finance",
             "label": "Finance",
             "description": "Resolve match exceptions and release payments"},
        ],
        "all_roles": list(LOGIN_ROLES),
    }


# ─────────────────────────────────────────────
# GET /auth/users
# ─────────────────────────────────────────────

@router.get("/users")
def list_users(user: AuthUser = Depends(current_user), role: Optional[str] = None):
    """
    The directory behind the "assign this exception to..." picker, which is why
    it is readable by any authenticated user and not admin-only.

    Email is returned only to admins -- an assignee dropdown needs a name and a
    role, not everyone's contact details.
    """
    include_email = user.can(PERM_ADMIN_USERS)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, name, role, is_active, email, last_login_at
                   FROM users
                   WHERE (%s IS NULL OR role = %s) AND is_active
                   ORDER BY role, name""",
                (role, role),
            )
            rows = cur.fetchall()
        conn.rollback()

    return {"users": [
        {
            "id": r[0], "name": r[1], "role": r[2], "is_active": r[3],
            **({"email": r[4],
                "last_login_at": r[5].isoformat() if r[5] else None} if include_email else {}),
        }
        for r in rows
    ]}


# ─────────────────────────────────────────────
# POST /auth/users/{user_id}/role   (admin only)
# ─────────────────────────────────────────────

@router.post("/users/{user_id}/role", dependencies=[Depends(require(PERM_ADMIN_USERS))])
def set_role(user_id: str, body: RoleChangeRequest, actor: AuthUser = Depends(current_user)):
    """
    The only way to become an admin. Takes effect on the target's next login,
    because the role they act under lives in their signed token (shared/auth.py
    §2) -- GET /auth/me reports role_changed so the UI can prompt for it.
    """
    if body.role not in LOGIN_ROLES:
        raise HTTPException(422, f"role must be one of {', '.join(LOGIN_ROLES)}")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, f"user {user_id} not found")
            old_role = row[0]
            if old_role not in LOGIN_ROLES:
                raise HTTPException(409, f"'{old_role}' accounts cannot be re-roled")

            cur.execute("UPDATE users SET role = %s WHERE id = %s", (body.role, user_id))
            cur.execute(
                """INSERT INTO audit_logs (user_id, action, entity_type, entity_id,
                                           old_value, new_value)
                   VALUES (%s, 'user_role_changed', 'user', %s, %s::jsonb, %s::jsonb)""",
                (actor.id, user_id,
                 json.dumps({"role": old_role}),
                 json.dumps({"role": body.role})),
            )
        conn.commit()

    return {"id": user_id, "role": body.role, "previous_role": old_role,
            "note": "takes effect at the user's next sign-in"}
