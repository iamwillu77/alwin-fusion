#!/usr/bin/env bash
# Opt-in manual smoke test for the v0.3 application integration engine.
#
# This script is never run by the automated test suite.  Enable it only
# when you (a developer) want to watch a *real* application launch and
# approve it yourself:
#
#   ALWIN_MANUAL_SMOKE=1 scripts/manual_smoke_apps.sh
#
# It runs everything inside a temporary sandbox home (never your real
# Documents directory), shows read-only detection first, then asks you
# interactively to approve one launch of LibreOffice Writer (or, if
# LibreOffice is not installed, shows the honest failure message).
#
# Reminder: Alwin Fusion only verifies that the process was started.
# It does not inspect, control or automate the application window.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "${ALWIN_MANUAL_SMOKE:-}" != "1" ]]; then
  echo "Manual application smoke test (opt-in; nothing was executed)."
  echo "Run it with:  ALWIN_MANUAL_SMOKE=1 scripts/manual_smoke_apps.sh"
  exit 0
fi

SANDBOX="$(mktemp -d /tmp/alwin-manual-smoke.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

export ALWIN_HOME="$SANDBOX/home"
export ALWIN_PERMITTED_ROOTS="Documents"
export ALWIN_STATE_DIR="$SANDBOX/state"
mkdir -p "$ALWIN_HOME/Documents" "$ALWIN_STATE_DIR"

printf 'Alwin Fusion v0.3 manual smoke test file.\n' \
  > "$ALWIN_HOME/Documents/alwin-smoke.txt"

echo "== Environment =="
echo "sandbox home: $ALWIN_HOME"
if command -v soffice >/dev/null 2>&1 || command -v libreoffice >/dev/null 2>&1; then
  echo "LibreOffice: detected on this machine."
else
  echo "LibreOffice: NOT detected (step 2 will fail honestly, which is also a test)."
fi
echo

echo "== 1) Detection (read-only, no approval, nothing launched) =="
python3 -m ai_core.cli "Which applications are installed?"
echo

echo "== 2) Launch (approve the proposal with 'y') =="
# The pipeline reads the approval answer from stdin; 'quit' then exits.
printf 'Open alwin-smoke.txt in Documents with LibreOffice Writer\ny\nquit\n' \
  | python3 -m ai_core.cli
echo

echo "== 3) Audit record (last line of audit.jsonl) =="
if [[ -f "$ALWIN_STATE_DIR/audit.jsonl" ]]; then
  tail -n 1 "$ALWIN_STATE_DIR/audit.jsonl"
else
  echo "(no audit file was written)"
fi
echo

echo "Done. If LibreOffice was installed, a Writer window should have opened"
echo "with alwin-smoke.txt; Alwin Fusion verified only that the process was"
echo "started -- it never inspected the window. The sandbox is deleted now."
