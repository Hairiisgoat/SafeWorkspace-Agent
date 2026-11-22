#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

echo "============================================================"
echo "  SafeWorkspace Agent - One-Click Launcher (macOS / Linux)"
echo "============================================================"

# 1. Locate Python 3.11+
PY_CMD="${PYTHON_EXE:-}"
if [ -z "$PY_CMD" ] && [ -x "$BACKEND_DIR/.venv/bin/python" ]; then
  PY_CMD="$BACKEND_DIR/.venv/bin/python"
elif [ -z "$PY_CMD" ] && [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PY_CMD="$ROOT_DIR/.venv/bin/python"
elif [ -z "$PY_CMD" ]; then
  PY_CMD="$(command -v python3 || command -v python || true)"
fi

if [ -z "$PY_CMD" ]; then
  echo "[ERROR] Python 3.11+ not found. Please install Python 3.11+ or set PYTHON_EXE."
  exit 1
fi

if ! "$PY_CMD" -c "import fastapi, uvicorn, langgraph, langchain_openai, sse_starlette, docx" >/dev/null 2>&1; then
  echo "[INFO] Installing backend dependencies..."
  "$PY_CMD" -m pip install -r "$BACKEND_DIR/requirements.txt"
fi

# 2. Check frontend dependencies
if ! command -v npm >/dev/null 2>&1; then
  echo "[ERROR] Node.js / npm not found. Please install Node.js 20+."
  exit 1
fi

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "[INFO] Installing frontend dependencies..."
  (cd "$FRONTEND_DIR" && npm install)
fi

cleanup() {
  echo ""
  echo "[INFO] Shutting down SafeWorkspace Agent..."
  kill "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
  wait "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "[1/2] Starting Backend  on http://127.0.0.1:8000 ..."
(cd "$BACKEND_DIR" && "$PY_CMD" -m uvicorn app.main:app --host 0.0.0.0 --port 8000) &
BACKEND_PID=$!

echo "[2/2] Starting Frontend on http://localhost:5173 ..."
(cd "$FRONTEND_DIR" && npm run dev) &
FRONTEND_PID=$!

echo "------------------------------------------------------------"
echo "  Web Workbench : http://localhost:5173"
echo "  Backend Docs  : http://127.0.0.1:8000/docs"
echo "  Press Ctrl+C to stop both services."
echo "------------------------------------------------------------"

wait
