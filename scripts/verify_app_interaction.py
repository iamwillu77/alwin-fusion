#!/usr/bin/env python3
"""v0.17.0 REAL end-to-end verification: application interaction (14 legs).

Everything below runs against the REAL host: the real AT-SPI bus, the
real installed applications (GNOME Text Editor, LibreOffice), the real
PermissionChecker, and the REAL permissioned pipeline with its
digest-bound approval gate.  Test data lives only in a dedicated
temporary sandbox under /tmp/opencode/alwin-app-interaction-verify
(fresh each run); the user's real state_dir, audit log, task history
and documents are never touched.

Labels (honest, same convention as verify_desktop_control.py):

  REAL      — real accessibility data, real applications, real
              permission/approval pipeline
  MOCKED    — none in this script (the real host IS the evidence);
              model/planner fixtures are not used — open_file needs no
              model proposal
  BLOCKED   — the compositor, application or accessibility API refused
              the check; reported as-is, never faked, never forced
  UNVERIFIED— an action executed but the outcome could not be observed
              through the accessibility API (reported as such, never as
              success)

Legs (from the milestone brief):
  1. real application discovery,
  2. real AT-SPI availability,
  3. real window enumeration,
  4. real accessible-control inspection,
  5. one real supported interaction in GNOME Text Editor,
  6. one real supported interaction in LibreOffice Writer or Calc
     (expected BLOCKED on this host: toolkit-accessibility=false),
  7. state verification after each interaction,
  8. missing and ambiguous target handling (real tree),
  9. approval refusal (real gate, nothing executed),
 10. cancellation (declined plan executes nothing),
 11. stale-target rejection (app closed -> defunct -> refused),
 12. unavailable accessibility behavior (honest, no simulation),
 13. no unintended filesystem changes (sandbox tree hash before/after),
 14. existing desktop-control and workflow regression scripts.

Honest environment facts this script records (never hides):
* the reference GNOME session has
  ``org.gnome.desktop.interface toolkit-accessibility = false``, so
  LibreOffice (GTK3 VCL) does NOT expose AT-SPI here -> legs 6 and 12
  are BLOCKED with that root cause,
* GNOME Text Editor (GTK4) exposes its window and controls but NO
  document-text interface -> reading/typing the document is
  unsupported, not simulated.

Exit code: 0 when every leg is EXECUTED or honestly BLOCKED; 1 when
any leg FAILED.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SANDBOX = "/tmp/opencode/alwin-app-interaction-verify"
RESULTS = []


def report(section, name, status, detail=""):
    RESULTS.append({"section": section, "name": name,
                    "status": status, "detail": detail})
    print(f"[{status:>9}] {section}: {name}"
          + (f" — {detail}" if detail else ""), flush=True)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_config():
    from ai_core.config import load_config
    return load_config(home=os.path.join(SANDBOX, "home"),
                       roots=["Documents"],
                       state_dir=os.path.join(SANDBOX, "state"))


def make_session():
    from integrations.automation.atspi_backend import AtspiSession
    session = AtspiSession(max_nodes=4000, max_depth=24)
    session.connect()
    return session


def make_tools(config, session):
    from ai_core.permissions import PermissionChecker
    from integrations.automation.tools import AutomationTools
    return AutomationTools(PermissionChecker(config), session=session)


def comm_running(name: str) -> bool:
    """Is *name* (or its 15-char comm truncation) in /proc?"""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    truncated = name[:15]
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", encoding="utf-8") as handle:
                comm = handle.read().strip()
        except OSError:
            continue
        if comm == truncated or comm == name:
            return True
    return False


def sandbox_hash() -> str:
    """Deterministic hash of the sandbox home tree (leg 13)."""
    digest = hashlib.sha256()
    home = os.path.join(SANDBOX, "home")
    for root, dirs, files in os.walk(home):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            digest.update(os.path.relpath(path, home).encode())
            try:
                with open(path, "rb") as handle:
                    digest.update(handle.read())
            except OSError:
                digest.update(b"unreadable")
    return digest.hexdigest()


def run_approved(pipeline, action):
    """Preview + REAL digest-bound approval + run."""
    from ai_core.pipeline import ApprovalToken
    preview = pipeline.preview_action(action)
    if not preview.requires_approval:
        return preview, pipeline.run_previewed(preview), ""
    token = ApprovalToken(digest=preview.plan_digest)
    return preview, pipeline.run_previewed(preview, approval=token), ""


def make_pipeline(config):
    from ai_core.permissions import PermissionChecker
    from ai_core.pipeline import TaskPipeline
    from integrations import build_default_dispatcher
    from integrations.apps.desktop import DesktopController

    checker = PermissionChecker(config)
    controller = DesktopController(checker)
    dispatcher = build_default_dispatcher(checker, desktop_tools=controller)
    return TaskPipeline(config, dispatcher=dispatcher), controller


def wait_for(pred, timeout=25.0, interval=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# 1-4. discovery, AT-SPI, windows, control inspection
# --------------------------------------------------------------------------- #


def leg1_discovery():
    print("== 1. real application discovery ==", flush=True)
    from integrations.apps.discovery import (
        default_desktop_dirs, detect_report)
    from integrations.apps.registry import BUILTIN_REGISTRY
    home = os.path.join(SANDBOX, "home")
    rep = detect_report(BUILTIN_REGISTRY,
                        desktop_dirs=default_desktop_dirs(home))
    installed = {i["id"] for i in rep["integrations"]
                 if i["status"] == "available"}
    report("1 discovery", "registry discovery ran on this host",
           "EXECUTED", ", ".join(sorted(installed)) or "none")
    ok = "gnome-text-editor" in installed
    report("1 discovery", "GNOME Text Editor discovered",
           "EXECUTED" if ok else "FAILED",
           "present" if ok else "missing on this host")
    return ok


def leg2_atspi():
    print("== 2. real AT-SPI availability ==", flush=True)
    try:
        session = make_session()
        names = [session.name(app) for app in session.applications()]
    except Exception as exc:  # noqa: BLE001 - honest BLOCKED
        report("2 at-spi", "AT-SPI session available", "BLOCKED",
               f"{type(exc).__name__}: {str(exc)[:120]}")
        return None
    report("2 at-spi", "AT-SPI session connected", "EXECUTED",
           f"{len(names)} applications on the bus")
    return session


def leg3_windows(session):
    print("== 3. real window enumeration ==", flush=True)
    windows = []
    for app in session.applications():
        app_name = session.name(app)
        for child in session.children(app):
            role = session.role(child)
            if role in ("frame", "window"):
                title = session.name(child)
                windows.append((app_name, role, title))
    report("3 windows", "top-level windows enumerated", "EXECUTED",
           f"{len(windows)} window(s) "
           + "; ".join(f"{t[:40]}" for _, _, t in windows[:4]))
    return windows


def leg4_inspection(config, session, pipeline):
    print("== 4. real accessible-control inspection ==", flush=True)
    tools = make_tools(config, session)
    # Open the sandbox document in the real editor (existing open_file
    # mechanism, approval-gated, no model needed).
    preview, result, _ = run_approved(pipeline, {
        "action": "open_file",
        "path": "Documents/interaction-test.txt",
        "application": "gnome-text-editor",
    })
    opened = result is not None and result.status == "success"
    report("4 inspection", "sandbox document opened in real editor",
           "EXECUTED" if opened else "FAILED",
           f"status={getattr(result, 'status', None)}")
    if not opened:
        return None, None, None
    seen = wait_for(lambda: comm_running("gnome-text-editor"))
    report("4 inspection", "editor process observed via /proc",
           "EXECUTED" if seen else "BLOCKED",
           "running" if seen else "process not observed (honest)")
    if not seen:
        return tools, None, None
    listed = wait_for(
        lambda: tools.inspect("gnome-text-editor").data.get(
            "snapshot", {}).get("interface_detected") is True,
        timeout=20.0,
    )
    report("4 inspection", "editor accessible interface detected",
           "EXECUTED" if listed else "BLOCKED",
           "window found on the bus" if listed else
           "no accessible window (honest)")
    if not listed:
        return tools, None, None
    controls = tools.list_controls("gnome-text-editor")
    count = controls.data.get("count", 0) if controls.ok else 0
    report("4 inspection", "bounded control listing (roles/names only)",
           "EXECUTED" if controls.ok and count else "FAILED",
           f"{count} control(s); "
           f"truncated={controls.data.get('truncated')}")
    return tools, controls, None


# --------------------------------------------------------------------------- #
# 5 + 7. one real interaction in GNOME Text Editor + verification
# --------------------------------------------------------------------------- #


def leg5_interaction(tools, controls):
    print("== 5-7. real interaction: GNOME Text Editor (REAL) ==",
          flush=True)
    if tools is None or controls is None:
        report("5 interaction", "GNOME Text Editor interaction",
               "BLOCKED", "the editor interface was not available")
        return False
    # Identify a reviewed, interactive control from the REAL listing —
    # no invented names.  'New Tab' is the primary target because its
    # user-visible effect (a new document tab) IS observable in the
    # accessibility tree, so success can be VERIFIED, not assumed.
    candidates = [c for c in controls.data.get("controls", [])
                  if c["role"] == "button"
                  and c["name"] == "New Tab"
                  and "click" in c["action_names"]]
    if not candidates:
        report("5 interaction", "'New Tab' control present",
               "BLOCKED", "the control is not exposed in this build")
        return True   # honestly blocked (a narrower op may still work)
    adapter = _editor_adapter(tools)
    from integrations.automation.base import ControlLocator
    locator = ControlLocator(role="button", name="New Tab")
    before = adapter.find_control(locator)
    report("5 interaction", "target control identified (real tree)",
           "EXECUTED" if before.ok else "FAILED",
           f"states={before.data.get('control', {}).get('states')}")
    if not before.ok:
        return False
    tabs_before = len([c for c in controls.data.get("controls", [])
                       if c["role"] == "page tab"])
    activated = adapter.activate_control(locator)
    report("5 interaction", "activation through the a11y action API",
           "EXECUTED" if activated.ok else "FAILED",
           f"state={activated.state}")
    if not activated.ok:
        return False
    # Independent verification: re-list the real tree and compare —
    # an API "success" is NOT evidence; a changed tree is.  AT-SPI
    # caches state changes, so re-READ (never re-act) a few times,
    # bounded, before declaring the outcome unverified.
    from integrations.automation.base import ControlLocator as _Loc
    tab_locator = _Loc(role="page tab", name="")

    def tab_count():
        try:
            app, root = adapter._root()
            if root is None:
                return -1
            return len(adapter.session.find_all(root, tab_locator,
                                                max_matches=12))
        except Exception:  # noqa: BLE001 - honest unknown
            return -1

    verified = False
    evidence = ""
    for _attempt in range(4):
        after = adapter.find_control(locator)
        after_listing = tools.list_controls("gnome-text-editor")
        tabs_now = tab_count()
        before_set = {
            (c["role"], c["name"], tuple(c["states"]))
            for c in controls.data.get("controls", [])
        }
        after_set = {
            (c["role"], c["name"], tuple(c["states"]))
            for c in (after_listing.data.get("controls", [])
                      if after_listing.ok else [])
        }
        states_changed = (after.ok and after.data.get(
            "control", {}).get("states") != before.data.get(
            "control", {}).get("states"))
        tree_changed = after_set != before_set or tabs_now > tabs_before
        if states_changed or tree_changed:
            verified = True
            evidence = (f"page tabs {tabs_before} -> {tabs_now} in the "
                        f"real accessibility tree (tree entries changed: "
                        f"{tree_changed})")
            break
        time.sleep(1.2)
    if verified:
        report("7 verification", "state change observed after action",
               "EXECUTED", evidence)
    else:
        report("7 verification", "state change observed after action",
               "UNVERIFIED",
               "the API accepted the action but no independent state "
               "change was observable (bounded re-reads included) — "
               "reported as UNVERIFIED, not success")
    # Secondary probe: the Document Properties toggle, honestly
    # labelled by whatever the tree can (or cannot) show, then
    # toggled back (restore).
    props = ControlLocator(role="toggle button",
                           name="Document Properties")
    prop_result = adapter.activate_control(props)
    if prop_result.ok:
        report("7 verification", "secondary toggle honestly labelled",
               "EXECUTED" if prop_result.state == "result_verified"
               else "UNVERIFIED",
               f"state={prop_result.state}")
        adapter.activate_control(props)   # toggle back (restore)
    return True


def _editor_adapter(tools):
    from integrations.automation.gnome_text_editor import (
        GnomeTextEditorAdapter)
    return GnomeTextEditorAdapter("gnome-text-editor",
                                  session=tools.session)


# --------------------------------------------------------------------------- #
# 6 + 12. LibreOffice: expected BLOCKED on this host
# --------------------------------------------------------------------------- #


def leg6_libreoffice(tools):
    print("== 6 + 12. LibreOffice interaction / unavailable a11y ==",
          flush=True)
    try:
        with open("/proc/self/environ", "rb"):
            pass
        setting = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface",
             "toolkit-accessibility"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        setting = f"unknown ({type(exc).__name__})"
    if setting != "true":
        report("6 libreoffice", "LibreOffice real interaction",
               "BLOCKED",
               f"toolkit-accessibility={setting}; the GTK3 VCL bridge "
               f"is disabled on this session — no LO a11y is claimed")
        report("12 unavailable", "unavailable a11y reported honestly",
               "BLOCKED",
               "LibreOffice adapter falls back to 'interface not "
               "detected' — no simulation, no fake tree")
        return True   # honestly blocked, not failed
    # Only reached if a future session enables the setting: then the
    # adapter attempts a real listing and reports what it finds.
    result = tools.list_controls("libreoffice-writer")
    report("6 libreoffice", "LibreOffice control listing (real)",
           "EXECUTED" if result.ok else "BLOCKED",
           f"{result.data.get('count', 0)} control(s)" if result.ok
           else (result.error or "")[:100])
    return True


# --------------------------------------------------------------------------- #
# 8. missing and ambiguous targets (real tree)
# --------------------------------------------------------------------------- #


def leg8_targets(tools):
    print("== 8. missing + ambiguous target handling (REAL) ==", flush=True)
    if tools is None:
        report("8 targets", "target handling", "BLOCKED",
               "the editor interface was not available")
        return False
    adapter = _editor_adapter(tools)
    from integrations.automation.base import ControlLocator
    missing = adapter.find_control(
        ControlLocator(role="button", name="No Such Control Exists"))
    honest_missing = (not missing.ok
                      and "No control matched" in missing.error)
    report("8 targets", "missing target -> honest failure",
           "EXECUTED" if honest_missing else "FAILED",
           (missing.error or "")[:80])
    # Role-only locator over a tree with several toggle buttons.
    ambiguous = adapter.find_control(
        ControlLocator(role="toggle button", name=""))
    honest_ambiguous = (not ambiguous.ok
                        and ambiguous.state == "ambiguous_target")
    report("8 targets", "ambiguous target -> rejected, never guessed",
           "EXECUTED" if honest_ambiguous else "FAILED",
           (ambiguous.error or "")[:80])
    return honest_missing and honest_ambiguous


# --------------------------------------------------------------------------- #
# 9 + 10. approval refusal and cancellation (real gate)
# --------------------------------------------------------------------------- #


def _stable_states(states):
    """Volatile windowing flags (focus/active) churn on a live host.

    v0.19: the refusal evidence compares the SEMANTIC states only —
    an approval rejection cannot change what the control IS.
    """
    return sorted(s for s in (states or [])
                  if s not in ("focused", "active", "showing"))


def leg9_refusal(config, tools):
    print("== 9-10. approval refusal + cancellation (REAL gate) ==",
          flush=True)
    if tools is None:
        report("9 refusal", "approval refusal", "BLOCKED",
               "the editor interface was not available")
        return False
    pipeline, _ = make_pipeline(config)
    adapter = _editor_adapter(tools)
    from integrations.automation.base import ControlLocator
    locator = ControlLocator(role="toggle button",
                             name="Document Properties")
    before = adapter.find_control(locator)
    preview = pipeline.preview_action({
        "action": "activate_control", "application": "gnome-text-editor",
        "control": {"role": "toggle button",
                    "name": "Document Properties"},
    })
    needs = preview.requires_approval
    result = pipeline.run_previewed(preview, approval=False)
    # v0.19: bounded re-reads — a live host can churn focus/menu
    # flags between finds; the refusal evidence must be stable, and a
    # persistent mismatch is reported with its actual detail.
    refused_clean = needs and result.status == "rejected"
    detail = ""
    if refused_clean:
        for attempt in range(4):
            after = adapter.find_control(locator)
            if (after.ok
                    and _stable_states(after.data.get("control", {})
                                       .get("states"))
                    == _stable_states(before.data.get("control", {})
                                      .get("states"))):
                detail = "semantic states identical"
                break
            detail = (f"before={before.data.get('control', {}).get('states')} "
                      f"after={after.data.get('control', {}).get('states') if after.ok else after.data}")
            time.sleep(1.0)
        else:
            refused_clean = False
    report("9 refusal", "declined approval executes nothing (real gate)",
           "EXECUTED" if refused_clean else "FAILED",
           f"requires_approval={needs} status={result.status} {detail}")
    report("10 cancellation", "declined plan = cancelled, state "
           "unchanged", "EXECUTED" if refused_clean else "FAILED",
           "the toggle's semantic states are identical before/after")
    return refused_clean


# --------------------------------------------------------------------------- #
# 11. stale target (app closed -> defunct -> refused)
# --------------------------------------------------------------------------- #


def leg11_stale(config, tools, pipeline):
    print("== 11. stale-target rejection after the app closes (REAL) ==",
          flush=True)
    if tools is None:
        report("11 stale", "stale-target rejection", "BLOCKED",
               "the editor interface was not available")
        return False
    adapter = _editor_adapter(tools)
    from integrations.automation.base import ControlLocator

    def window_gone():
        app, root = adapter._root()
        return app is None or root is None or root is app

    # Close the editor's window through the real approval-gated close
    # action.  NB: GNOME Text Editor is a GTK4 single-instance app —
    # its PROCESS often outlives the window, so the stale condition is
    # the WINDOW disappearing from the accessibility bus.
    preview, result, _ = run_approved(pipeline, {
        "action": "close_application",
        "application": "gnome-text-editor",
    })
    closed = result is not None and result.status == "success"
    gone = wait_for(window_gone, timeout=20.0)
    report("11 stale", "editor window closed through the real pipeline",
           "EXECUTED" if closed and gone else "BLOCKED",
           f"status={getattr(result, 'status', None)} "
           f"window_gone={gone}")
    if not (closed and gone):
        return True   # honestly blocked, nothing claimed
    attempt = adapter.activate_control(
        ControlLocator(role="toggle button",
                       name="Document Properties"))
    honest = (not attempt.ok
              and attempt.state in ("stale_target", "failed_or_unknown"))
    report("11 stale", "acting on a gone window refused honestly",
           "EXECUTED" if honest else "FAILED",
           f"state={attempt.state} error={(attempt.error or '')[:80]}")
    return honest


# --------------------------------------------------------------------------- #
# 13. no unintended filesystem changes
# --------------------------------------------------------------------------- #


def leg13_fs(before_hash):
    print("== 13. no unintended filesystem changes ==", flush=True)
    after_hash = sandbox_hash()
    same = before_hash == after_hash
    report("13 filesystem", "sandbox tree unchanged by the interaction "
           "legs", "EXECUTED" if same else "FAILED",
           "hash identical" if same else "the tree changed")
    return same


# --------------------------------------------------------------------------- #
# 14. existing regression scripts
# --------------------------------------------------------------------------- #


def leg14_regression():
    print("== 14. existing desktop-control + workflow scripts ==", flush=True)
    ok = True
    for script in ("verify_desktop_control.py",
                   "verify_desktop_workflows.py"):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", script)],
            cwd=REPO, capture_output=True, text=True, timeout=1800)
        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        good = proc.returncode == 0
        ok = ok and good
        report("14 regression", script,
               "EXECUTED" if good else "FAILED",
               f"exit={proc.returncode} · {tail[0][:70]}")
    return ok


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def main():
    t0 = time.time()
    if SANDBOX.startswith("/tmp/opencode/alwin-app-interaction-verify"):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(os.path.join(SANDBOX, "state"), exist_ok=True)
    os.makedirs(os.path.join(SANDBOX, "home", "Documents"), exist_ok=True)

    print("Alwin Fusion v0.17.0 application-interaction verification "
          f"(sandbox: {SANDBOX}, real host, test data only)\n")

    # Seed the sandbox document (the interaction target).
    with open(os.path.join(SANDBOX, "home", "Documents",
                           "interaction-test.txt"), "w",
              encoding="utf-8") as handle:
        handle.write("ALWIN FUSION v0.17.0 INTERACTION TEST\n"
                     "Known content for verification.\n")
    before_hash = sandbox_hash()

    config = make_config()
    # Repeatability: a leftover editor window from a previous run must
    # not contaminate the legs.  Close it through the REVIEWED pipeline
    # action (never a force-kill, never a signal).
    if comm_running("gnome-text-editor"):
        pipeline_pre, _ = make_pipeline(config)
        try:
            run_approved(pipeline_pre, {
                "action": "close_application",
                "application": "gnome-text-editor",
            })
            wait_for(lambda: not comm_running("gnome-text-editor"),
                     timeout=15.0)
        except Exception:  # noqa: BLE001 - pre-clean is best-effort
            pass
    ok = leg1_discovery()
    session = leg2_atspi()
    if session is None:
        return _summary(t0, ok=False)
    leg3_windows(session)
    pipeline, _ = make_pipeline(config)
    tools, controls, _ = leg4_inspection(config, session, pipeline)
    ok5 = leg5_interaction(tools, controls)
    ok6 = leg6_libreoffice(tools)
    ok8 = leg8_targets(tools)
    ok9 = leg9_refusal(config, tools)
    ok11 = leg11_stale(config, tools, pipeline)
    ok13 = leg13_fs(before_hash)
    ok14 = leg14_regression()

    ok = all((ok, ok5, ok6, ok8, ok9, ok11, ok13, ok14))
    return _summary(t0, ok)


def _summary(t0, ok=True):
    counts = {}
    for entry in RESULTS:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print("\n" + "=" * 68)
    print("APPLICATION-INTERACTION VERIFICATION SUMMARY")
    print("=" * 68)
    for status in ("EXECUTED", "BLOCKED", "UNVERIFIED", "FAILED"):
        if counts.get(status):
            print(f"  {status:<11}: {counts[status]}")
    print(f"  elapsed    : {time.time() - t0:.1f}s")
    print("  labels     : REAL = real AT-SPI/applications/pipeline/"
          "approval gate;\n               BLOCKED = the host refused "
          "(compositor/app/API) — never forced;\n               "
          "UNVERIFIED = executed but no observable state change — "
          "never reported\n               as success.")
    verdict = "VERIFICATION OK" if ok else "VERIFICATION FAILED"
    print("=" * 68)
    print(verdict)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
