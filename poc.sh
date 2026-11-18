#!/usr/bin/env bash
# Frontend product analytics POC helper.
#   ./poc.sh up       start everything and wait until it's ready
#   ./poc.sh test     run every dashboard query against Loki and show results
#   ./poc.sh status   container status
#   ./poc.sh logs [s] follow logs (e.g. alloy, loadgen)
#   ./poc.sh down     stop (data kept)
#   ./poc.sh reset    stop and delete all data
set -euo pipefail
cd "$(dirname "$0")"

log() { printf '\033[1;34m[poc]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[poc] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

wait_http() { # wait_http <name> <url> <timeout-seconds>
  local i
  for ((i = 0; i < $3; i += 3)); do
    if curl -fsS -o /dev/null "$2" 2>/dev/null; then log "$1 ready"; return 0; fi
    sleep 3
  done
  die "$1 did not become ready at $2. Check: ./poc.sh logs"
}

case "${1:-}" in
  up)
    command -v docker >/dev/null || die "Docker is required"
    docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
    log "Starting Loki, Alloy, Grafana, demo shop and traffic generator"
    docker compose up -d
    wait_http "Loki" "http://127.0.0.1:3100/ready" 180
    wait_http "Grafana" "http://localhost:3000/api/health" 180
    wait_http "Demo shop" "http://localhost:8080/" 60
    wait_http "Insights" "http://localhost:3001/" 60
    log "Seeding traffic (about 300 sessions, then ~20/min live). Dashboard fills in ~30 s"
    cat <<'EOF'

  Grafana     http://localhost:3000            (dashboards: "Product Analytics" + "Product Insights")
  Insights    http://localhost:3001            (replay, funnels, retention, paths, heatmaps)
  Demo shop   http://localhost:8080            (click around, your visits show up too)
  Explore     http://localhost:3000/explore    (ad-hoc LogQL, see docs/QUERIES.md)
  Alloy UI    http://localhost:12345           (pipeline graph)

EOF
    ;;
  test)
    command -v python3 >/dev/null || die "python3 is required for the query test"
    python3 tools/test_queries.py http://127.0.0.1:3100
    ;;
  status) docker compose ps ;;
  logs) shift; docker compose logs -f --tail 100 "$@" ;;
  down) docker compose down ;;
  reset) docker compose down -v; log "All data deleted" ;;
  *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
