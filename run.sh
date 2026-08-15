#!/usr/bin/env bash
#
# Start (or stop) the whole Inbound-to-Pay stack locally.
#
#   ./run.sh start     bring everything up
#   ./run.sh stop      stop the app processes (leaves Postgres/Redis running)
#   ./run.sh status    show what is up and healthy
#   ./run.sh logs      tail every service log
#   ./run.sh reseed    wipe and re-seed the database
#   ./run.sh migrate   apply pending schema migrations to a running database
#
# Ports: yard 8001, procurement 8002, gateway 8003, simulator 8004, frontend 5173.
# Postgres is on host port 5435 (not 5432) because dev machines commonly
# already run a native Postgres -- see docker-compose.yml.

set -euo pipefail
cd "$(dirname "$0")"

VENV=./.venv/bin
LOGDIR=/tmp/inbound-to-pay
mkdir -p "$LOGDIR"

pids() { pgrep -f "$1" 2>/dev/null || true; }

wait_for() {  # wait_for <url> <name>
  for _ in $(seq 1 40); do
    if curl -sf --max-time 2 "$1" >/dev/null 2>&1; then
      echo "  ✓ $2"
      return 0
    fi
    sleep 0.5
  done
  echo "  ✗ $2 did not become healthy — see $LOGDIR"
  return 1
}

start() {
  echo "▸ infrastructure"
  docker compose up -d postgres redis >/dev/null 2>&1
  for _ in $(seq 1 40); do
    docker compose exec -T postgres pg_isready -U postgres >/dev/null 2>&1 && break
    sleep 0.5
  done
  echo "  ✓ postgres + redis"

  echo "▸ services"
  ( cd backend && nohup "../$VENV/python" -m uvicorn services.yard_api.main:app \
      --host 127.0.0.1 --port 8001 > "$LOGDIR/yard_api.log" 2>&1 & )
  ( cd backend && nohup "../$VENV/python" -m uvicorn services.procurement_api.main:app \
      --host 127.0.0.1 --port 8002 > "$LOGDIR/procurement_api.log" 2>&1 & )
  ( cd backend && nohup "../$VENV/python" -m uvicorn services.dashboard_gateway.main:app \
      --host 127.0.0.1 --port 8003 > "$LOGDIR/gateway.log" 2>&1 & )
  nohup "$VENV/python" backend/services/dock_worker/main.py  > "$LOGDIR/dock_worker.log" 2>&1 &
  nohup "$VENV/python" backend/services/match_worker/main.py > "$LOGDIR/match_worker.log" 2>&1 &

  wait_for http://127.0.0.1:8001/health "yard-api      :8001"
  wait_for http://127.0.0.1:8002/health "procurement   :8002"
  wait_for http://127.0.0.1:8003/health "gateway       :8003"

  # supplier-agent drives the yard and procurement HTTP APIs, so it starts only
  # after both are answering -- otherwise its first PO_CREATED would fail on a
  # connection refused and sit waiting for XAUTOCLAIM to retry it.
  nohup "$VENV/python" backend/services/supplier_agent/main.py > "$LOGDIR/supplier_agent.log" 2>&1 &
  ( cd backend && nohup "../$VENV/python" -m uvicorn services.simulator.main:app \
      --host 127.0.0.1 --port 8004 > "$LOGDIR/simulator.log" 2>&1 & )
  wait_for http://127.0.0.1:8004/health "simulator     :8004"

  [ -n "$(pids dock_worker/main.py)"  ] && echo "  ✓ dock-worker"
  [ -n "$(pids match_worker/main.py)" ] && echo "  ✓ match-worker"
  [ -n "$(pids supplier_agent/main.py)" ] && echo "  ✓ supplier-agent"

  echo "▸ frontend"
  ( cd frontend/app && nohup npm run dev > "$LOGDIR/vite.log" 2>&1 & )
  wait_for http://localhost:5173 "frontend      :5173"

  cat <<EOF

  Open  →  http://localhost:5173

  API docs (interactive, click "Try it out"):
    Yard API        http://127.0.0.1:8001/docs
    Procurement API http://127.0.0.1:8002/docs
    Gateway         http://127.0.0.1:8003/docs
    Simulator       http://127.0.0.1:8004/docs   (POST /sim/start to run the yard)

  Logs: $LOGDIR
EOF
}

stop() {
  for p in "uvicorn services.yard_api" "uvicorn services.procurement_api" \
           "uvicorn services.dashboard_gateway" "dock_worker/main.py" \
           "match_worker/main.py" "supplier_agent/main.py" \
           "uvicorn services.simulator" "vite"; do
    ids=$(pids "$p")
    [ -n "$ids" ] && kill $ids 2>/dev/null && echo "  stopped $p" || true
  done
  echo "Postgres and Redis left running (docker compose down to stop them)."
}

status() {
  for port in 8001 8002 8003 8004; do
    printf "  :%s  " "$port"
    curl -sf --max-time 2 "http://127.0.0.1:$port/health" || echo "DOWN"
    echo
  done
  printf "  :5173 "; curl -sf -o /dev/null -w "HTTP %{http_code}\n" http://localhost:5173 || echo "DOWN"
  printf "  dock-worker  "; [ -n "$(pids dock_worker/main.py)" ] && echo "up" || echo "DOWN"
  printf "  match-worker "; [ -n "$(pids match_worker/main.py)" ] && echo "up" || echo "DOWN"
  printf "  supplier-agent "; [ -n "$(pids supplier_agent/main.py)" ] && echo "up" || echo "DOWN"
}

migrate() {
  # docker-compose mounts schema.sql as an initdb script, which Postgres runs
  # ONLY on an empty data directory -- an already-seeded database never sees a
  # schema.sql edit. These files carry the same delta to a live database and
  # are all idempotent, so re-running this is safe.
  echo "▸ migrations"
  for f in backend/migrations/*.sql; do
    [ -e "$f" ] || continue
    printf "  %s ... " "$(basename "$f")"
    if docker compose exec -T postgres psql -U postgres -d inbound_test \
         -v ON_ERROR_STOP=1 -q < "$f" >/dev/null 2>&1; then
      echo "ok"
    else
      echo "FAILED"; exit 1
    fi
  done
  # Gives the seeded demo users their v5 login credentials. --master-only
  # touches reference data only; no PO chains or traffic are regenerated.
  echo "▸ demo logins"
  "$VENV/python" backend/seed/seed.py --master-only
}

case "${1:-start}" in
  start)  start ;;
  stop)   stop ;;
  status) status ;;
  logs)   tail -f "$LOGDIR"/*.log ;;
  reseed) "$VENV/python" backend/seed/seed.py --reset ;;
  migrate) migrate ;;
  *)      echo "usage: $0 {start|stop|status|logs|reseed|migrate}"; exit 1 ;;
esac
