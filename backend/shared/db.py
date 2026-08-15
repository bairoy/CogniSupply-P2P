"""
Shared PostgreSQL connection helper. Every service (Yard API, Procurement
API, Dashboard Gateway, both workers) imports get_conn() from here instead
of configuring its own connection — one place to change the connection
string, matching how event_bus.py centralizes the Redis connection.

Each request/worker-loop iteration should get ONE connection and use it
for both the domain write and record_event() — that's what makes the
"commit both together" transaction boundary in api-contract.md possible.
A connection pool (not a single global connection) is used so concurrent
FastAPI requests don't serialize behind each other.
"""

import os
from contextlib import contextmanager

from psycopg2.pool import ThreadedConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    # Host port 5435, not 5432 -- see docker-compose.yml. Developer machines
    # commonly already run a native Postgres on 5432.
    "postgresql://postgres:test@localhost:5435/inbound_test",
)

# ThreadedConnectionPool, NOT SimpleConnectionPool. FastAPI runs `def`
# (non-async) endpoints in a threadpool, so several threads call getconn() /
# putconn() concurrently. SimpleConnectionPool does no locking around its
# internal free-list and can hand the same connection to two threads.
_pool = ThreadedConnectionPool(1, 20, DATABASE_URL)


@contextmanager
def get_conn():
    """
    Usage:
        with get_conn() as conn:
            ... domain write ...
            record_event(conn, ...)
            conn.commit()
            publish_to_redis(conn, ...)

    The connection is returned to the pool on exit. It does NOT
    auto-commit or auto-rollback on a clean exit — the caller controls
    the transaction boundary explicitly, per the locked write-path rule.
    On an exception, the connection is rolled back before being returned,
    so a half-open transaction never leaks back into the pool.
    """
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
