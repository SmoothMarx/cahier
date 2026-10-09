#!/usr/bin/env bash
# Cahier — one-command verification.
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
# Prefer an explicit interpreter, then any candidate that can actually import the
# test deps, then PATH. A venv that exists but lacks pytest must not be picked.
VENV_PY="${CAHIER_PY:-}"
if [ -z "$VENV_PY" ]; then
  for cand in "${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python3" \
              "${HERMES_HOME:-$HOME/.hermes}/venv/bin/python3" \
              "$(command -v python3 || true)"; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" -c 'import pytest, fastapi, httpx' >/dev/null 2>&1; then VENV_PY="$cand"; break; fi
  done
  [ -n "$VENV_PY" ] || { echo "no interpreter with pytest+fastapi+httpx found; set CAHIER_PY" >&2; exit 2; }
fi
# Scratch: the tests and the panel harness create throwaway dirs under it, and a
# failing run must not leave litter behind either. Nothing here is hardcoded to
# $HOME/.hermes: TMPDIR wins, then the Hermes scratch dir, then a temp dir.
SCRATCH="${TMPDIR:-${HERMES_HOME:-$HOME/.hermes}/cache/scratch}"
mkdir -p "$SCRATCH" 2>/dev/null || SCRATCH="$(mktemp -d)"
# The panel harness builds its stub node_modules somewhere disposable; tell it
# where, so a fresh clone never writes under $HOME/.hermes on its own.
export CAHIER_JS_WORK="${CAHIER_JS_WORK:-$SCRATCH/cahier-js}"

FIXTURE_DIR="$SCRATCH"
mkdir -p "$FIXTURE_DIR" 2>/dev/null || FIXTURE_DIR="$(mktemp -d)"
FIXTURE="$FIXTURE_DIR/cahier-fixture.json"
cleanup() {
  rc=$?
  rm -rf "$CAHIER_JS_WORK" 2>/dev/null || true
  rm -f "$FIXTURE" 2>/dev/null || true
  find "$SCRATCH" -maxdepth 1 -name 'cahier-inbox-*' -o -maxdepth 1 -name 'cahier-state-*' \
    -o -maxdepth 1 -name 'cahier-shape-*' 2>/dev/null | xargs -r rm -rf
  return $rc
}
trap cleanup EXIT

echo "== control plane: cahier_ctl iteration =="
# Compile whichever control plane is actually in use: a configured path, the one
# in $HERMES_HOME/scripts, or the bundled copy. A fresh clone has only the last.
CTL="$("$VENV_PY" -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("api", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m._ctl_path())
' "$PLUGIN/dashboard/plugin_api.py")"
python3 -m py_compile "$CTL" && echo "  compile OK  ($CTL)"

echo "== install doctor =="
"$VENV_PY" "$PLUGIN/scripts/doctor.py" || echo "  (informational — a FAIL here is an install problem, not a test failure)"

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
