#!/usr/bin/env bash
# Isolated dev whiteboard: a free port, its own data dir, a headless Chrome viewer.
# Never touches the live board on :8765 or ./boards.
#   board.sh start | stop | status | wb <whiteboard CLI args...>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DIR="${TMPDIR:-/tmp}/whiteboard-dev"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
# Each start takes a new free port: the server binds without SO_REUSEADDR, so a
# port it just released stays unusable for about a minute.
PORT="$(cat "$DIR/port" 2>/dev/null || echo 0)"
URL="http://127.0.0.1:$PORT"

export WHITEBOARD_DATA="$DIR/data" WHITEBOARD_NO_BROWSER=1
cd "$ROOT"

health() { curl -fsS -m 2 "$URL/api/health" 2>/dev/null; }
viewers() { health | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["viewers"])' 2>/dev/null || echo 0; }
wait_for() {  # wait_for <seconds> <command...>
  local n=$(( $1 * 5 )); shift
  while [ "$n" -gt 0 ]; do "$@" && return 0; sleep 0.2; n=$((n - 1)); done
  return 1
}
find_chrome() {
  local c
  for c in "${CHROME:-}" google-chrome google-chrome-stable chromium chromium-browser; do
    [ -n "$c" ] && command -v "$c" >/dev/null 2>&1 && { command -v "$c"; return 0; }
  done
  ls "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | tail -1 | grep . 
}
stop() {
  [ "$PORT" != 0 ] && health >/dev/null && curl -fsS -m 3 -X POST -H 'Content-Type: application/json' -d '{}' \
    "$URL/api/shutdown" >/dev/null 2>&1 || true
  local f pid
  for f in "$DIR"/chrome.pid "$DIR"/server.pid; do
    [ -f "$f" ] || continue
    pid="$(cat "$f")"
    kill "$pid" 2>/dev/null || true
    wait_for 5 eval "! kill -0 $pid 2>/dev/null" || kill -9 "$pid" 2>/dev/null || true
  done
  rm -rf "$DIR"
}

case "${1:-}" in
  start)
    stop  # always a fresh process, so it runs the code on disk now
    mkdir -p "$DIR/data"
    PORT="${WB_DEV_PORT:-$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}"
    URL="http://127.0.0.1:$PORT"
    echo "$PORT" >"$DIR/port"
    "$PY" -m whiteboard serve --port "$PORT" >"$DIR/server.log" 2>&1 &
    echo $! >"$DIR/server.pid"
    wait_for 10 eval 'health >/dev/null' || { echo "server did not start:"; cat "$DIR/server.log"; exit 1; }
    chrome="$(find_chrome)" || { echo "no Chrome/Chromium found; set CHROME=/path/to/chrome"; exit 1; }
    "$chrome" --headless=new --no-first-run --no-default-browser-check --disable-gpu \
      --hide-scrollbars --window-size=1600,1000 --user-data-dir="$DIR/chrome" \
      --remote-debugging-port=0 "$URL" >"$DIR/chrome.log" 2>&1 &
    echo $! >"$DIR/chrome.pid"
    wait_for 15 eval '[ "$(viewers)" -ge 1 ]' || { echo "viewer did not connect:"; tail -20 "$DIR/chrome.log"; exit 1; }
    echo "ready: $URL  (data: $DIR/data, logs: $DIR/*.log)"
    ;;
  stop) stop; echo "stopped" ;;
  status) health || { echo "not running"; exit 1; }; echo "  $URL" ;;
  wb) shift; health >/dev/null || { echo "not running; run: board.sh start"; exit 1; }
      exec "$PY" -m whiteboard --url "$URL" --agent dev "$@" ;;
  *) sed -n 2,4p "${BASH_SOURCE[0]}"; exit 2 ;;
esac
