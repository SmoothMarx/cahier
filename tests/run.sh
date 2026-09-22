#!/usr/bin/env bash
# Cahier Hub — one-command verification.
#
#   ./tests/run.sh
#
# Runs both halves:
#   * the backend contract suite (pytest, real FastAPI app + router)
#   * the panel harness (node, real backend payload, stubbed SDK)
# Exits non-zero if either half fails.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN="$(dirname "$HERE")"
VENV_PY="${CAHIER_HUB_PY:-/home/smoothmarx/.hermes/hermes-agent/venv/bin/python3}"
FIXTURE="${TMPDIR:-/tmp}/cahier-hub-fixture.json"
mkdir -p "$(dirname "$FIXTURE")" 2>/dev/null || FIXTURE="$HOME/.hermes/cache/scratch/cahier-hub-fixture.json"

echo "== control plane: cahier_ctl iteration =="
python3 -m py_compile "$HOME/.hermes/scripts/cahier_ctl.py" && echo "  compile OK"

echo "== backend contract tests =="
"$VENV_PY" -m pytest "$PLUGIN/tests" -q

echo "== panel harness (real payload, stubbed SDK) =="
for scope in active all; do
  "$VENV_PY" "$PLUGIN/tests/dump_fixture.py" "$scope" > "$FIXTURE"
  echo "-- scope=$scope ($(wc -c < "$FIXTURE") bytes)"
  node "$PLUGIN/tests/plugin_harness.mjs" "$FIXTURE"
done

echo
echo "OK — panel lists every active cahier, deterministically, groups them by profile ▸ project,"
echo "     reads them in-window, and writes nothing but the human's ✎ filing."
