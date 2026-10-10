#!/usr/bin/env bash
# Contract F14 — live Formance e2e runbook (see docs/live-e2e.md).
#
# Usage (on a Docker host, e.g. thebrick):
#   cd /tmp/aa-live
#   ./scripts/live-e2e.sh
#
# Does NOT tear anything down. Teardown instructions are printed at the end.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LEDGER_URL="${FORNANCE_LEDGER_URL:-http://127.0.0.1:3068}"
HEALTH_WAIT="${HEALTH_WAIT:-120}"
VENV_DIR="${VENV_DIR:-$REPO_ROOT/api/.venv}"
CAPTURE_OUT="${LIVE_CAPTURE_OUT:-$REPO_ROOT/live-shapes.json}"

echo "==> Repo root:   $REPO_ROOT"
echo "==> Ledger URL:  $LEDGER_URL"
echo "==> Health wait: ${HEALTH_WAIT}s"

# 1. Python venv (python 3.11+, deps pinned in api/pyproject.toml).
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "==> Creating venv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q -e "$REPO_ROOT/api[dev]"

# 2. Bring up ONLY the Formance stack from the repo compose file.
echo "==> docker compose up -d formance-postgres formance-ledger"
(cd "$REPO_ROOT" && docker compose up -d formance-postgres formance-ledger)

# 3. Wait for the ledger's _info endpoint.
echo "==> Waiting for $LEDGER_URL/_info ..."
deadline=$((SECONDS + HEALTH_WAIT))
until curl -sf "$LEDGER_URL/_info" >/dev/null 2>&1; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "!! Ledger did not become healthy within ${HEALTH_WAIT}s" >&2
    docker compose -f "$REPO_ROOT/docker-compose.yml" logs formance-ledger >&2 || true
    exit 1
  fi
  sleep 3
done
echo "==> Ledger healthy ($(curl -s "$LEDGER_URL/_info" | head -c 200))"

# 4. Run the live suite.
echo "==> Running the live suite"
cd "$REPO_ROOT/api"
FORNANCE_LEDGER_URL="$LEDGER_URL" LIVE_CAPTURE_OUT="$CAPTURE_OUT" \
  "$VENV_DIR/bin/python" -m pytest tests/live -v --timeout=120

echo
echo "==> Live suite finished. Shape capture: $CAPTURE_OUT"
echo
echo "Teardown (only when you are done inspecting):"
echo "  docker compose -f $REPO_ROOT/docker-compose.yml stop formance-ledger formance-postgres"
echo "  docker compose -f $REPO_ROOT/docker-compose.yml rm -f formance-ledger formance-postgres"
echo "  rm -rf $REPO_ROOT"
