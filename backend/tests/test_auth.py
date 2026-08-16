"""
Auth + role enforcement, tested against the REAL running stack.

Per CLAUDE.md's testing discipline: this makes actual HTTP requests to the
three services against live Postgres and Redis. It does not mock the database
or import the app in-process, because the thing being tested -- a token issued
by one service being accepted by two others -- only exists across processes.

Run:  ./run.sh start && ./.venv/bin/python -m pytest backend/tests/test_auth.py -v

Every request here is either a read or a write against a deliberately
nonexistent entity id. A 404 from a write endpoint therefore means "permission
granted, then the handler rejected the fake id" -- which is exactly the signal
we want, and it leaves no rows behind. Nothing in this file mutates seeded data.
"""

import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

YARD = os.environ.get("YARD_API", "http://127.0.0.1:8001")
PROCUREMENT = os.environ.get("PROCUREMENT_API", "http://127.0.0.1:8002")
GATEWAY = os.environ.get("GATEWAY_API", "http://127.0.0.1:8003")

DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "inbound2026")
ACCOUNTS = {
    "operator": "shubham@cognisupply.in",
    "procurement": "sachin@cognisupply.in",
    "finance": "serohn@cognisupply.in",
    "admin": "baiju@cognisupply.in",
}

# Ids that cannot exist, so a permitted write still changes nothing.
GHOST = "-DOES-NOT-EXIST"


def _login(email: str, password: str = DEMO_PASSWORD) -> httpx.Response:
    return httpx.post(f"{GATEWAY}/auth/login",
                      json={"email": email, "password": password}, timeout=15)


@pytest.fixture(scope="session")
def tokens() -> dict[str, str]:
    """One token per role. Skips the whole module if the stack is not up."""
    try:
        httpx.get(f"{GATEWAY}/health", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.skip(f"stack not running ({exc}); start it with ./run.sh start")

    out = {}
    for role, email in ACCOUNTS.items():
        res = _login(email)
        if res.status_code != 200:
            pytest.skip(f"demo account {email} cannot sign in ({res.status_code}); "
                        "run ./run.sh migrate")
        out[role] = res.json()["token"]
    return out


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# Marker text on every row the permission probe is forced to create. Most write
# endpoints below are probed with a nonexistent entity id and so write nothing,
# but POST /requisitions takes no id -- a permitted role genuinely creates a
# requisition. Tagging those rows is what lets the fixture below guarantee the
# suite is non-destructive rather than merely intending to be.
PROBE_MARKER = "permission probe, never written"


@pytest.fixture(scope="session", autouse=True)
def _clean_probe_rows():
    """
    Delete every row this suite created, however it exits.

    A test suite that leaves rows behind quietly corrupts the seeded demo data
    it runs against -- the KPI tiles are computed from these tables, so a
    handful of orphan requisitions moves numbers that are supposed to be
    measured (README §8).
    """
    yield
    try:
        from shared.db import get_conn

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM requisitions WHERE raw_text = %s",
                            (PROBE_MARKER,))
                ids = [r[0] for r in cur.fetchall()]
                if ids:
                    cur.execute("DELETE FROM event_log WHERE entity_type='requisition' "
                                "AND entity_id = ANY(%s)", (ids,))
                    cur.execute("DELETE FROM requisitions WHERE id = ANY(%s)", (ids,))
            conn.commit()
        if ids:
            print(f"\ncleaned up {len(ids)} probe requisition(s): {', '.join(ids)}")
    except Exception as exc:  # never fail a green run on cleanup
        print(f"\nWARNING: could not clean up probe rows: {exc}")


# ─────────────────────────────────────────────
# Public surface
# ─────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    f"{GATEWAY}/health", f"{YARD}/health", f"{PROCUREMENT}/health",
    f"{GATEWAY}/auth/roles",
])
def test_public_paths_need_no_token(url):
    assert httpx.get(url, timeout=10).status_code == 200


def test_customer_tracker_stays_public():
    """BUILD_PLAN §161: a supplier with a tracking number must not need an account."""
    res = httpx.get(f"{GATEWAY}/track/TRL-1001", timeout=10)
    assert res.status_code != 401


@pytest.mark.parametrize("url", [
    f"{GATEWAY}/dashboard/overview",
    f"{YARD}/yard-status",
    f"{PROCUREMENT}/purchase-orders",
])
def test_reads_require_a_token(url):
    assert httpx.get(url, timeout=10).status_code == 401


def test_reads_succeed_for_every_role(tokens):
    for role, token in tokens.items():
        res = httpx.get(f"{GATEWAY}/dashboard/overview", headers=auth(token), timeout=15)
        assert res.status_code == 200, f"{role} cannot read the dashboard"


# ─────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────

def test_wrong_password_and_unknown_email_are_indistinguishable():
    """Anything that tells them apart is an account-enumeration oracle."""
    wrong = _login("shubham@cognisupply.in", "not-the-password")
    unknown = _login(f"{uuid.uuid4().hex}@nowhere.dev", "not-the-password")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_email_is_case_insensitive():
    assert _login("SHUBHAM@CogniSupply.IN").status_code == 200


def test_tampered_token_is_rejected(tokens):
    res = httpx.get(f"{GATEWAY}/dashboard/overview",
                    headers=auth(tokens["admin"] + "x"), timeout=10)
    assert res.status_code == 401


def test_malformed_authorization_header_is_rejected(tokens):
    for value in ("", "Bearer", tokens["admin"], f"Basic {tokens['admin']}"):
        res = httpx.get(f"{GATEWAY}/dashboard/overview",
                        headers={"Authorization": value}, timeout=10)
        assert res.status_code == 401, f"accepted {value!r}"


def test_me_reports_the_role_and_its_permissions(tokens):
    res = httpx.get(f"{GATEWAY}/auth/me", headers=auth(tokens["finance"]), timeout=10)
    assert res.status_code == 200
    body = res.json()
    assert body["role"] == "finance"
    assert body["role_changed"] is False
    assert "payment:write" in body["permissions"]
    assert "yard:write" not in body["permissions"]


# ─────────────────────────────────────────────
# Signup
# ─────────────────────────────────────────────

def test_signup_cannot_self_assign_admin():
    res = httpx.post(f"{GATEWAY}/auth/signup", timeout=15, json={
        "name": "Escalation Attempt", "email": f"{uuid.uuid4().hex}@test.dev",
        "password": "a-good-password", "role": "admin",
    })
    assert res.status_code == 422


def test_signup_cannot_self_assign_system():
    res = httpx.post(f"{GATEWAY}/auth/signup", timeout=15, json={
        "name": "Fake Agent", "email": f"{uuid.uuid4().hex}@test.dev",
        "password": "a-good-password", "role": "system",
    })
    assert res.status_code == 422


def test_signup_rejects_a_short_password():
    res = httpx.post(f"{GATEWAY}/auth/signup", timeout=15, json={
        "name": "Too Short", "email": f"{uuid.uuid4().hex}@test.dev",
        "password": "short", "role": "operator",
    })
    assert res.status_code == 422


def test_signup_rejects_a_duplicate_email():
    res = httpx.post(f"{GATEWAY}/auth/signup", timeout=15, json={
        "name": "Impostor", "email": "shubham@cognisupply.in",
        "password": "a-good-password", "role": "operator",
    })
    assert res.status_code == 409


def test_service_account_cannot_sign_in():
    """USR-000 owns rows in payments.approved_by but is not an identity."""
    assert _login("agent@cognisupply.in").status_code == 401


# ─────────────────────────────────────────────
# The capability matrix (api-contract.md §v5.1)
# ─────────────────────────────────────────────

# (label, method+url, body, {role: allowed?})
# allowed=True asserts NOT 403 (the fake id then yields 404/409/422);
# allowed=False asserts exactly 403.
MATRIX = [
    ("yard: create shipment", f"{YARD}/shipments", {"po_id": f"PO{GHOST}"},
     {"operator": True, "procurement": False, "finance": False, "admin": True}),
    ("yard: trailer arrive", f"{YARD}/trailers/TRL{GHOST}/arrive", {},
     {"operator": True, "procurement": False, "finance": False, "admin": True}),
    ("yard: dock reassign", f"{YARD}/dock-assignments/DA{GHOST}/reassign",
     {"new_dock_id": "DOCK-02"},
     {"operator": True, "procurement": False, "finance": False, "admin": True}),
    # The only entry without a ghost id -- permitted roles really do write a
    # row here, tagged with PROBE_MARKER and removed by _clean_probe_rows.
    ("procurement: raise requisition", f"{PROCUREMENT}/requisitions",
     {"raw_text": PROBE_MARKER},
     {"operator": False, "procurement": True, "finance": False, "admin": True}),
    ("exception: assign", f"{PROCUREMENT}/exceptions/EXC{GHOST}/assign",
     {"assigned_to": "USR-003"},
     {"operator": False, "procurement": True, "finance": True, "admin": True}),
    ("exception: resolve", f"{PROCUREMENT}/exceptions/EXC{GHOST}/resolve",
     {"resolution": "APPROVE"},
     {"operator": False, "procurement": False, "finance": True, "admin": True}),
    ("payment: settle", f"{PROCUREMENT}/payments/PAY{GHOST}/pay", {},
     {"operator": False, "procurement": False, "finance": True, "admin": True}),
    ("alert: acknowledge", f"{GATEWAY}/alerts/ALT{GHOST}/acknowledge", {},
     {"operator": True, "procurement": True, "finance": True, "admin": True}),
    ("admin: change a role", f"{GATEWAY}/auth/users/USR{GHOST}/role", {"role": "finance"},
     {"operator": False, "procurement": False, "finance": False, "admin": True}),
]


@pytest.mark.parametrize("label,url,body,expected", MATRIX,
                         ids=[m[0] for m in MATRIX])
def test_capability_matrix(tokens, label, url, body, expected):
    for role, allowed in expected.items():
        res = httpx.post(url, json=body, headers=auth(tokens[role]), timeout=20)
        if allowed:
            assert res.status_code != 403, (
                f"{role} should be permitted to {label} but got 403"
            )
        else:
            assert res.status_code == 403, (
                f"{role} must NOT be permitted to {label}, got {res.status_code}"
            )


@pytest.mark.parametrize("label,url,body,_expected", MATRIX,
                         ids=[m[0] for m in MATRIX])
def test_every_write_rejects_an_anonymous_caller(label, url, body, _expected):
    assert httpx.post(url, json=body, timeout=20).status_code == 401


# ─────────────────────────────────────────────
# The acting user comes from the token (api-contract.md §v5.2)
# ─────────────────────────────────────────────

def test_requisition_records_the_token_holder_not_the_body(tokens):
    """
    A spoofed requested_by must be ignored. This one DOES write a row -- it is
    the only way to prove the server used the token -- so it cleans up after
    itself.
    """
    res = httpx.post(f"{PROCUREMENT}/requisitions", timeout=60,
                     headers=auth(tokens["procurement"]),
                     json={"raw_text": "2 units of Grade 8 Hex Bolt M12 for Bhiwandi",
                           "requested_by": "USR-001"})  # admin: a lie
    assert res.status_code == 201
    req_id = res.json()["id"]

    try:
        detail = httpx.get(f"{PROCUREMENT}/requisitions/{req_id}",
                           headers=auth(tokens["procurement"]), timeout=15).json()
        # The procurement account is USR-003; the body claimed USR-001.
        assert detail["requisition"]["requested_by"] == "USR-003", (
            "server trusted the body's requested_by instead of the bearer token"
        )
    finally:
        from shared.db import get_conn

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM event_log WHERE entity_type='requisition' "
                            "AND entity_id=%s", (req_id,))
                cur.execute("DELETE FROM requisitions WHERE id=%s", (req_id,))
            conn.commit()
