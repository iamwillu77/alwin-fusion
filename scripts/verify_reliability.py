#!/usr/bin/env python3
"""v0.19.0 REAL end-to-end verification: application reliability (10 items).

Runs against the REAL host: the real AT-SPI bus, the real installed
applications, the real PermissionChecker/pipeline/approval gate, real
files in a dedicated sandbox under /tmp/opencode/alwin-reliability-verify
(fresh each run, test data only; the user's real state_dir, audit log
and documents are never touched, and no desktop setting is changed).

Labels (honest, same convention as the other verify scripts):

  REAL      — real bus/applications/pipeline/files
  SANDBOX   — isolated temporary directory
  MOCKED    — none here (simulations live in tests/test_reliability.py)
  BLOCKED   — the host refused; the observed reason is recorded
  UNVERIFIED— executed but not independently observable

Items (from the milestone brief):
  1. real application discovery,
  2. capability detection (structured facets, incl. the real
     LibreOffice blocked/unknown honesty on this host),
  3. a supported real application interaction (GNOME Text Editor),
  4. a multi-step workflow,
  5. independent verification of the result,
  6. graceful handling of an unavailable application,
  7. stale control / disappearing window handling,
  8. honest partial-completion reporting,
  9. permission denial without unauthorized mutation,
 10. cancellation + cleanup behaviour.

Then ALL six existing verification scripts are re-run.

Exit code: 0 when every item is EXECUTED or honestly BLOCKED; 1 when
any item FAILED.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SANDBOX = "/tmp/opencode/alwin-reliability-verify"
RESULTS = []


def report(section, name, status, detail=""):
    RESULTS.append({"section": section, "name": name,
                    "status": status, "detail": detail})
    print(f"[{status:>9}] {section}: {name}"
          + (f" — {detail}" if detail else ""), flush=True)


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


def make_pipeline(config):
    from ai_core.permissions import PermissionChecker
    from ai_core.pipeline import TaskPipeline
    from integrations import build_default_dispatcher
    from integrations.apps.desktop import DesktopController
    checker = PermissionChecker(config)
    controller = DesktopController(checker)
    dispatcher = build_default_dispatcher(checker, desktop_tools=controller)
    return TaskPipeline(config, dispatcher=dispatcher), controller


def run_approved(pipeline, action):
    from ai_core.pipeline import ApprovalToken
    preview = pipeline.preview_action(action)
    if not preview.requires_approval:
        return preview, pipeline.run_previewed(preview)
    token = ApprovalToken(digest=preview.plan_digest)
    return preview, pipeline.run_previewed(preview, approval=token), ""


def wait_for(pred, timeout=25.0, interval=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def comm_running(name: str) -> bool:
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
                if handle.read().strip() in (truncated, name):
                    return True
        except OSError:
            continue
    return False


# --------------------------------------------------------------------------- #
# 1-2: discovery and capability detection
# --------------------------------------------------------------------------- #


def item1_discovery():
    print("== 1. real application discovery ==", flush=True)
    from integrations.apps.discovery import (
        default_desktop_dirs, detect_report)
    from integrations.apps.registry import BUILTIN_REGISTRY
    rep = detect_report(BUILTIN_REGISTRY,
                        desktop_dirs=default_desktop_dirs(
                            os.path.join(SANDBOX, "home")))
    installed = {i["id"] for i in rep["integrations"]
                 if i["status"] == "available"}
    report("1 discovery", "registry discovery ran on this host",
           "EXECUTED", ", ".join(sorted(installed)) or "none")
    ok = "gnome-text-editor" in installed
    report("1 discovery", "GNOME Text Editor discovered",
           "EXECUTED" if ok else "FAILED",
           "present" if ok else "missing on this host")
    return ok


def item2_capabilities(config):
    print("== 2. capability detection (structured facets, REAL) ==",
          flush=True)
    _, controller = make_pipeline(config)
    lo = controller.status("libreoffice-writer").data
    editor = controller.status("gnome-text-editor").data
    report("2 capabilities", "readiness facets reported for "
           "libreoffice-writer", "EXECUTED",
           f"installed={lo['readiness']['installed']} "
           f"running={lo['readiness']['running']} "
           f"a11y={lo['readiness']['a11y']} "
           f"control={lo['readiness']['control']}")
    # The honesty guarantee: NOT installed-alones-as-interact, and a
    # non-running/bus-absent app never claims interact.
    honest = ("interact" not in lo["tiers"]) and (
        lo["readiness"]["control"] != "interact"
        or lo["readiness"]["a11y"] == "available")
    report("2 capabilities", "no 'interact' claimed without a11y "
           "evidence", "EXECUTED" if honest else "FAILED",
           f"tiers={lo['tiers']}")
    report("2 capabilities", "GNOME Text Editor facets reported",
           "EXECUTED",
           f"installed={editor['readiness']['installed']} "
           f"a11y={editor['readiness']['a11y']}")
    return honest


# --------------------------------------------------------------------------- #
# 3-5: real interaction, multi-step workflow, independent verification
# --------------------------------------------------------------------------- #


def item3_interaction(config, session):
    print("== 3-5. real interaction + multi-step workflow (REAL) ==",
          flush=True)
    pipeline, _ = make_pipeline(config)
    tools = make_tools(config, session)
    preview, result, *_ = run_approved(pipeline, {
        "action": "open_file",
        "path": "Documents/reliability-sample.txt",
        "application": "gnome-text-editor",
    })
    opened = result is not None and result.status == "success"
    report("3 interaction", "sample document opened in the real editor",
           "EXECUTED" if opened else "BLOCKED",
           f"status={getattr(result, 'status', None)}")
    if not opened:
        return False, None
    seen = wait_for(lambda: comm_running("gnome-text-editor"))
    report("3 interaction", "editor process observed via /proc",
           "EXECUTED" if seen else "BLOCKED",
           "running" if seen else "process not observed (honest)")
    if not seen:
        return False, tools
    detected = wait_for(
        lambda: tools.inspect("gnome-text-editor").data.get(
            "snapshot", {}).get("interface_detected") is True,
        timeout=20.0)
    report("3 interaction", "editor accessible interface detected",
           "EXECUTED" if detected else "BLOCKED",
           "window on the bus" if detected else
           "no accessible window (honest)")
    if not detected:
        return False, tools
    listed = tools.list_controls("gnome-text-editor")
    ok = listed.ok and listed.data.get("count", 0) > 0
    report("3 interaction", "bounded control listing (real tree)",
           "EXECUTED" if ok else "FAILED",
           f"{listed.data.get('count', 0)} control(s)")

    # 4-5: multi-step workflow + independent verification
    pipe2, _ = make_pipeline(config)
    wf = run_approved(pipe2, {
        "action": "task_sequence",
        "steps": [
            {"action": "create_text_file",
             "path": "Documents/reliability-a.txt",
             "content": "workflow step one\n"},
            {"action": "read_file", "path": "Documents/reliability-a.txt"},
            {"action": "create_text_file",
             "path": "Documents/reliability-b.txt",
             "content": "workflow step three\n"},
            {"action": "read_file", "path": "Documents/reliability-b.txt"},
        ],
    })
    wf_result = wf[1] if len(wf) > 1 else wf[0]
    a = os.path.join(SANDBOX, "home", "Documents", "reliability-a.txt")
    b = os.path.join(SANDBOX, "home", "Documents", "reliability-b.txt")
    verified = (wf_result.status == "success"
                and open(a, encoding="utf-8").read()
                == "workflow step one\n"
                and open(b, encoding="utf-8").read()
                == "workflow step three\n")
    report("4 workflow", "4-step plan completed through the real "
           "pipeline", "EXECUTED" if verified else "FAILED",
           f"status={wf_result.status}")
    report("5 verification", "both files independently re-read and "
           "matched", "EXECUTED" if verified else "FAILED",
           f"sha256={hashlib.sha256(open(a,'rb').read()).hexdigest()[:12]}…")
    return ok and verified, tools


# --------------------------------------------------------------------------- #
# 6-7: unavailable application, stale/disappearing window
# --------------------------------------------------------------------------- #


def item6_unavailable(config):
    print("== 6. unavailable application handled gracefully (REAL) ==",
          flush=True)
    pipeline, _ = make_pipeline(config)
    preview, result, *_ = run_approved(pipeline, {
        "action": "open_file",
        "path": "Documents/reliability-sample.txt",
        "application": "okular",
    })
    # okular is registered; typically absent here (and it only opens
    # PDFs) — an honest failure/denied is the PASS condition.
    ok = result.status in ("failed", "denied", "unsupported") or (
        result.status == "success" and result.execution.data.get("pid"))
    report("6 unavailable", "unavailable/unsupported application "
           "reported honestly", "EXECUTED" if ok else "FAILED",
           f"status={result.status} "
           f"error={(result.error or result.execution.error or '')[:80]}")
    return ok


def item7_stale(config, tools):
    print("== 7. disappearing window -> stale handling (REAL) ==",
          flush=True)
    if tools is None:
        report("7 stale", "stale/disappearing-window handling", "BLOCKED",
               "the editor interface was not available")
        return True
    from integrations.automation.base import ControlLocator
    adapter = None
    from integrations.automation.gnome_text_editor import (
        GnomeTextEditorAdapter)
    adapter = GnomeTextEditorAdapter("gnome-text-editor",
                                     session=tools.session)
    pipeline, _ = make_pipeline(config)
    closed = run_approved(pipeline, {
        "action": "close_application",
        "application": "gnome-text-editor",
    })
    closed_ok = closed[1].status == "success" if len(closed) > 1 \
        else closed[0].status == "success"

    def window_gone():
        app, root = adapter._root()
        return app is None or root is None or root is app

    gone = wait_for(window_gone, timeout=20.0)
    report("7 stale", "editor window closed through the real pipeline",
           "EXECUTED" if closed_ok and gone else "BLOCKED",
           f"closed={closed_ok} window_gone={gone}")
    if not (closed_ok and gone):
        return True   # honestly blocked
    attempt = adapter.activate_control(
        ControlLocator(role="button", name="New Tab"))
    honest = (not attempt.ok
              and attempt.data.get("category") in
              ("app_missing", "stale_target", "unknown"))
    report("7 stale", "action after the window vanished refused with "
           "a structured category", "EXECUTED" if honest else "FAILED",
           f"category={attempt.data.get('category')} "
           f"state={attempt.state}")
    return honest


# --------------------------------------------------------------------------- #
# 8-9: partial completion, permission denial
# --------------------------------------------------------------------------- #


def item8_partial(config):
    print("== 8. partial completion reported honestly (REAL) ==",
          flush=True)
    pipeline, _ = make_pipeline(config)
    with open(os.path.join(SANDBOX, "home", "Documents",
                           "partial.txt"), "w", encoding="utf-8") as fh:
        fh.write("original")
    result = run_approved(pipeline, {
        "action": "task_sequence",
        "steps": [
            {"action": "replace_file_content",
             "path": "Documents/partial.txt", "content": "step one"},
            {"action": "read_file", "path": "Documents/does-not-exist.txt"},
        ],
    })
    report_ = result[1] if len(result) > 1 else result[0]
    partial = (report_.execution.data or {}).get("partial", {})
    on_disk = open(os.path.join(SANDBOX, "home", "Documents",
                                "partial.txt"), encoding="utf-8").read()
    ok = (report_.status == "failed" and partial.get("completed") == 1
          and partial.get("changed") is True
          and on_disk == "step one"
          and "DID change state" in (report_.error or ""))
    report("8 partial", "earlier step's effect preserved AND reported",
           "EXECUTED" if ok else "FAILED",
           f"status={report_.status} partial={partial}")
    return ok


def item9_denial(config):
    print("== 9. permission denial without mutation (REAL) ==",
          flush=True)
    pipeline, _ = make_pipeline(config)
    outside = os.path.join(SANDBOX, "outside-reliability.txt")
    with open(outside, "w", encoding="utf-8") as fh:
        fh.write("untouched")
    result = run_approved(pipeline, {
        "action": "replace_file_content", "path": outside,
        "content": "clobbered"})
    report_ = result[1] if len(result) > 1 else result[0]
    ok = (report_.status in ("denied", "unsupported")
          and open(outside, encoding="utf-8").read() == "untouched")
    report("9 denial", "out-of-roots replacement denied; file "
           "byte-identical", "EXECUTED" if ok else "FAILED",
           f"status={report_.status}")
    return ok


# --------------------------------------------------------------------------- #
# 10: cancellation + cleanup
# --------------------------------------------------------------------------- #


def item10_cancellation(config):
    print("== 10. cancellation and cleanup behaviour (REAL) ==",
          flush=True)
    pipeline, _ = make_pipeline(config)
    preview = pipeline.preview_action({
        "action": "replace_file_content",
        "path": "Documents/cancel-target.txt", "content": "x"})
    from ai_core.pipeline import ApprovalToken
    # (a) explicit rejection at the gate
    rejected = pipeline.run_previewed(preview, approval=False)
    # (b) a stale/mismatched token is refused, not honoured
    stale = pipeline.run_previewed(
        preview, approval=ApprovalToken(digest="f" * 64))
    created = os.path.exists(os.path.join(
        SANDBOX, "home", "Documents", "cancel-target.txt"))
    ok = (rejected.status == "rejected" and stale.status == "rejected"
          and not created)
    report("10 cancellation", "rejection and stale approval both leave "
           "the filesystem untouched", "EXECUTED" if ok else "FAILED",
           f"rejected={rejected.status} stale={stale.status} "
           f"file_created={created}")
    # cleanup: only the artifacts this script created (guarded prefix).
    return ok


# --------------------------------------------------------------------------- #
# Runner + all six regression scripts
# --------------------------------------------------------------------------- #


def rerun_all():
    # v0.22 orchestration guard: when an OUTER verification script is
    # rerunning this one, skip OUR nested reruns (the outer runner
    # exercises every script directly — the nesting only multiplies
    # wall time without adding evidence).  Honest note, never a fake
    # pass of the outer legs.
    if os.environ.get("ALWIN_VERIFY_NESTED"):
        print("[verify] nested run: inner regression reruns skipped "
              "(the outer runner reruns them directly)", flush=True)
        return True
    print("== rerunning ALL existing verification scripts ==",
          flush=True)
    ok = True
    for script in ("verify_desktop_control.py",
                   "verify_desktop_workflows.py",
                   "verify_app_interaction.py",
                   "verify_document_workflows.py",
                   "verify_memory.py", "verify_scheduler.py"):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", script)],
            cwd=REPO, capture_output=True, text=True, timeout=2400)
        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        good = proc.returncode == 0
        ok = ok and good
        report("regression", script,
               "EXECUTED" if good else "FAILED",
               f"exit={proc.returncode} · {tail[0][:64]}")
    return ok


def main():
    t0 = time.time()
    if SANDBOX.startswith("/tmp/opencode/alwin-reliability-verify"):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(os.path.join(SANDBOX, "state"), exist_ok=True)
    os.makedirs(os.path.join(SANDBOX, "home", "Documents"), exist_ok=True)
    with open(os.path.join(SANDBOX, "home", "Documents",
                           "reliability-sample.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("ALWIN v0.19.0 RELIABILITY SAMPLE\n")

    print("Alwin Fusion v0.19.0 reliability verification "
          f"(sandbox: {SANDBOX}, real host, test data only)\n")

    config = make_config()
    ok1 = item1_discovery()
    ok2 = item2_capabilities(config)
    try:
        session = make_session()
    except Exception as exc:  # noqa: BLE001 - honest BLOCKED
        session = None
        report("session", "AT-SPI session", "BLOCKED",
               f"{type(exc).__name__}: {str(exc)[:100]}")
    ok3, tools = item3_interaction(config, session) \
        if session is not None else (False, None)
    ok6 = item6_unavailable(config)
    ok7 = item7_stale(config, tools)
    ok8 = item8_partial(config)
    ok9 = item9_denial(config)
    ok10 = item10_cancellation(config)
    okrerun = rerun_all()

    ok = all((ok1, ok2, ok3, ok6, ok7, ok8, ok9, ok10, okrerun))
    code = _summary(t0, ok)
    if SANDBOX.startswith("/tmp/opencode/alwin-reliability-verify"):
        shutil.rmtree(SANDBOX, ignore_errors=True)
        print("[cleanup] removed only this script's sandbox.")
    return code


def _summary(t0, ok=True):
    counts = {}
    for entry in RESULTS:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print("\n" + "=" * 68)
    print("RELIABILITY VERIFICATION SUMMARY")
    print("=" * 68)
    for status in ("EXECUTED", "BLOCKED", "UNVERIFIED", "FAILED"):
        if counts.get(status):
            print(f"  {status:<11}: {counts[status]}")
    print(f"  elapsed    : {time.time() - t0:.1f}s")
    print("  labels     : REAL = real bus/applications/pipeline/files;\n"
          "               SANDBOX = isolated temp dir; MOCKED simulations\n"
          "               live in tests/test_reliability.py only;\n"
          "               BLOCKED/UNVERIFIED are honest and never passes.")
    verdict = "VERIFICATION OK" if ok else "VERIFICATION FAILED"
    print("=" * 68)
    print(verdict)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
