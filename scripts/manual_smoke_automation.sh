#!/usr/bin/env bash
# Opt-in manual smoke test for the v0.4 desktop-automation engine.
#
# This script is NEVER run by the automated test suite (which uses only
# mocks and temporary directories).  Enable it only when you (a
# developer) want to watch ALWIN drive a *real* LibreOffice window over
# real AT-SPI, approving every mutation yourself:
#
#   ALWIN_AUTOMATION_SMOKE=1 scripts/manual_smoke_automation.sh
#
# What it does, in order — read this before running it:
#
#   1. reports the environment (LibreOffice, accessibility bus),
#   2. READ-ONLY:  inspects the state of LibreOffice Writer
#      (expected honestly: 'installed_not_running' — ALWIN never
#      launches anything from an inspection),
#   3. MUTATION (you approve): opens a sandbox text file in Writer,
#   4. READ-ONLY:  re-inspects (expected: 'document_open' or
#      'interface_detected' — whichever is actually provable),
#   5. READ-ONLY:  finds the Save button, reads the Title label,
#   6. MUTATION (you approve): attempts a controlled save of the open
#      document.  The file is unmodified, so the digest will NOT change
#      and ALWIN must honestly report failure ('state unknown') — that
#      is the digest-confirmation rule working, not a bug,
#   7. MUTATION (you approve): a two-step task sequence (inspect, then
#      find) demonstrating per-step validation and verification.
#
# Safety boundaries:
#   * everything runs in a temporary sandbox home; your real ~/Documents
#     and your real LibreOffice profile (~/.config/libreoffice) are never
#     touched because HOME is redirected,
#   * ALWIN sends no keystrokes and no mouse events; the only mutation
#     primitives are accessibility actions on identified controls and
#     text entry into identified input fields,
#   * if AT-SPI is unavailable or LibreOffice has not exposed its
#     interface yet, every step fails honestly with 'state unknown'
#     instead of guessing — that is also a valid, expected outcome.
#
# Note on desktop accessibility (observed on Ubuntu GNOME/Wayland):
#   LibreOffice only appears on the accessibility bus when the desktop
#   exposes assistive technology (e.g. GNOME Settings -> Accessibility,
#   or 'gsettings set org.gnome.desktop.interface toolkit-accessibility
#   true').  ALWIN never changes that setting itself.  Without it, expect
#   steps 3-7 to honestly report 'process_started' and 'state unknown' —
#   the engine refusing to fake a window it cannot see.
#
# Close the Writer window when you are done; the sandbox is deleted on
# exit.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "${ALWIN_AUTOMATION_SMOKE:-}" != "1" ]]; then
  echo "Manual desktop-automation smoke test (opt-in; nothing was executed)."
  echo "Run it with:  ALWIN_AUTOMATION_SMOKE=1 scripts/manual_smoke_automation.sh"
  exit 0
fi

SANDBOX="$(mktemp -d /tmp/alwin-automation-smoke.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

# Redirect *everything* user-visible into the sandbox: HOME covers the
# real Documents directory and the LibreOffice user profile.
export HOME="$SANDBOX/home"
export ALWIN_HOME="$SANDBOX/home"
export ALWIN_PERMITTED_ROOTS="Documents"
export ALWIN_STATE_DIR="$SANDBOX/state"
mkdir -p "$ALWIN_HOME/Documents" "$ALWIN_STATE_DIR"

printf 'Alwin Fusion v0.4 automation smoke test file.\n' \
  > "$ALWIN_HOME/Documents/alwin-smoke.txt"

echo "== Environment =="
echo "sandbox home:  $ALWIN_HOME"
if command -v soffice >/dev/null 2>&1 || command -v libreoffice >/dev/null 2>&1; then
  echo "LibreOffice:   detected on this machine."
else
  echo "LibreOffice:   NOT detected (every step must fail honestly)."
fi
if [[ -S "/run/user/$(id -u)/at-spi/bus_0" ]] || [[ -n "${AT_SPI_BUS:-}" ]] \
   || [[ -S "/run/user/$(id -u)/at-spi/bus" ]]; then
  echo "AT-SPI bus:    present (accessibility is available)."
else
  echo "AT-SPI bus:    not detected (interface steps will report state unknown)."
fi
echo

echo "== 1) State inspection (READ-ONLY, nothing launched) =="
# Every '|| true' below: an honest failure exits non-zero, and honest
# failures are an expected outcome of this smoke test — never a reason
# to abort it.
python3 -m ai_core.cli "Inspect the state of LibreOffice Writer." || true
echo

echo "== 2) Launch Writer on the sandbox file (approve with 'y') =="
printf 'Open alwin-smoke.txt in Documents with LibreOffice Writer\ny\nquit\n' \
  | python3 -m ai_core.cli || true
echo
echo "Waiting a few seconds for LibreOffice to expose its interface..."
sleep 5

echo "== 3) Re-inspection (READ-ONLY) =="
python3 -m ai_core.cli "Inspect the state of LibreOffice Writer." || true
echo

echo "== 4) Find the Save button (READ-ONLY) =="
python3 -m ai_core.cli "Find the Save button in LibreOffice Writer." || true
echo

echo "== 5) Read the Title label (READ-ONLY, untrusted app text) =="
python3 -m ai_core.cli "Read the text of the Title label in LibreOffice Writer." || true
echo

echo "== 6) Controlled save attempt (approve with 'y') =="
echo "   (The document is unmodified, so the file digest must NOT change"
echo "    and ALWIN must report an honest 'state unknown' failure.)"
printf 'Save the document in LibreOffice Writer to Documents/alwin-smoke.txt\ny\nquit\n' \
  | python3 -m ai_core.cli || true
echo

echo "== 7) Two-step task sequence (approve with 'y') =="
printf 'Inspect the state of LibreOffice Writer then find the Save button in LibreOffice Writer\ny\nquit\n' \
  | python3 -m ai_core.cli || true
echo

echo "== Audit trail (last record) =="
if [[ -f "$ALWIN_STATE_DIR/audit.jsonl" ]]; then
  tail -n 1 "$ALWIN_STATE_DIR/audit.jsonl"
else
  echo "(no audit file was written)"
fi
echo

if pgrep -u "$(id -u)" -x soffice.bin >/dev/null 2>&1; then
  echo "Note: LibreOffice is still running.  Close the Writer window"
  echo "showing alwin-smoke.txt before continuing; the sandbox (and this"
  echo "document) is deleted when this script exits."
fi
echo "Done. Nothing outside the sandbox was read or changed."
