#!/usr/bin/env bash
# dev.sh — start/stop the local control-panel (backend + frontend dev servers).
#
#   ./dev.sh start     backend (:8000) + frontend (:5178), backgrounded
#   ./dev.sh stop      cut off everything this script started (or find via port)
#   ./dev.sh restart   stop then start
#   ./dev.sh static    build + preview the read-only public dashboard (:4173)
#   ./dev.sh logs      tail both dev-server logs
#   ./dev.sh tunnel    start with a Cloudflare tunnel (public URL, token-gated)
#   ./dev.sh status    show what's running and where
#
# Env overrides: BACKEND_PORT (default 8000), FRONTEND_PORT (default 5178),
# STATIC_PORT (default 4173).
# Tunnel env:     TUNNEL_TOKEN (auto-generated if not set), TUNNEL_HMR=1 (enable HMR over tunnel, ungated socket),
#                 TUNNEL_STRICT=1 (disable localhost exemption in tunnel gate).
# Note: restart drops the tunnel; run ./dev.sh tunnel again to re-establish.

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
TUNNEL_PID_FILE="$RUN_DIR/tunnel.pid"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"
STATIC_LOG="$RUN_DIR/static.log"
TUNNEL_LOG="$RUN_DIR/tunnel.log"
TUNNEL_URL_FILE="$RUN_DIR/tunnel.url"
TUNNEL_TOKEN_FILE="$RUN_DIR/tunnel.token"

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

_gen_token() {
  # Use openssl rand -hex 16 instead of tr -dc ... < /dev/urandom | head -c:
  # head closes the pipe, tr takes SIGPIPE, and set -o pipefail (line 14)
  # turns that into a script abort.
  openssl rand -hex 16
}

_live_tunnel_token() {
  # Print the token file iff the tunnel pid is alive — prevents re-exposing
  # a frontend that died mid-tunnel when a bare ./dev.sh start is run.
  if _is_alive "$TUNNEL_PID_FILE" && [[ -f "$TUNNEL_TOKEN_FILE" ]]; then
    cat "$TUNNEL_TOKEN_FILE"
  fi
}

_start_backend() {
  if _is_alive "$BACKEND_PID_FILE"; then
    echo "backend already running (pid $(cat "$BACKEND_PID_FILE"))"
  else
    echo "starting backend on :$BACKEND_PORT ..."
    (cd "$ROOT_DIR" && nohup .venv/bin/python3 main.py serve --port "$BACKEND_PORT" \
      > "$BACKEND_LOG" 2>&1 & echo $! > "$BACKEND_PID_FILE")
  fi
}

_start_frontend() {
  local token="${1:-}"
  if _is_alive "$FRONTEND_PID_FILE"; then
    echo "frontend already running (pid $(cat "$FRONTEND_PID_FILE"))"
  else
    echo "starting frontend on :$FRONTEND_PORT ..."
    # Call vite directly (not `npm run dev --`) so the tracked PID IS the real
    # server, not an npm wrapper process.
    (cd "$ROOT_DIR/web" && TUNNEL_TOKEN="$token" nohup npx vite --port "$FRONTEND_PORT" \
      > "$FRONTEND_LOG" 2>&1 & echo $! > "$FRONTEND_PID_FILE")
  fi
}

start() {
  _start_backend
  _start_frontend "$(_live_tunnel_token)"

  sleep 2
  echo ""
  echo "control panel:  http://localhost:$FRONTEND_PORT"
  echo "backend api:    http://127.0.0.1:$BACKEND_PORT/docs"
  echo "logs:           ./dev.sh logs"
}

stop() {
  echo "stopping..."
  _kill_by_pidfile_and_pattern "$TUNNEL_PID_FILE" "cloudflared tunnel .*--url http://localhost:$FRONTEND_PORT" "tunnel"
  _kill_by_pidfile_and_pattern "$BACKEND_PID_FILE" "main.py serve --port $BACKEND_PORT" "backend"
  _kill_by_pidfile_and_pattern "$FRONTEND_PID_FILE" "vite --port $FRONTEND_PORT" "frontend"
  _kill_by_pidfile_and_pattern "$STATIC_PID_FILE" "vite preview.*--port $STATIC_PORT" "static preview"
  rm -f "$TUNNEL_URL_FILE" "$TUNNEL_TOKEN_FILE"
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

tunnel() {
  if ! command -v cloudflared >/dev/null 2>&1; then
    echo "cloudflared not found. Install it: sudo pacman -S cloudflared"
    exit 1
  fi

  local token
  token="$(_gen_token)"
  (umask 077; echo "$token" > "$TUNNEL_TOKEN_FILE")

  _start_backend

  # If frontend is already alive it was started without the token and env vars
  # can't be injected into a live process — kill and relaunch it with the token,
  # or the tunnel is wide open.
  if _is_alive "$FRONTEND_PID_FILE"; then
    _kill_by_pidfile_and_pattern "$FRONTEND_PID_FILE" "vite --port $FRONTEND_PORT" "frontend"
  fi
  _start_frontend "$token"

  : > "$TUNNEL_LOG"
  nohup cloudflared tunnel --no-autoupdate --url "http://localhost:$FRONTEND_PORT" \
    > "$TUNNEL_LOG" 2>&1 &
  echo $! > "$TUNNEL_PID_FILE"

  local url=""
  for _ in {1..45}; do
    if ! _is_alive "$TUNNEL_PID_FILE"; then
      echo "tunnel process died"
      tail -n 20 "$TUNNEL_LOG"
      rm -f "$TUNNEL_PID_FILE" "$TUNNEL_URL_FILE" "$TUNNEL_TOKEN_FILE"
      exit 1
    fi
    url="$(grep -Eo 'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
    if [[ -n "$url" ]]; then
      break
    fi
    sleep 1
  done

  if [[ -z "$url" ]]; then
    echo "tunnel URL not found in log"
    tail -n 20 "$TUNNEL_LOG"
    _kill_by_pidfile_and_pattern "$TUNNEL_PID_FILE" "cloudflared tunnel .*--url http://localhost:$FRONTEND_PORT" "tunnel"
    rm -f "$TUNNEL_URL_FILE" "$TUNNEL_TOKEN_FILE"
    exit 1
  fi

  echo "$url" > "$TUNNEL_URL_FILE"
  echo ""
  echo "public link:    $url/?k=$token"
  echo "note:           the bare URL 404s without the token"
  echo "backend api:    http://127.0.0.1:$BACKEND_PORT/docs"
  echo "logs:           ./dev.sh logs"
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

  # Tunnel status (separate block; the loop above uses IFS=: which would shred an https:// URL)
  if _is_alive "$TUNNEL_PID_FILE"; then
    if [[ -f "$TUNNEL_URL_FILE" ]]; then
      echo "✓ tunnel — running (pid $(cat "$TUNNEL_PID_FILE"), $(cat "$TUNNEL_URL_FILE"))"
    else
      echo "✓ tunnel — running (pid $(cat "$TUNNEL_PID_FILE"), url pending)"
    fi
  else
    echo "✗ tunnel — not running"
  fi
}

logs() {
  tail -f "$BACKEND_LOG" "$FRONTEND_LOG" "$TUNNEL_LOG" 2>/dev/null
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) restart ;;
  static) static ;;
  tunnel) tunnel ;;
  status) status ;;
  logs) logs ;;
  *)
    echo "usage: $0 {start|stop|restart|static|tunnel|status|logs}"
    exit 1
    ;;
esac