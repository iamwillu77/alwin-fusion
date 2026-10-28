#!/usr/bin/env bash
# Opt-in manual smoke test for the v0.7 Alwin companion (companion/).
#
# This script is NEVER run by the automated test suite (which uses only
# mocks, temporary directories and headless cairo surfaces, and never
# opens a window).  Enable it only when you (a) developer) want to
# confirm the real companion works on this machine:
#
#   ALWIN_COMPANION_SMOKE=1 scripts/manual_smoke_companion.sh
#
# What it does, in order — read this before running it:
#
#   1. reports the environment (Python, GTK4, display, pycairo),
#   2. runs the HEADLESS self-check (python3 -m companion --check):
#      state graph, task-event mapping (including "failed never
#      celebrates"), greeting rules, settings and 2D rendering of all
#      16 states of the full-body character,
#   3. runs the HEADLESS walkthrough (python3 -m companion --smoke):
#      greeting dedup + quiet hours, approval mapping, renderer-error
#      isolation, asset fallback, settings permissions,
#   4. runs the companion's automated tests (renderer, character,
#      states, window, greeting, presentation, adapter, security),
#   5. opens the REAL companion window next to the real task window
#      (python3 -m taskui --companion) so you can watch the full-body
#      cartoon boy (hair, hoodie, arms, legs, shoes) blink, breathe,
#      wave, think and show writing/coding/celebrating/error poses as
#      you submit a task yourself.  Close the window to finish.
#
# Safety boundaries:
#   * everything runs in a temporary sandbox home; your real
#     ~/Documents and real state directory are never touched because
#     HOME / ALWIN_HOME / ALWIN_STATE_DIR are redirected,
#   * the companion executes nothing: it only *observes* records the
#     trusted pipeline already produced (read-only observer feed),
#   * no microphone is opened, no voice engine is started or installed,
#     no web server is started, no automatic startup entry is created,
#   * the greeting name comes from the environment (ALWIN_COMPANION_NAME)
#     or your saved settings — nothing is hard-coded,
#   * if GTK4 is not installed the window step is skipped with an
#     honest message; the headless steps still prove the companion core.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "${ALWIN_COMPANION_SMOKE:-}" != "1" ]]; then
  echo "Manual companion smoke test (opt-in; nothing was run)."
  echo "Run it with:  ALWIN_COMPANION_SMOKE=1 scripts/manual_smoke_companion.sh"
  exit 0
fi

SANDBOX="$(mktemp -d /tmp/alwin-companion-smoke.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

# Redirect *everything* user-visible into the sandbox: HOME covers the
# real Documents directory and the real state/audit/history/settings files.
export HOME="$SANDBOX/home"
export ALWIN_HOME="$SANDBOX/home"
export ALWIN_PERMITTED_ROOTS="Documents"
export ALWIN_STATE_DIR="$SANDBOX/state"
export ALWIN_COMPANION_NAME="${ALWIN_COMPANION_NAME:-Friend}"
mkdir -p "$ALWIN_HOME/Documents" "$ALWIN_STATE_DIR"

echo "== Environment =="
echo "sandbox home:  $ALWIN_HOME"
echo "greeting name: $ALWIN_COMPANION_NAME (ALWIN_COMPANION_NAME)"
echo "python:        $(python3 --version)"
if python3 -c 'import gi; gi.require_version("Gtk","4.0")' 2>/dev/null; then
  echo "GTK4:          available (the window will open)."
  HAVE_GTK=1
else
  echo "GTK4:          NOT available (headless steps only)."
  HAVE_GTK=0
fi
if python3 -c 'import cairo' 2>/dev/null; then
  echo "pycairo:       $(python3 -c 'import cairo; print(cairo.version)')"
else
  echo "pycairo:       NOT available (headless steps only)."
  HAVE_GTK=0
fi
if [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
  echo "display:       present (${DISPLAY:-}${WAYLAND_DISPLAY:+/wayland})."
  HAVE_DISPLAY=1
else
  echo "display:       absent (headless steps only)."
  HAVE_DISPLAY=0
fi
echo

echo "== 1) Headless companion self-check =="
python3 -m companion --check
echo

echo "== 2) Headless companion walkthrough =="
python3 -m companion --smoke
echo

echo "== 3) Companion automated tests =="
scripts/run_tests.sh -q -k companion
echo

echo "== 4) Companion settings produced in the sandbox state dir =="
if [[ -f "$ALWIN_STATE_DIR/companion_settings.json" ]]; then
  echo "--- companion_settings.json (owner-only: $(stat -c '%a' "$ALWIN_STATE_DIR/companion_settings.json")) ---"
  cat "$ALWIN_STATE_DIR/companion_settings.json"
else
  echo "(no settings file yet — the window writes one when you change a setting)"
fi
echo

if [[ "$HAVE_GTK" == "1" && "$HAVE_DISPLAY" == "1" ]]; then
  echo "== 5) Opening the real task window + companion =="
  echo "   Try, in order:"
  echo "     a) Watch Alwin's startup greeting (text bubble; no voice, no mic)."
  echo "     b) In the task window submit 'Show files and folders in my Documents.'"
  echo "        -> Alwin thinks, then celebrates only when the task VERIFIES."
  echo "     c) Submit 'Create a folder named CompanionSmoke in Documents.'"
  echo "        -> Alwin waits for approval (it never approves itself);"
  echo "           the plan/approve buttons live only in the task window."
  echo "     d) Reject it: Alwin stays calm — no celebration on rejection."
  echo "     e) Use the companion's Settings (greeting/subtitles/always on top)"
  echo "        and its Hide control; the task engine keeps running."
  echo "   Close the windows to finish the smoke test."
  echo
  python3 -m taskui --companion
  echo
  echo "== Resulting sandbox Documents =="
  ls -la "$ALWIN_HOME/Documents" || true
else
  echo "== 5) Real-window step skipped =="
  echo "   (Needs both GTK4 and a display.  The headless steps above already"
  echo "    exercised the state machine, task-event mapping, greeting rules,"
  echo "    renderer isolation and asset fallback.)"
fi

echo
echo "Done. Everything ran inside the sandbox; nothing outside it was"
echo "read or changed, and the sandbox is deleted on exit."
