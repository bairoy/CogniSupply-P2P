#!/usr/bin/env bash
#
# Demo-day remote control. One word per demo moment, so nothing has to be
# typed from memory in front of an audience.
#
#   ./demo.sh status          what is up, and is the feed running
#   ./demo.sh feed on|off     start / pause the WMS feed  (also a button in the UI)
#   ./demo.sh step            advance the world exactly one tick while paused
#
#   ./demo.sh delay           a truck slips its ETA   -> the yard re-plans
#   ./demo.sh block           a dock goes out of service -> routed around
#   ./demo.sh unblock         capacity returns        -> picked up next re-plan
#   ./demo.sh surge           a burst of arrivals     -> queued by priority
#   ./demo.sh rush            an outbound rush        -> both directions, one solve
#
#   ./demo.sh price           an invoice overcharges  -> exception, not a payment
#   ./demo.sh qty             an invoice over-bills quantity -> exception
#   ./demo.sh missing-po      an unreadable PO ref    -> routed to a human
#
# Everything here is a real state change through a real endpoint. Nothing on
# this menu fakes a screen -- `delay` genuinely posts a late ETA and the dock
# worker genuinely re-solves around it.

set -euo pipefail
cd "$(dirname "$0")"

SIM=http://127.0.0.1:8004
GATEWAY=http://127.0.0.1:8003
EMAIL=${DEMO_EMAIL:-baiju@cognisupply.in}
PASSWORD=${DEMO_PASSWORD:-inbound2026}

die() { echo "  ✗ $1" >&2; exit 1; }

token() {
  curl -sf --max-time 5 -X POST "$GATEWAY/auth/login" \
       -H 'content-type: application/json' \
       -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' \
    || die "login failed -- is the gateway up? (./run.sh status)"
}

# Scenarios are POST-only and authenticated, so /docs cannot fire them without
# a bearer token. That is the whole reason this script exists.
fire() {  # fire <path> [json-body]
  local tok; tok=$(token)
  curl -sf --max-time 15 -X POST "$SIM$1" \
       -H "authorization: Bearer $tok" \
       -H 'content-type: application/json' \
       ${2:+-d "$2"} \
    | python3 -m json.tool \
    || die "$1 failed -- see /tmp/inbound-to-pay/simulator.log"
}

scenario() { fire "/sim/scenario/$1?count=${2:-3}"; }

case "${1:-status}" in
  status)
    ./run.sh status
    echo
    echo "  WMS feed:"
    curl -sf --max-time 5 "$SIM/sim/status" -H "authorization: Bearer $(token)" \
      | python3 -c '
import sys, json
s = json.load(sys.stdin)
print("    running:", s["running"], " ticks:", s["ticks"])
for k, v in sorted(s["actions"].items(), key=lambda kv: -kv[1]):
    print(f"    {k:16} {v}")'
    ;;

  feed)
    case "${2:-}" in
      on)  fire /sim/start ;;
      off) fire /sim/stop ;;
      *)   die "usage: ./demo.sh feed on|off" ;;
    esac
    ;;

  step)       fire /sim/tick ;;

  delay)      scenario delay-trailer        "${2:-3}" ;;
  block)      scenario block-dock           "${2:-1}" ;;
  unblock)    scenario unblock-docks        "${2:-9}" ;;
  surge)      scenario surge-arrivals       "${2:-5}" ;;
  rush)       scenario outbound-rush        "${2:-3}" ;;

  price)      scenario inject-price-mismatch "${2:-1}" ;;
  qty)        scenario inject-qty-mismatch   "${2:-1}" ;;
  missing-po) scenario inject-missing-po     "${2:-1}" ;;

  *) sed -n '3,26p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
