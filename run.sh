#!/usr/bin/env bash
#
# Start (or stop) the whole Inbound-to-Pay stack locally.
#
#   ./run.sh start     bring everything up
#   ./run.sh stop      stop the app processes (leaves Postgres/Redis running)
#   ./run.sh status    show what is up and healthy
#   ./run.sh logs      tail every service log
#   ./run.sh reseed    wipe and re-seed the database
#
# Ports: yard 8001, procurement 8002, gateway 8003, frontend 5173.
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
  [ -n "$(pids dock_worker/main.py)"  ] && echo "  ✓ dock-worker"
  [ -n "$(pids match_worker/main.py)" ] && echo "  ✓ match-worker"

  echo "▸ frontend"
  ( cd frontend/app && nohup npm run dev > "$LOGDIR/vite.log" 2>&1 & )
  wait_for http://localhost:5173 "frontend      :5173"

  cat <<EOF

  Open  →  http://localhost:5173

  API docs (interactive, click "Try it out"):
    Yard API        http://127.0.0.1:8001/docs
    Procurement API http://127.0.0.1:8002/docs
    Gateway         http://127.0.0.1:8003/docs

  Logs: $LOGDIR
EOF
}

stop() {
  for p in "uvicorn services.yard_api" "uvicorn services.procurement_api" \
           "uvicorn services.dashboard_gateway" "dock_worker/main.py" \
           "match_worker/main.py" "vite"; do
    ids=$(pids "$p")
    [ -n "$ids" ] && kill $ids 2>/dev/null && echo "  stopped $p" || true
  done
  echo "Postgres and Redis left running (docker compose down to stop them)."
}

status() {
  for port in 8001 8002 8003; do
    printf "  :%s  " "$port"
    curl -sf --max-time 2 "http://127.0.0.1:$port/health" || echo "DOWN"
    echo
  done
  printf "  :5173 "; curl -sf -o /dev/null -w "HTTP %{http_code}\n" http://localhost:5173 || echo "DOWN"
  printf "  dock-worker  "; [ -n "$(pids dock_worker/main.py)" ] && echo "up" || echo "DOWN"
  printf "  match-worker "; [ -n "$(pids match_worker/main.py)" ] && echo "up" || echo "DOWN"
}

case "${1:-start}" in
  start)  start ;;
  stop)   stop ;;
  status) status ;;
  logs)   tail -f "$LOGDIR"/*.log ;;
  reseed) "$VENV/python" backend/seed/seed.py --reset ;;
  *)      echo "usage: $0 {start|stop|status|logs|reseed}"; exit 1 ;;
esac
