#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${SERVER_HOST:-0.0.0.0}"
PORT="${SERVER_PORT:-5000}"
THREADS="${WAITRESS_THREADS:-8}"

# Resolve Python
if [ -f ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
else
    PYTHON="python"
fi

# Ensure .env exists
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo "[i] Creating .env from .env.example..."
    cp .env.example .env
fi

# Run preflight
echo "[*] Executing preflight checks..."
$PYTHON preflight.py --skip-db --skip-llm

# Run Waitress production WSGI server
echo "[*] Starting production Waitress server on $HOST:$PORT (threads: $THREADS)..."
exec $PYTHON production.py --host "$HOST" --port "$PORT" --threads "$THREADS"
