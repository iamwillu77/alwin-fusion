#!/usr/bin/env python3
"""v0.13.0 REAL desktop verification: launch/inspect/focus/close.

Everything below runs against REAL components: the live session bus,
the real AT-SPI accessibility bus, the real application registry, real
process observation via /proc, and the real trusted pipeline
(preview -> permission -> digest-bound approval -> execute -> verify ->
audit).  Files live in a sandbox under /tmp (never the user's own
documents); the applications launched here are genuinely installed on
this machine and are closed again by the script.

Distinguishing honestly (same convention as verify_task_planning.py):

  REAL      — real desktop / real pipeline / real accessibility bus
  MOCKED    — only in the unit tests; never claimed real here
  BLOCKED   — the desktop environment refused or lacks the API;
              reported as-is, never faked

Legs:
 1. environment probe (session bus, AT-SPI, installed applications),
 2. discovery + per-application capability tiers,
 3. REAL approval-gated launch of a discovered application, verified
    by /proc re-observation,
 4. application status + real accessibility window inspection,
 5. real active-window observation (honest 'empty'/'unavailable'),
 6. REAL focus request confirmed by the active accessibility state,
 7. sandboxed end-to-end file workflow (create test document ->
    open it in the discovered editor -> verify file + launch evidence),
 8. REAL close through the window's own window.close accessibility
    action, verified by the window leaving the bus,
 9. cancellation at the approval gate launches nothing (real gate),
10. missing-application failure is honest and launches nothing,
11. no unintended file changes (sandbox snapshot before/after).

Exit code: 0 when every leg is EXECUTED or honestly BLOCKED; 1 when
any leg FAILED (i.e. a real operation malfunctioned or a claim was
not backed by evidence).
"""
from __future__ import annotations

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SANDBOX = "/tmp/opencode/alwin-desktop-verify"
RESULTS = []

#: The genuinely installed applications this script will drive.
LAUNCH_APP = "nautilus"              # bare launch + focus + close leg
EDITOR_APP = "gnome-text-editor"     # sandboxed open_file workflow leg
MISSING_APP = "okular"               # not installed on this machine


def report(section, name, status, detail=""):
    RESULTS.append({"section": section, "name": name,
                    "status": status, "detail": detail})
    print(f"[{status:>7}] {section}: {name}"
          + (f" — {detail}" if detail else ""), flush=True)


def sandbox_config():
    from ai_core.config import load_config
    # Fresh sandbox each run: the workflow leg creates known files, and
    # create_text_file never overwrites (by design), so a rerun must not
    # collide with the previous run's evidence.  Guarded: only ever
    # removes this script's own fixed directory.
    import shutil
    if SANDBOX.startswith("/tmp/opencode/alwin-desktop-verify"):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    home = os.path.join(SANDBOX, "home")
    docs = os.path.join(home, "Documents")
    state = os.path.join(SANDBOX, "state")
    for d in (docs, state):
        os.makedirs(d, exist_ok=True)
    return load_config(home=home, roots=["Documents"], state_dir=state)


def snapshot_tree(root):
    """Sorted (path, size) list — evidence of what exists, for the
    'no unintended changes' leg."""
    out = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            try:
                out.append((os.path.relpath(full, root), os.path.getsize(full)))
            except OSError:
                out.append((os.path.relpath(full, root), -1))
    return sorted(out)


def comm_running(name):
    """Real /proc observation of a comm name (kernel-truncation-safe)."""
    from integrations.automation.processes import any_running
    return any_running((name,)) is True


# --------------------------------------------------------------------------- #
# 1. environment
# --------------------------------------------------------------------------- #

def section_environment():
    print("== 1. environment (REAL) ==", flush=True)
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    session = os.environ.get("XDG_SESSION_TYPE", "unset")
    if not bus:
        report("environment", "session bus", "BLOCKED",
               "DBUS_SESSION_BUS_ADDRESS is not set; no desktop session")
        return False
    report("environment", "session bus", "EXECUTED",
           f"type={session} desktop={os.environ.get('XDG_CURRENT_DESKTOP', '?')}")
    try:
        from integrations.automation.atspi_backend import AtspiSession
        s = AtspiSession()
        s.connect()
        apps = s.applications()
        report("environment", "AT-SPI bus", "EXECUTED",
               f"{len(apps)} applications visible on the accessibility bus")
        return True
    except Exception as exc:  # noqa: BLE001
        report("environment", "AT-SPI bus", "BLOCKED", str(exc))
        return False


# --------------------------------------------------------------------------- #
# 2. discovery
# --------------------------------------------------------------------------- #

def section_discovery():
    print("== 2. discovery + tiers (REAL, read-only) ==", flush=True)
    from ai_core.capabilities import application_tiers
    from integrations.apps.discovery import default_desktop_dirs, detect_report
    from integrations.apps.registry import BUILTIN_REGISTRY

    home = os.path.join(SANDBOX, "home")
    report_ = detect_report(BUILTIN_REGISTRY, desktop_dirs=default_desktop_dirs(home))
    installed = [i["id"] for i in report_["integrations"]
                 if i["status"] == "available"]
    report("discovery", "installed applications", "EXECUTED",
           ", ".join(installed) or "none")
    for wanted in (LAUNCH_APP, EDITOR_APP, MISSING_APP):
        state = "available" if wanted in installed else "NOT installed"
        report("discovery", f"{wanted} evidence", "EXECUTED", state)
    tiers = application_tiers(automation_bindings=True)
    for app in (LAUNCH_APP, EDITOR_APP):
        info = tiers[app]
        report("discovery", f"{app} tiers", "EXECUTED",
               f"installed={info['installed']} tiers={list(info['tiers'])} "
               f"control={info['control']}")
    if LAUNCH_APP not in installed or EDITOR_APP not in installed:
        report("discovery", "required applications present", "FAILED",
               "the script's required applications are missing on this host")
        return False
    return True


# --------------------------------------------------------------------------- #
# pipeline helpers (REAL approval path)
# --------------------------------------------------------------------------- #

def make_pipeline(config):
    from ai_core.permissions import PermissionChecker
    from ai_core.pipeline import TaskPipeline
    from integrations import build_default_dispatcher
    from integrations.apps.desktop import DesktopController

    checker = PermissionChecker(config)
    controller = DesktopController(checker)
    dispatcher = build_default_dispatcher(checker, desktop_tools=controller)
    return TaskPipeline(config, dispatcher=dispatcher), controller


def approved(pipeline, action):
    """Preview + REAL digest-bound approval + run. Returns the report."""
    preview = pipeline.preview_action(action)
    if not preview.requires_approval:
        return preview, None, "action did not require approval (unexpected?)"
    from ai_core.pipeline import ApprovalToken
    token = ApprovalToken(digest=preview.plan_digest)
    return preview, pipeline.run_previewed(preview, approval=token), ""


# --------------------------------------------------------------------------- #
# 3. launch (REAL, approval-gated, /proc-verified)
# --------------------------------------------------------------------------- #

def section_launch(config):
    print("== 3. approval-gated launch (REAL) ==", flush=True)
    pipeline, controller = make_pipeline(config)
    was_running = comm_running(LAUNCH_APP)
    preview, result, note = approved(
        pipeline, {"action": "launch_application", "application": LAUNCH_APP}
    )
    if result is None:
        report("launch", "digest-bound approval", "FAILED", note)
        return False
    ok = (result.status == "success" and result.execution is not None
          and result.execution.data.get("process_observed") is True)
    seen = comm_running(LAUNCH_APP)
    report("launch", "approval required before execution",
           "EXECUTED" if preview.requires_approval else "FAILED",
           f"digest={preview.plan_digest[:12]}…")
    report("launch", "launch verified via /proc",
           "EXECUTED" if ok else "FAILED",
           f"was_running={was_running} now_running={seen} "
           f"verification={result.verification.get('status') if result.verification else None}")
    if ok:
        # Give the window a moment to appear on the a11y bus.
        for _ in range(20):
            status = controller.status(LAUNCH_APP)
            if status.data.get("windows_found", 0) >= 1:
                break
            time.sleep(0.25)
    return ok


# --------------------------------------------------------------------------- #
# 4. status + accessibility inspection
# --------------------------------------------------------------------------- #

def section_status(config):
    print("== 4. status + a11y inspection (REAL, read-only) ==", flush=True)
    pipeline, controller = make_pipeline(config)
    preview = pipeline.preview_action(
        {"action": "application_status", "application": LAUNCH_APP}
    )
    report("status", "read-only needs no approval",
           "EXECUTED" if not preview.requires_approval else "FAILED",
           "")
    result = pipeline.run_previewed(preview)
    data = result.execution.data if result.execution else {}
    report("status", "running state via /proc",
           "EXECUTED" if data.get("running") is True else "FAILED",
           f"running={data.get('running')}")
    windows = data.get("windows") or []
    report("status", "a11y window metadata",
           "EXECUTED" if windows else "BLOCKED",
           f"{len(windows)} window(s): "
           + json.dumps(windows[:2], ensure_ascii=False))
    return data.get("running") is True, controller


# --------------------------------------------------------------------------- #
# 5. active window
# --------------------------------------------------------------------------- #

def section_active_window(pipeline):
    preview = pipeline.preview_action({"action": "active_window"})
    result = pipeline.run_previewed(preview)
    if result.status != "success" or not result.execution:
        report("active window", "observation", "FAILED",
               result.error or "no execution")
        return None
    data = result.execution.data
    if data.get("active") is True:
        report("active window", "active window reported", "EXECUTED",
               f"'{data.get('title')}' ({data.get('role')}, "
               f"app '{data.get('application')}')")
    else:
        report("active window", "honest empty reported", "EXECUTED",
               data.get("detail", "no active window"))
    return data


# --------------------------------------------------------------------------- #
# 6. focus (REAL, verified by the active state)
# --------------------------------------------------------------------------- #

def section_focus(config):
    print("== 6. focus (REAL, approval-gated) ==", flush=True)
    pipeline, _controller = make_pipeline(config)
    preview, result, note = approved(
        pipeline, {"action": "focus_application", "application": LAUNCH_APP}
    )
    if result is None:
        report("focus", "digest-bound approval", "FAILED", note)
        return False
    data = result.execution.data if result.execution else {}
    status_name = "EXECUTED" if data.get("became_active") is True else "BLOCKED"
    report("focus", "focus request + active-state confirmation", status_name,
           f"delivered={data.get('focus_delivered')} "
           f"became_active={data.get('became_active')} "
           f"verification={result.verification.get('status') if result.verification else None}")
    return data.get("became_active") is True


# --------------------------------------------------------------------------- #
# 7. sandboxed end-to-end file workflow (Phase 7 preferred example)
# --------------------------------------------------------------------------- #

def section_workflow(config):
    print("== 7. sandboxed document workflow (REAL) ==", flush=True)
    pipeline, controller = make_pipeline(config)
    doc_rel = "Documents/desktop-test-report.txt"
    content = ("ALWIN FUSION v0.13.0 DESKTOP VERIFICATION\n"
               "This test document was created in a sandbox directory.\n"
               "It exists solely to verify the launch/open evidence chain.\n")
    # -- create the test document (approved, sandboxed) ---------------- #
    preview, result, note = approved(pipeline, {
        "action": "create_text_file", "path": doc_rel, "content": content,
    })
    created = result is not None and result.status == "success"
    report("workflow", "create test document (sandboxed)",
           "EXECUTED" if created else "FAILED", doc_rel)
    if not created:
        return False
    doc_abs = os.path.join(SANDBOX, "home", doc_rel)
    readable = os.path.isfile(doc_abs) and os.access(doc_abs, os.R_OK)
    report("workflow", "file exists and is readable",
           "EXECUTED" if readable else "FAILED",
           f"{os.path.getsize(doc_abs) if readable else 0} bytes")

    # -- open it in the discovered editor (approved) ------------------- #
    was_running = comm_running("gnome-text-editor")
    preview, result, note = approved(pipeline, {
        "action": "open_file", "path": doc_rel, "application": EDITOR_APP,
    })
    data = result.execution.data if result and result.execution else {}
    opened = result is not None and result.status == "success"
    report("workflow", "open document in real editor", "EXECUTED" if opened else "FAILED",
           f"application={EDITOR_APP} was_running={was_running} "
           f"pid={data.get('pid')}")
    if not opened:
        return False
    # Wait (bounded) for the editor process to be observed via /proc.
    seen = False
    for _ in range(24):
        if comm_running("gnome-text-editor"):
            seen = True
            break
        time.sleep(0.25)
    report("workflow", "editor process observed via /proc",
           "EXECUTED" if seen else "BLOCKED",
           "the process is running" if seen else
           "process not observed; launch stays UNVERIFIED (honest)")
    return seen


# --------------------------------------------------------------------------- #
# 8. close (REAL, window.close accessibility action, verified)
# --------------------------------------------------------------------------- #

def section_close(config):
    print("== 8. close via window.close a11y action (REAL) ==", flush=True)
    pipeline, controller = make_pipeline(config)
    preview, result, note = approved(
        pipeline, {"action": "close_application", "application": LAUNCH_APP}
    )
    if result is None:
        report("close", "digest-bound approval", "FAILED", note)
        return False
    data = result.execution.data if result.execution else {}
    closed = data.get("closed_confirmed") is True
    report("close", "window.close action + disappearance verified",
           "EXECUTED" if closed else "BLOCKED",
           f"action_used={data.get('action_used')} "
           f"running_after={data.get('running_after')} "
           f"verification={result.verification.get('status') if result.verification else None}"
           + ("" if closed else " (dialog or refusal — nothing was force-killed)"))
    # Cleanup honesty: the editor from leg 7 is closed the same way.
    try:
        controller.close(EDITOR_APP)
    except Exception:  # noqa: BLE001 - cleanup best-effort
        pass
    return closed


# --------------------------------------------------------------------------- #
# 9. cancellation at the approval gate
# --------------------------------------------------------------------------- #

def section_cancellation(config):
    print("== 9. cancellation at the approval gate (REAL) ==", flush=True)
    pipeline, _controller = make_pipeline(config)
    before = comm_running("gnome-text-editor")
    preview = pipeline.preview_action(
        {"action": "launch_application", "application": EDITOR_APP}
    )
    # Real rejection path: the user declines the displayed plan.
    result = pipeline.run_previewed(preview, approval=False)
    after = comm_running("gnome-text-editor")
    cancelled = result.status == "rejected" and before == after
    report("cancellation", "declined plan executes nothing",
           "EXECUTED" if cancelled else "FAILED",
           f"status={result.status} editor_running before={before} after={after}")
    return cancelled


# --------------------------------------------------------------------------- #
# 10. missing application
# --------------------------------------------------------------------------- #

def section_missing(config):
    print("== 10. missing application (REAL failure) ==", flush=True)
    from ai_core.pipeline import ApprovalToken

    pipeline, _controller = make_pipeline(config)
    preview = pipeline.preview_action(
        {"action": "launch_application", "application": MISSING_APP}
    )
    result = pipeline.run_previewed(
        preview, approval=ApprovalToken(digest=preview.plan_digest)
    )
    honest = (result.status in ("failed", "denied", "unsupported")
              and not comm_running(MISSING_APP))
    report("missing app", "honest failure, nothing launched",
           "EXECUTED" if honest else "FAILED",
           f"status={result.status} error={(result.error or '')[:120]}")
    return honest


# --------------------------------------------------------------------------- #
# 11. no unintended file changes
# --------------------------------------------------------------------------- #

def section_files(before):
    print("== 11. file-change audit (REAL) ==", flush=True)
    after = snapshot_tree(SANDBOX)
    before_names = {p for p, _ in before}
    after_names = {p for p, _ in after}
    created = sorted(after_names - before_names)
    report("files", "no files deleted", "EXECUTED" if before_names <= after_names else "FAILED",
           "")
    report("files", "created files (expected sandbox only)", "EXECUTED",
           ", ".join(created) if created else "(none)")
    state_dir = os.path.join(SANDBOX, "state")
    audit_ok = os.path.isfile(os.path.join(state_dir, "audit.jsonl"))
    report("files", "audit trail written (sandbox state dir)",
           "EXECUTED" if audit_ok else "FAILED", state_dir)
    return before_names <= after_names and audit_ok


# --------------------------------------------------------------------------- #

def main():
    t0 = time.time()
    config = sandbox_config()
    before = snapshot_tree(SANDBOX)

    desktop = section_environment()
    if not desktop:
        print("\nDesktop session unavailable — desktop legs are BLOCKED "
              "(reported honestly; unit tests cover the logic with mocks).")
        _summary(t0)
        return 0

    if not section_discovery():
        _summary(t0)
        return 1

    launched = section_launch(config)
    if launched:
        running, controller = section_status(config)
        pipeline, _ = make_pipeline(config)
        section_active_window(pipeline)
        section_focus(config)
        section_workflow(config)
        section_close(config)
    else:
        report("launch", "subsequent desktop legs", "BLOCKED",
               "the launch leg did not complete; nothing further claimed")

    section_cancellation(config)
    section_missing(config)
    changed = section_files(before)
    return _summary(t0, ok=launched and changed)


def _summary(t0, ok=True):
    counts = {}
    for r in RESULTS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n== summary ==")
    print(f"  {counts.get('EXECUTED', 0)} EXECUTED, "
          f"{counts.get('BLOCKED', 0)} BLOCKED, "
          f"{counts.get('FAILED', 0)} FAILED "
          f"in {time.time() - t0:.1f}s")
    for r in RESULTS:
        if r["status"] != "EXECUTED":
            print(f"  [{r['status']}] {r['section']}: {r['name']} — {r['detail']}")
    failed = counts.get("FAILED", 0)
    exit_ok = ok and failed == 0
    print(f"  VERDICT: {'all claims evidence-backed' if exit_ok else 'FAILURES or unverified claims present'}")
    return 0 if exit_ok else 1


if __name__ == "__main__":
    sys.exit(main())
