"""
Authentication and role-based authorization, shared by every HTTP service.

Design decisions, and why:

1. STATELESS HS256 JWTs, not server-side sessions. Three separate FastAPI
   processes (yard :8001, procurement :8002, gateway :8003) all need to know
   who the caller is. A session table would make every request on every
   service a database round-trip, and a session cache would mean Redis state
   -- explicitly ruled out (README §1). A signed token that each service
   verifies locally with the same JWT_SECRET costs nothing per request and
   adds no shared mutable state. Only /auth/login and /auth/signup touch the
   users table; every other request verifies the signature and moves on.

2. THE TOKEN CARRIES THE ROLE. It is signed, so the client cannot edit it.
   The cost is that a role change takes effect at the user's next login
   rather than instantly -- acceptable, and the honest trade for (1). Tokens
   are short-lived (JWT_TTL_HOURS, default 12) to bound that window.

3. bcrypt FOR PASSWORDS, never a plain hash. Passwords are compared with
   bcrypt.checkpw, which is constant-time; a plaintext or SHA-256 password
   column would be indefensible in a demo that talks about auditability.

4. PERMISSIONS ARE NAMED CAPABILITIES, not role checks scattered in handlers.
   Endpoints declare what they need ("yard:write"), not who may do it
   ("operator or admin"). Adding a role later means editing ROLE_PERMISSIONS
   here, not hunting through three services.

Enforcement model (see docs/api-contract.md §0):
  - Reads require a valid token.
  - Writes require a valid token AND the endpoint's capability.
  - GET /health, POST /auth/login, POST /auth/signup and GET /track/{ref}
    are public -- /track is the customer-facing tracker, deliberately
    unauthenticated per docs/BUILD_PLAN.md §161.
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt
import jwt
from fastapi import HTTPException, Request

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

# A default exists so the stack still boots from a bare checkout, but it is
# a DEV secret and the services log a warning when it is in use. Set
# JWT_SECRET in .env for anything that outlives a laptop.
DEV_SECRET = "dev-only-inbound-to-pay-secret-change-me"
JWT_SECRET = os.environ.get("JWT_SECRET") or DEV_SECRET
JWT_ALGORITHM = "HS256"
JWT_TTL_HOURS = int(os.environ.get("JWT_TTL_HOURS", "12"))

# ─────────────────────────────────────────────
# Roles and permissions
# ─────────────────────────────────────────────

# The role vocabulary is the one already in schema.sql's users.role comment.
# Like status values, it is APPEND-ONLY: never rename or repurpose one.
ROLE_OPERATOR = "operator"
ROLE_PROCUREMENT = "procurement"
ROLE_FINANCE = "finance"
ROLE_ADMIN = "admin"
ROLE_SYSTEM = "system"

# Roles a person may hold. 'system' is excluded on purpose: USR-000 is the
# service account that touchless approvals are recorded against (it is the FK
# target in payments.approved_by), not an identity anyone logs in as.
LOGIN_ROLES = (ROLE_OPERATOR, ROLE_PROCUREMENT, ROLE_FINANCE, ROLE_ADMIN)

# Roles a person may choose for themselves at signup. 'admin' is excluded:
# self-elevation to admin would make the whole matrix below decorative. An
# existing admin grants it via POST /auth/users/{id}/role.
SELF_SIGNUP_ROLES = (ROLE_OPERATOR, ROLE_PROCUREMENT, ROLE_FINANCE)

# Capabilities, one per kind of state change in the system. Reads are not
# listed -- any authenticated user may read, which keeps every dashboard
# screen populated for every role while still gating who can act.
PERM_YARD_WRITE = "yard:write"            # shipments, trailers, tracking, arrive, dock, unload, reassign
# v7. Separate from yard:write rather than folded into it, even though the same
# two roles hold both today: outbound is the half of the yard a site is most
# likely to hand to a 3PL, and a capability you cannot grant separately is one
# you cannot delegate separately. Splitting it later would mean re-issuing every
# operator's token; splitting it now costs one line.
PERM_OUTBOUND_WRITE = "outbound:write"    # outbound orders, staging, dispatch, load, deliver
PERM_PROCUREMENT_WRITE = "procurement:write"  # requisitions, supplier selection -> PO
PERM_INVOICE_WRITE = "invoice:write"      # invoice intake (manual JSON + OCR upload)
PERM_EXCEPTION_ASSIGN = "exception:assign"
PERM_EXCEPTION_RESOLVE = "exception:resolve"  # approving an override the match engine refused
PERM_PAYMENT_WRITE = "payment:write"
PERM_ALERT_ACK = "alert:ack"              # acknowledging an alert is low-stakes, anyone operational
PERM_ADMIN_USERS = "admin:users"

ROLE_PERMISSIONS: dict[str, set[str]] = {
    # E2 side: the yard operator moves trucks and doors, and nothing else.
    ROLE_OPERATOR: {
        PERM_YARD_WRITE,
        PERM_OUTBOUND_WRITE,   # v7 -- the people who move trucks move them both ways
        PERM_ALERT_ACK,
    },
    # PR2 buy side: raises demand, picks suppliers, takes invoices in, and can
    # route an exception to the right person -- but cannot approve the money.
    ROLE_PROCUREMENT: {
        PERM_PROCUREMENT_WRITE,
        PERM_INVOICE_WRITE,
        PERM_EXCEPTION_ASSIGN,
        PERM_ALERT_ACK,
    },
    # PR2 pay side: owns the money decisions. Resolving an exception creates a
    # payment (procurement_api.resolve_exception), which is exactly why
    # exception:resolve sits here and not with procurement -- separation of
    # duties between who orders goods and who pays for them.
    ROLE_FINANCE: {
        PERM_INVOICE_WRITE,
        PERM_EXCEPTION_ASSIGN,
        PERM_EXCEPTION_RESOLVE,
        PERM_PAYMENT_WRITE,
        PERM_ALERT_ACK,
    },
    ROLE_ADMIN: {
        PERM_YARD_WRITE,
        PERM_OUTBOUND_WRITE,
        PERM_PROCUREMENT_WRITE,
        PERM_INVOICE_WRITE,
        PERM_EXCEPTION_ASSIGN,
        PERM_EXCEPTION_RESOLVE,
        PERM_PAYMENT_WRITE,
        PERM_ALERT_ACK,
        PERM_ADMIN_USERS,
    },
    # Never issued a token; listed so permissions_for() has no missing key.
    ROLE_SYSTEM: set(),
}


def permissions_for(role: str) -> set[str]:
    """Unknown roles get nothing, rather than everything."""
    return ROLE_PERMISSIONS.get(role, set())


# ─────────────────────────────────────────────
# Passwords
# ─────────────────────────────────────────────

# bcrypt truncates silently at 72 BYTES. Rejecting long passwords outright is
# better than accepting one and only checking its first 72 bytes at login.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 8

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    """
    False -- never an exception -- for a user with no password set (NULL hash
    on a pre-v5 row or the service account) and for a malformed stored hash.
    A login route must not be able to 500 its way into telling an attacker
    which emails exist.
    """
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def validate_password(password: str) -> None:
    """Raises HTTPException(422) with a message the signup form can show."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(422, f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise HTTPException(422, f"password must be at most {MAX_PASSWORD_BYTES} bytes")


def normalize_email(email: str) -> str:
    """
    Lowercased and stripped, matching the uq_users_email_lower index exactly.
    Storing the normalized form means the index and the lookup can never
    disagree about what counts as a duplicate.
    """
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "invalid email address")
    return email


# ─────────────────────────────────────────────
# Tokens
# ─────────────────────────────────────────────

def issue_token(user_id: str, name: str, role: str) -> tuple[str, int]:
    """Returns (token, expires_in_seconds)."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=JWT_TTL_HOURS)
    payload = {
        "sub": user_id,          # users.id, e.g. 'USR-003'
        "name": name,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM), JWT_TTL_HOURS * 3600


class AuthUser:
    """The authenticated caller, decoded from the token. No DB row attached."""

    __slots__ = ("id", "name", "role", "permissions")

    def __init__(self, user_id: str, name: str, role: str):
        self.id = user_id
        self.name = name
        self.role = role
        self.permissions = permissions_for(role)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def __repr__(self) -> str:
        return f"AuthUser({self.id}, {self.role})"


def decode_token(token: str) -> AuthUser:
    """Raises HTTPException(401) on anything that is not a valid live token."""
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token expired, please sign in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "invalid token")

    user_id = claims.get("sub")
    role = claims.get("role")
    if not user_id or not role:
        raise HTTPException(401, "malformed token")
    return AuthUser(user_id, claims.get("name") or user_id, role)


def user_from_header(authorization: Optional[str]) -> AuthUser:
    """Parses an `Authorization: Bearer <token>` header value."""
    if not authorization:
        raise HTTPException(401, "not authenticated")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "expected 'Authorization: Bearer <token>'")
    return decode_token(token.strip())


# ─────────────────────────────────────────────
# FastAPI wiring
# ─────────────────────────────────────────────

# Paths reachable without a token. Exact matches plus /track/ by prefix.
PUBLIC_PATHS = {
    "/health",
    "/auth/login",
    "/auth/signup",
    "/auth/roles",
    # FastAPI's own docs: /docs is worth keeping open in a hackathon demo so a
    # judge can click "Try it out". Every endpoint it calls still needs a token.
    "/docs",
    "/redoc",
    "/openapi.json",
}
PUBLIC_PREFIXES = ("/track/",)


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def current_user(request: Request) -> AuthUser:
    """
    FastAPI dependency: the authenticated caller.

    Reads what auth_middleware already decoded, so a request decodes its token
    exactly once no matter how many dependencies ask for the user.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "not authenticated")
    return user


def require(permission: str):
    """
    FastAPI dependency factory: gate an endpoint on one capability.

        @app.post("/trailers/{id}/dock", dependencies=[Depends(require(PERM_YARD_WRITE))])

    403, not 401, when the caller is authenticated but lacks the capability --
    the distinction is what lets the frontend tell "sign in again" apart from
    "your role cannot do this".
    """

    def dependency(request: Request) -> AuthUser:
        user = current_user(request)
        if not user.can(permission):
            raise HTTPException(
                403,
                f"role '{user.role}' is not permitted to {permission}",
            )
        return user

    return dependency
