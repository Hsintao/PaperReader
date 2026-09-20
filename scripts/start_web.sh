#!/usr/bin/env bash
set -euo pipefail

# One-click start for the browser build: the FastAPI process serves
# frontend/dist on a single local port. For hot-reload UI work use
# `make backend` + `make frontend` (Vite on 5173) instead.
#
# PAPERREADER_PORT=<n> pins the port; otherwise 8000-8004 are tried in order.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIST="$ROOT/frontend/dist"

die() {
  echo "error: $*" >&2
  exit 1
}

# --- interpreter with the backend dependencies ------------------------------

PYTHON="${PYTHON:-python}"
if ! "$PYTHON" -c 'import uvicorn, fastapi' >/dev/null 2>&1; then
  for env_name in pt d2l; do
    conda env list 2>/dev/null | awk '{print $1}' | grep -qx "$env_name" || continue
    # shellcheck disable=SC1091
    source "$(conda info --base)/etc/profile.d/conda.sh"
    conda activate "$env_name"
    PYTHON=python
    break
  done
fi
"$PYTHON" -c 'import uvicorn, fastapi' >/dev/null 2>&1 ||
  die "$PYTHON has no uvicorn/fastapi. Run scripts/setup_$(uname | tr 'A-Z' 'a-z').sh first, or set PYTHON=<interpreter>."

# --- helpers ----------------------------------------------------------------

# Prints "free", "paperreader" (a PaperReader backend already answers on that
# port) or "busy" (something else owns it).
probe() {
  "$PYTHON" -c '
import json, socket, sys, urllib.request

port = int(sys.argv[1])
sock = socket.socket()
sock.settimeout(0.3)
in_use = sock.connect_ex(("127.0.0.1", port)) == 0
sock.close()
if not in_use:
    print("free")
    raise SystemExit
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
        app = json.load(response).get("app")
except Exception:
    app = None
print("paperreader" if app == "PaperReader" else "busy")
' "$1"
}

open_url() {
  if command -v open >/dev/null 2>&1; then
    open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$1" >/dev/null 2>&1
  else
    echo "Open $1 in your browser."
  fi
}

# --- config file ------------------------------------------------------------

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "created .env from .env.example — fill in OPENAI_API_KEY / MINERU_API_KEY"
  echo "or set them in the in-app Settings before uploading a paper."
fi

# --- port -------------------------------------------------------------------

if [ -n "${PAPERREADER_PORT:-}" ]; then
  candidates="$PAPERREADER_PORT"
else
  candidates="8000 8001 8002 8003 8004"
fi

PORT=""
for candidate in $candidates; do
  case "$(probe "$candidate")" in
    free)
      PORT="$candidate"
      break
      ;;
    paperreader)
      echo "PaperReader is already serving http://127.0.0.1:$candidate — opening it."
      open_url "http://127.0.0.1:$candidate"
      exit 0
      ;;
    busy)
      if [ -n "${PAPERREADER_PORT:-}" ]; then
        die "port $candidate is in use by another process"
      fi
      ;;
  esac
done
[ -n "$PORT" ] || die "no free port among $candidates; set PAPERREADER_PORT to pick one"

# --- web UI -----------------------------------------------------------------

need_build=1
if [ -f "$FRONTEND_DIST/index.html" ] &&
  [ -z "$(find "$ROOT/frontend/src" "$ROOT/frontend/index.html" -newer "$FRONTEND_DIST/index.html" -print -quit 2>/dev/null)" ]; then
  need_build=0
fi

if [ "$need_build" = 1 ]; then
  command -v npm >/dev/null 2>&1 || die "npm is required to build the web UI"
  echo "building web UI into frontend/dist ..."
  npm --prefix "$ROOT/frontend" run build
fi

# --- serve ------------------------------------------------------------------

echo "starting PaperReader on http://127.0.0.1:$PORT ..."
cd "$ROOT/backend"
PAPERREADER_FRONTEND_DIR="$FRONTEND_DIST" "$PYTHON" -m uvicorn app.main:app \
  --host 127.0.0.1 --port "$PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' INT TERM EXIT

for _ in $(seq 1 60); do
  [ "$(probe "$PORT")" = "paperreader" ] && break
  kill -0 "$SERVER_PID" 2>/dev/null || die "backend exited during startup"
  sleep 0.5
done
[ "$(probe "$PORT")" = "paperreader" ] || die "backend did not become healthy on port $PORT"

echo
echo "PaperReader web UI: http://127.0.0.1:$PORT"
echo "Ctrl+C to stop."
open_url "http://127.0.0.1:$PORT"

wait "$SERVER_PID"
