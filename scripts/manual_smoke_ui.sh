#!/usr/bin/env bash
# Opt-in manual smoke test for the v0.5 task experience (taskui).
#
# This script is NEVER run by the automated test suite (which uses only
# mocks and temporary directories, and never opens a window).  Enable it
# only when you (a developer) want to confirm the real interface works
# end to end on this machine:
#
#   ALWIN_UI_SMOKE=1 scripts/manual_smoke_ui.sh
#
# What it does, in order — read this before running it:
#
#   1. reports the environment (Python, GTK4, display, AI runtime),
#   2. runs the HEADLESS walkthrough (python3 -m taskui --smoke): the
#      real manager + trusted pipeline execute a read-only task, then a
#      mutating task that is REJECTED (proving nothing runs without
#      approval), then the same task APPROVED (proving approval binds to
#      the displayed plan digest and the result is verified on disk),
#   3. prints the headless capability check (--check),
#   4. opens the REAL GTK4 window on your display so you can submit a
#      task yourself, watch the plan, approve or reject it, and inspect
#      the verified result and history.  Close the window to finish.
#
# Safety boundaries:
#   * everything runs in a temporary sandbox home; your real
#     ~/Documents and real state directory are never touched because
#     HOME / ALWIN_HOME / ALWIN_STATE_DIR are redirected,
#   * the interface executes nothing itself: every action goes through
#     the same permission -> approval -> execution -> verification ->
#     audit pipeline the CLI uses,
#   * nothing is installed, no model is downloaded, and no cloud
#     service is contacted (the only network client talks to a local
#     Ollama server on this machine, and only if you pass --llm),
#   * if GTK4 is not installed the window step is skipped with an
#     honest message; the headless walkthrough still proves the whole
#     task lifecycle.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "${ALWIN_UI_SMOKE:-}" != "1" ]]; then
  echo "Manual task-experience smoke test (opt-in; nothing was run)."
  echo "Run it with:  ALWIN_UI_SMOKE=1 scripts/manual_smoke_ui.sh"
  exit 0
fi

SANDBOX="$(mktemp -d /tmp/alwin-ui-smoke.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

# Redirect *everything* user-visible into the sandbox: HOME covers the
# real Documents directory and the real state/audit/history files.
export HOME="$SANDBOX/home"
export ALWIN_HOME="$SANDBOX/home"
export ALWIN_PERMITTED_ROOTS="Documents"
export ALWIN_STATE_DIR="$SANDBOX/state"
mkdir -p "$ALWIN_HOME/Documents" "$ALWIN_STATE_DIR"

echo "== Environment =="
echo "sandbox home:  $ALWIN_HOME"
echo "python:        $(python3 --version)"
if python3 -c 'import gi; gi.require_version("Gtk","4.0")' 2>/dev/null; then
  echo "GTK4:          available (the window will open)."
  HAVE_GTK=1
else
  echo "GTK4:          NOT available (headless walkthrough only)."
  HAVE_GTK=0
fi
if [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
  echo "display:       present (${DISPLAY:-}${WAYLAND_DISPLAY:+/wayland})."
  HAVE_DISPLAY=1
else
  echo "display:       absent (headless walkthrough only)."
  HAVE_DISPLAY=0
fi
if curl -s --max-time 1 http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "Ollama:        reachable at localhost:11434."
else
  echo "Ollama:        not reachable (the deterministic parser is used; that is fine)."
fi
echo

echo "== 1) Headless task-experience walkthrough =="
python3 -m taskui --smoke
echo

echo "== 2) Headless capability check =="
python3 -m taskui --check
echo

echo "== 3) Local history written by the walkthrough =="
if [[ -f "$ALWIN_STATE_DIR/task_history.jsonl" ]]; then
  echo "--- task_history.jsonl (owner-only: $(stat -c '%a' "$ALWIN_STATE_DIR/task_history.jsonl")) ---"
  cat "$ALWIN_STATE_DIR/task_history.jsonl"
else
  echo "(no history file was written)"
fi
echo
if [[ -f "$ALWIN_STATE_DIR/audit.jsonl" ]]; then
  echo "--- audit.jsonl (last record) ---"
  tail -n 1 "$ALWIN_STATE_DIR/audit.jsonl"
  echo
fi

if [[ "$HAVE_GTK" == "1" && "$HAVE_DISPLAY" == "1" ]]; then
  echo "== 4) Opening the real GTK4 task window =="
  echo "   Try, in order:"
  echo "     a) 'Show files and folders in my Documents.'"
  echo "        -> completes read-only, shows verification evidence."
  echo "     b) 'Create a folder named SmokeTest in Documents.'"
  echo "        -> shows the real plan; press Reject (nothing is created),"
  echo "        then repeat and press Approve (folder is created + verified)."
  echo "     c) Inspect a history row at the bottom."
  echo "   Close the window to finish the smoke test."
  echo
  python3 -m taskui
  echo
  echo "== Resulting sandbox Documents =="
  ls -la "$ALWIN_HOME/Documents" || true
else
  echo "== 4) GTK window step skipped =="
  echo "   (Needs both GTK4 and a display.  The headless walkthrough above"
  echo "    already exercised submit -> plan -> approve/reject -> execute ->"
  echo "    verify -> history through the real trusted pipeline.)"
fi

echo
echo "Done. Everything ran inside the sandbox; nothing outside it was"
echo "read or changed, and the sandbox is deleted on exit."
