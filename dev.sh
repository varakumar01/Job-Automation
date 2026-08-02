#!/usr/bin/env bash
# dev.sh — start/stop the local control-panel (backend + frontend dev servers).
#
#   ./dev.sh start     backend (:8000) + frontend (:5178), backgrounded
#   ./dev.sh stop      cut off everything this script started (or find via port)
#   ./dev.sh restart   stop then start
#   ./dev.sh status    show what's running and where
#   ./dev.sh static    build + preview the read-only public dashboard (:4173)
#   ./dev.sh logs      tail both dev-server logs
#
# Env overrides: BACKEND_PORT (default 8000), FRONTEND_PORT (default 5178),
# STATIC_PORT (default 4173).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
mkdir -p "$RUN_DIR"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5178}"
STATIC_PORT="${STATIC_PORT:-4173}"

BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
STATIC_PID_FILE="$RUN_DIR/static.pid"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"
STATIC_LOG="$RUN_DIR/static.log"

_is_alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

# Kill by tracked PID (if any) AND by port pattern — `npm run dev` wraps the
# real vite/node process as a child, so killing only the tracked npm PID can
# leave the actual server running. Matching by port pattern in the process
# command line reaches the real listener regardless of which layer spawned it.
_kill_by_pidfile_and_pattern() {
  local pid_file="$1" pattern="$2" label="$3" found=0
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      found=1
    fi
    rm -f "$pid_file"
  fi
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    pkill -f "$pattern" 2>/dev/null || true
    found=1
  fi
  if [[ "$found" -eq 1 ]]; then
    echo "  stopped $label"
  fi
}

start() {
  if _is_alive "$BACKEND_PID_FILE"; then
    echo "backend already running (pid $(cat "$BACKEND_PID_FILE"))"
  else
    echo "starting backend on :$BACKEND_PORT ..."
    (cd "$ROOT_DIR" && nohup .venv/bin/python3 main.py serve --port "$BACKEND_PORT" \
      > "$BACKEND_LOG" 2>&1 & echo $! > "$BACKEND_PID_FILE")
  fi

  if _is_alive "$FRONTEND_PID_FILE"; then
    echo "frontend already running (pid $(cat "$FRONTEND_PID_FILE"))"
  else
    echo "starting frontend on :$FRONTEND_PORT ..."
    # Call vite directly (not `npm run dev --`) so the tracked PID IS the real
    # server, not an npm wrapper process.
    (cd "$ROOT_DIR/web" && nohup npx vite --port "$FRONTEND_PORT" \
      > "$FRONTEND_LOG" 2>&1 & echo $! > "$FRONTEND_PID_FILE")
  fi

  sleep 2
  echo ""
  echo "control panel:  http://localhost:$FRONTEND_PORT"
  echo "backend api:    http://127.0.0.1:$BACKEND_PORT/docs"
  echo "logs:           ./dev.sh logs"
}

stop() {
  echo "stopping..."
  _kill_by_pidfile_and_pattern "$BACKEND_PID_FILE" "main.py serve --port $BACKEND_PORT" "backend"
  _kill_by_pidfile_and_pattern "$FRONTEND_PID_FILE" "vite --port $FRONTEND_PORT" "frontend"
  _kill_by_pidfile_and_pattern "$STATIC_PID_FILE" "vite preview.*--port $STATIC_PORT" "static preview"
  echo "done."
}

restart() {
  stop
  sleep 1
  start
}

static() {
  echo "building static dashboard..."
  (cd "$ROOT_DIR/web" && npm run build:static)
  if _is_alive "$STATIC_PID_FILE"; then
    echo "static preview already running (pid $(cat "$STATIC_PID_FILE"))"
  else
    echo "starting static preview on :$STATIC_PORT ..."
    (cd "$ROOT_DIR/web" && nohup npx vite preview --mode static --port "$STATIC_PORT" \
      > "$STATIC_LOG" 2>&1 & echo $! > "$STATIC_PID_FILE")
  fi
  sleep 1
  echo ""
  echo "static dashboard: http://localhost:$STATIC_PORT"
}

status() {
  for entry in "backend:$BACKEND_PID_FILE:$BACKEND_PORT" \
               "frontend:$FRONTEND_PID_FILE:$FRONTEND_PORT" \
               "static preview:$STATIC_PID_FILE:$STATIC_PORT"; do
    IFS=: read -r name pid_file port <<< "$entry"
    if _is_alive "$pid_file"; then
      echo "✓ $name — running (pid $(cat "$pid_file"), :$port)"
    else
      echo "✗ $name — not running"
    fi
  done
}

logs() {
  tail -f "$BACKEND_LOG" "$FRONTEND_LOG" 2>/dev/null
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) restart ;;
  static) static ;;
  status) status ;;
  logs) logs ;;
  *)
    echo "usage: $0 {start|stop|restart|static|status|logs}"
    exit 1
    ;;
esac
