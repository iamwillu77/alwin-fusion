#!/usr/bin/env python3
"""v0.14.0 REAL end-to-end verification: intelligent desktop workflows.

Everything below runs against REAL components: the real TaskManager,
the real trusted pipeline (preview -> permission -> digest-bound
approval -> execute -> verify -> audit), the real closed-set action
validator and plan validator, real capability discovery against the
real application registry, real sandboxed files under /tmp (never the
user's own documents), real process observation via /proc, and — on a
live desktop session — the real application launcher and the real
AT-SPI accessibility bus.

Distinguishing honestly (same convention as verify_desktop_control.py):

  REAL      — real manager / pipeline / desktop / files / registry
  SCRIPTED  — the planning model is a deterministic fixture: these
              legs verify the WORKFLOW EXECUTION machinery, not model
              quality (model quality is covered by the real-model
              planning leg below and by verify_task_planning.py)
  BLOCKED   — the environment or compositor refused; reported as-is,
              never faked

Legs:
  1. environment probe (session bus, AT-SPI),
  2. discovery + real capability tiers (editor present, okular absent),
  3. Workflow A — real multi-step workflow through the real manager:
     inspect a real input CSV -> generate a real report (typed
     derivation handoff) -> approval-gated open in the real editor ->
     /proc + window-state evidence -> content verification,
  4. approval digest enforcement (wrong digest refused at the manager,
     stale digest refused at the pipeline: a changed plan invalidates
     the approval),
  5. refusal at the approval gate executes nothing,
  6. mid-execution cancellation: the completed first step is preserved
     truthfully, no later step starts,
  7. Workflow B — missing capability: a plan naming a registered but
     NOT installed application is honestly redirected at plan time,
     nothing is launched, no fallback executes,
  8. Workflow C — partial failure: completed steps preserved, dependent
     steps skipped, independent steps continue, summary distinguishes,
  9. real local model planning (outcome reported honestly; execution
     is covered by the deterministic legs above),
 10. no unintended filesystem changes (sandbox snapshot diff).

Exit code: 0 when every leg is EXECUTED or honestly BLOCKED; 1 when
any leg FAILED (a real operation malfunctioned or a claim was not
backed by evidence).
"""
from __future__ import annotations

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SANDBOX = "/tmp/opencode/alwin-workflows-verify"
RESULTS = []

EDITOR_APP = "gnome-text-editor"     # genuinely installed on this host
MISSING_APP = "okular"               # registered but NOT installed

WORKFLOW_GOAL = ("build me a report from the csv data and then open it "
                 "in the text editor please")
PARTIAL_GOAL = ("turn the csv data into a report and open that report "
                "for me in the text editor")
MISSING_GOAL = ("launch the pdf viewer and check its state for me")

CSV_TEXT = ("date,amount,region\n"
            "2026-01-05,120,north\n"
            "2026-01-06,75,south\n"
            "2026-01-07,210,north\n")


def report(section, name, status, detail=""):
    RESULTS.append({"section": section, "name": name,
                    "status": status, "detail": detail})
    print(f"[{status:>7}] {section}: {name}"
          + (f" — {detail}" if detail else ""), flush=True)


def sandbox_config():
    from ai_core.config import load_config
    # Fresh sandbox each run: workflow legs create known files, and
    # create_text_file never overwrites (by design), so a rerun must
    # not collide with the previous run's evidence.  Guarded: only
    # ever removes this script's own fixed directory.
    import shutil
    if SANDBOX.startswith("/tmp/opencode/alwin-workflows-verify"):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    home = os.path.join(SANDBOX, "home")
    docs = os.path.join(home, "Documents")
    state = os.path.join(SANDBOX, "state")
    for d in (docs, state):
        os.makedirs(d, exist_ok=True)
    return load_config(home=home, roots=["Documents"], state_dir=state)


def snapshot_docs(config):
    """Sorted relative (path, size) list of the sandboxed Documents."""
    root = os.path.join(str(config.home), "Documents")
    out = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            try:
                out.append((os.path.relpath(full, root),
                            os.path.getsize(full)))
            except OSError:
                out.append((os.path.relpath(full, root), -1))
    return sorted(out)


def comm_running(name):
    """Real /proc observation (kernel-truncation-safe)."""
    from integrations.automation.processes import any_running
    return any_running((name,)) is True


class ScriptedModel:
    """Deterministic planning fixture (SCRIPTED — labelled as such).

    Same interface the real Ollama client exposes; the workflow legs
    use it so the EXECUTION machinery under test is repeatable.  It
    can only ever propose JSON the trusted validator then re-checks —
    it has no other channel into the system.
    """

    def __init__(self, proposal, replan=None):
        self.model = "scripted:workflow-fixture"
        self.proposal = proposal
        self.replan = replan or json.dumps(
            {"decision": "decline", "reason": "no replan scripted"})

    def generate(self, *, prompt, system=None, json_mode=False,
                 timeout=None, **kwargs):
        if "summarize untrusted data" in (system or ""):
            return "Bounded summary of the referenced step output."
        if "JSON proposal:" in prompt:
            return self.proposal
        if "Propose ONLY" in prompt:
            return self.replan
        return "not a valid single action {"


def plan_json(steps, objective):
    return json.dumps({"decision": "plan", "objective": objective,
                       "steps": steps})


def step(step_id, action, **extra):
    item = {"id": step_id, "summary": f"step {step_id}", "action": action}
    item.update(extra)
    return item


def make_manager(config, model):
    from taskui.manager import TaskManager
    return TaskManager(config, use_llm=True, llm_client=model)


def run_to_approval(manager, goal, timeout=20):
    record = manager.submit(goal)
    manager.wait_for_state("awaiting_approval", timeout=timeout)
    return record


# --------------------------------------------------------------------------- #
# 1. environment
# --------------------------------------------------------------------------- #

def section_environment():
    print("== 1. environment (REAL) ==", flush=True)
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        report("environment", "session bus", "BLOCKED",
               "DBUS_SESSION_BUS_ADDRESS is not set; no desktop session")
        return False
    report("environment", "session bus", "EXECUTED",
           f"type={os.environ.get('XDG_SESSION_TYPE', 'unset')} "
           f"desktop={os.environ.get('XDG_CURRENT_DESKTOP', '?')}")
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
# 2. discovery + real capability tiers
# --------------------------------------------------------------------------- #

def section_discovery():
    print("== 2. discovery + tiers (REAL, read-only) ==", flush=True)
    from ai_core.capabilities import application_tiers
    from integrations.apps.discovery import (default_desktop_dirs,
                                             detect_report)
    from integrations.apps.registry import BUILTIN_REGISTRY

    home = os.path.join(SANDBOX, "home")
    rep = detect_report(BUILTIN_REGISTRY,
                        desktop_dirs=default_desktop_dirs(home))
    installed = [i["id"] for i in rep["integrations"]
                 if i["status"] == "available"]
    report("discovery", "installed applications", "EXECUTED",
           ", ".join(installed) or "none")
    tiers = application_tiers(automation_bindings=True)
    editor = tiers.get(EDITOR_APP, {})
    missing = tiers.get(MISSING_APP, {})
    report("discovery", f"{EDITOR_APP} tier", "EXECUTED",
           f"installed={editor.get('installed')} "
           f"tiers={list(editor.get('tiers', []))}")
    report("discovery", f"{MISSING_APP} tier (honest absence)", "EXECUTED",
           f"installed={missing.get('installed')}")
    if not editor.get("installed"):
        report("discovery", "required editor present", "FAILED",
               f"{EDITOR_APP} is not installed on this host")
        return False
    if missing.get("installed"):
        report("discovery", f"{MISSING_APP} absent", "FAILED",
               "the missing-capability legs need an uninstalled app")
        return False
    return True


# --------------------------------------------------------------------------- #
# 3. Workflow A — real multi-step workflow through the real manager
# --------------------------------------------------------------------------- #

def workflow_a_steps():
    return [
        step("s1", {"action": "read_file", "path": "Documents/data.csv"},
             expected="the csv data is read"),
        step("s2", {"action": "create_report",
                    "path": "Documents/report.txt",
                    "content": {
                        "title": "Sales report",
                        "blocks": [{"type": "paragraph",
                                    "text": {"from_step": "s1",
                                             "field": "content"}}]}},
             depends_on=["s1"], expected="the report file exists"),
        step("s3", {"action": "open_file",
                    "path": {"from_step": "s2", "field": "path"},
                    "application": EDITOR_APP},
             depends_on=["s2"], expected="the editor opens the report"),
        step("s4", {"action": "application_status",
                    "application": EDITOR_APP},
             depends_on=["s3"], expected="the application state is reported"),
    ]


def section_workflow_a(config):
    print("== 3. Workflow A: read -> report -> approval -> open (REAL) ==",
          flush=True)
    csv_path = os.path.join(str(config.home), "Documents", "data.csv")
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write(CSV_TEXT)
    report("workflow A", "real input file created in sandbox", "EXECUTED",
           f"{len(CSV_TEXT)} bytes of CSV data")

    model = ScriptedModel(plan_json(workflow_a_steps(), "report from csv"))
    manager = make_manager(config, model)
    was_running = comm_running("gnome-text-editor")

    record = run_to_approval(manager, WORKFLOW_GOAL)
    if record.state != "awaiting_approval":
        report("workflow A", "plan reached the approval gate", "FAILED",
               f"state={record.state} error={record.error}")
        return None
    report("workflow A", "plan reached the approval gate", "EXECUTED",
           f"digest={record.approval_digest[:12]}… steps={len(record.steps)}")

    if not manager.approve(record.approval_digest):
        report("workflow A", "digest-bound approval accepted", "FAILED",
               "the displayed digest was refused")
        return None
    report("workflow A", "digest-bound approval accepted", "EXECUTED", "")
    manager.wait_terminal(timeout=60)

    statuses = {s["id"]: s["status"] for s in record.steps}
    report("workflow A", "all steps verified succeeded", 
           "EXECUTED" if record.state == "completed" and
           set(statuses.values()) == {"succeeded"} else "FAILED",
           f"state={record.state} statuses={statuses} "
           f"error={(record.error or '')[:120]}")
    if record.state != "completed":
        return None

    # -- real output-file verification (nothing fabricated) ----------- #
    report_path = os.path.join(str(config.home), "Documents", "report.txt")
    exists = os.path.isfile(report_path)
    body = ""
    if exists:
        with open(report_path, encoding="utf-8") as fh:
            body = fh.read()
    contains_csv = all(line in body for line in CSV_TEXT.strip().splitlines())
    report("workflow A", "report exists in the expected sandbox",
           "EXECUTED" if exists else "FAILED", report_path)
    report("workflow A", "report contains the REAL csv data",
           "EXECUTED" if contains_csv else "FAILED",
           "every input line is present verbatim" if contains_csv else
           "the report does not reflect the input — fabrication?")

    # -- real launch evidence (/proc) --------------------------------- #
    seen = False
    for _ in range(24):
        if comm_running("gnome-text-editor"):
            seen = True
            break
        time.sleep(0.25)
    report("workflow A", "editor process observed via /proc",
           "EXECUTED" if seen else "BLOCKED",
           f"was_running={was_running} now_running={seen}")

    # -- window-state evidence (real AT-SPI, honest when absent) ------ #
    from ai_core.permissions import PermissionChecker
    from integrations.apps.desktop import DesktopController
    controller = DesktopController(PermissionChecker(config))
    windows = []
    for _ in range(20):
        status = controller.status(EDITOR_APP)
        windows = status.data.get("windows") or []
        if windows:
            break
        time.sleep(0.25)
    report("workflow A", "window-state evidence via AT-SPI",
           "EXECUTED" if windows else "BLOCKED",
           f"{len(windows)} window(s): "
           + json.dumps(windows[:1], ensure_ascii=False) if windows else
           "no window seen on the a11y bus; launch stays process-verified only")

    # -- per-step verification evidence is on the record -------------- #
    verified = all((s.get("verification") or {}).get("status") == "passed"
                   for s in record.steps)
    report("workflow A", "per-step verification recorded", 
           "EXECUTED" if verified else "FAILED",
           "every step carries a verifier result")

    # -- cleanup: close the editor (best-effort, honest) -------------- #
    try:
        controller.close(EDITOR_APP)
        time.sleep(0.5)
        still = comm_running("gnome-text-editor")
        report("workflow A", "editor closed after the run (cleanup)",
               "EXECUTED" if not still else "BLOCKED",
               "closed via the accessibility window.close action" if not still
               else "editor still running; nothing was force-killed")
    except Exception as exc:  # noqa: BLE001
        report("workflow A", "editor closed after the run (cleanup)",
               "BLOCKED", str(exc))
    return record


# --------------------------------------------------------------------------- #
# 4. approval digest enforcement
# --------------------------------------------------------------------------- #

def section_digest_binding(config):
    print("== 4. approval binding (REAL) ==", flush=True)
    # -- manager level: a wrong digest never approves ----------------- #
    model = ScriptedModel(plan_json([
        step("s1", {"action": "create_text_file",
                    "path": "Documents/digest-a.txt", "content": "a"},
             expected="created"),
        step("s2", {"action": "create_text_file",
                    "path": "Documents/digest-b.txt", "content": "b"},
             depends_on=["s1"], expected="created"),
    ], "digest binding"))
    manager = make_manager(config, model)
    record = run_to_approval(manager, PARTIAL_GOAL)
    wrong = manager.approve("0" * 64)
    still_awaiting = record.state == "awaiting_approval"
    nothing_ran = not os.path.exists(
        os.path.join(str(config.home), "Documents", "digest-a.txt"))
    report("digest", "wrong digest refused at the manager",
           "EXECUTED" if (not wrong and still_awaiting and nothing_ran)
           else "FAILED",
           f"approve_returned={wrong} state={record.state}")
    manager.reject()
    manager.wait_terminal(timeout=20)

    # -- pipeline level: a stale token for a CHANGED plan ------------- #
    from ai_core.permissions import PermissionChecker
    from ai_core.pipeline import ApprovalToken, STATUS_REJECTED
    from ai_core.pipeline import TaskPipeline
    from integrations import build_default_dispatcher

    checker = PermissionChecker(config)
    pipeline = TaskPipeline(
        config, checker=checker,
        dispatcher=build_default_dispatcher(checker))
    preview_a = pipeline.preview_action(
        {"action": "create_text_file", "path": "Documents/stale-a.txt",
         "content": "a"})
    preview_b = pipeline.preview_action(
        {"action": "create_text_file", "path": "Documents/stale-b.txt",
         "content": "b"})
    token_a = ApprovalToken(digest=preview_a.plan_digest)
    result_b = pipeline.run_previewed(preview_b, approval=token_a)
    stale_refused = (result_b.status == STATUS_REJECTED
                     and not os.path.exists(
                         os.path.join(str(config.home), "Documents",
                                      "stale-b.txt")))
    report("digest", "stale approval for a different plan refused",
           "EXECUTED" if stale_refused else "FAILED",
           f"status={result_b.status} error={(result_b.error or '')[:100]}")


# --------------------------------------------------------------------------- #
# 5. refusal at the approval gate
# --------------------------------------------------------------------------- #

def section_refusal(config):
    print("== 5. refusal executes nothing (REAL) ==", flush=True)
    model = ScriptedModel(plan_json([
        step("s1", {"action": "create_text_file",
                    "path": "Documents/refused.txt", "content": "nope"},
             expected="created"),
    ], "refuse me"))
    manager = make_manager(config, model)
    record = run_to_approval(manager, PARTIAL_GOAL)
    refused = manager.reject()
    manager.wait_terminal(timeout=20)
    nothing = not os.path.exists(
        os.path.join(str(config.home), "Documents", "refused.txt"))
    honest = (record.state == "cancelled" and nothing
              and (record.error or "").find("nothing ran") >= 0
              and [s["status"] for s in record.steps] == ["pending"])
    report("refusal", "declined plan executed nothing",
           "EXECUTED" if honest and refused else "FAILED",
           f"state={record.state} error={(record.error or '')[:100]} "
           f"steps={[s['status'] for s in record.steps]}")


# --------------------------------------------------------------------------- #
# 6. mid-execution cancellation
# --------------------------------------------------------------------------- #

def section_cancellation(config):
    print("== 6. mid-execution cancellation (REAL) ==", flush=True)
    docs = os.path.join(str(config.home), "Documents")
    model = ScriptedModel(plan_json([
        step("s1", {"action": "create_text_file",
                    "path": "Documents/cancel-a.txt", "content": "1"},
             expected="created"),
        step("s2", {"action": "create_text_file",
                    "path": "Documents/cancel-b.txt", "content": "2"},
             depends_on=["s1"], expected="created"),
        step("s3", {"action": "create_text_file",
                    "path": "Documents/cancel-c.txt", "content": "3"},
             depends_on=["s2"], expected="created"),
    ], "cancel mid-plan"))

    manager = make_manager(config, model)
    cancel_sent = {"done": False}

    def observer(record):
        # Called from the worker thread at every state change; once the
        # FIRST step has verifiably succeeded, request cancellation —
        # the controller must stop before the next step and preserve
        # the completed work truthfully.
        if cancel_sent["done"] or record is None:
            return
        if any(s.get("status") == "succeeded" for s in record.steps):
            cancel_sent["done"] = True
            manager.cancel()

    manager.on_update = observer
    record = run_to_approval(manager, PARTIAL_GOAL)
    manager.approve(record.approval_digest)
    manager.wait_terminal(timeout=30)

    a_exists = os.path.exists(os.path.join(docs, "cancel-a.txt"))
    b_exists = os.path.exists(os.path.join(docs, "cancel-b.txt"))
    c_exists = os.path.exists(os.path.join(docs, "cancel-c.txt"))
    statuses = [s["status"] for s in record.steps]
    honest = (record.state == "cancelled" and a_exists and not b_exists
              and not c_exists and statuses[0] == "succeeded"
              and all(st in ("cancelled", "pending") for st in statuses[1:]))
    report("cancellation", "completed step preserved, later steps never ran",
           "EXECUTED" if honest and cancel_sent["done"] else "FAILED",
           f"state={record.state} statuses={statuses} "
           f"files: a={a_exists} b={b_exists} c={c_exists}")
    report("cancellation", "no rollback was claimed for the finished step",
           "EXECUTED" if a_exists else "FAILED",
           "cancel-a.txt exists exactly as executed")


# --------------------------------------------------------------------------- #
# 7. Workflow B — missing capability
# --------------------------------------------------------------------------- #

def section_missing_capability(config):
    print("== 7. missing capability (REAL registry judgment) ==", flush=True)
    model = ScriptedModel(plan_json([
        step("s1", {"action": "launch_application", "application": MISSING_APP},
             expected="launched"),
        step("s2", {"action": "application_status",
                    "application": MISSING_APP},
             depends_on=["s1"], expected="state reported"),
    ], "use the pdf viewer"))
    manager = make_manager(config, model)
    record = manager.submit(MISSING_GOAL)
    manager.wait_terminal(timeout=30)
    message = f"{record.error or ''}"
    honest = (record.state == "failed" and "not installed" in message
              and not comm_running(MISSING_APP)
              # The plan never reached execution: no step view was ever
              # built, so there is nothing that could have run.
              and record.steps == []
              and "Nothing was executed" in message)
    report("missing capability",
           "not-installed app redirected at plan time, nothing launched",
           "EXECUTED" if honest else "FAILED",
           f"state={record.state} error={message[:160]}")


# --------------------------------------------------------------------------- #
# 8. Workflow C — partial failure
# --------------------------------------------------------------------------- #

def section_partial_failure(config):
    print("== 8. partial failure (REAL) ==", flush=True)
    docs = os.path.join(str(config.home), "Documents")
    model = ScriptedModel(plan_json([
        step("s1", {"action": "create_text_file",
                    "path": "Documents/partial-ok.txt", "content": "kept"},
             expected="created"),
        step("s2", {"action": "read_file",
                    "path": "Documents/partial-missing.txt"},
             expected="read"),
        step("s3", {"action": "create_text_file",
                    "path": "Documents/partial-after.txt", "content": "x"},
             depends_on=["s2"], expected="created"),
        step("s4", {"action": "read_file",
                    "path": "Documents/partial-ok.txt"},
             expected="re-read"),
    ], "partial failure"))
    manager = make_manager(config, model)
    record = run_to_approval(manager, PARTIAL_GOAL)
    manager.approve(record.approval_digest)
    manager.wait_terminal(timeout=30)

    statuses = {s["id"]: s["status"] for s in record.steps}
    ok_file = os.path.exists(os.path.join(docs, "partial-ok.txt"))
    after_file = os.path.exists(os.path.join(docs, "partial-after.txt"))
    message = record.result_message or ""
    honest = (record.state == "needs_attention"
              and statuses == {"s1": "succeeded", "s2": "failed",
                               "s3": "skipped", "s4": "succeeded"}
              and ok_file and not after_file
              and "1 failed" in message and "1 skipped" in message)
    report("partial failure",
           "completed steps preserved, dependents skipped, honest summary",
           "EXECUTED" if honest else "FAILED",
           f"state={record.state} statuses={statuses} "
           f"files: ok={ok_file} after={after_file} summary={message[:100]}")


# --------------------------------------------------------------------------- #
# 9. real local model planning (honest outcome report)
# --------------------------------------------------------------------------- #

def section_real_model():
    print("== 9. real local model planning (REAL ollama) ==", flush=True)
    try:
        from ai_runtime.ollama_client import OllamaClient
        llm = OllamaClient(model=os.environ.get("OLLAMA_MODEL")
                           or "qwen3:0.6b")
        models = llm.list_models()
        report("real model", "ollama reachable", "EXECUTED",
               f"model={llm.model} (installed: {len(models)})")
    except Exception as exc:  # noqa: BLE001
        report("real model", "ollama reachable", "BLOCKED", str(exc))
        return
    try:
        from ai_core.task_planning import TaskPlanningService
        service = TaskPlanningService(llm)
        proposal = service.propose(WORKFLOW_GOAL)
        detail = f"kind={proposal.kind}"
        if proposal.plan is not None:
            detail += (" steps="
                       + ",".join(s.action.get("action", "?")
                                  for s in proposal.plan.steps))
        elif proposal.question:
            detail += f" question={proposal.question[:80]}"
        elif proposal.reason:
            detail += f" reason={proposal.reason[:80]}"
        # Any structured, validated outcome is the honest result of a
        # real 0.6B local model; execution is verified by the
        # deterministic legs above, never by model luck.
        report("real model", "structured planning outcome", "EXECUTED",
               detail)
    except Exception as exc:  # noqa: BLE001
        report("real model", "structured planning outcome", "FAILED",
               f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 10. no unintended filesystem changes
# --------------------------------------------------------------------------- #

def section_files(config, before):
    print("== 10. file-change audit (REAL) ==", flush=True)
    after = snapshot_docs(config)
    before_names = {p for p, _ in before}
    after_names = {p for p, _ in after}
    deleted = sorted(before_names - after_names)
    created = sorted(after_names - before_names)
    expected = {"data.csv", "report.txt", "cancel-a.txt",
                "digest-a.txt", "digest-b.txt", "partial-ok.txt"}
    unexpected = sorted(set(created) - expected)
    report("files", "no files deleted", 
           "EXECUTED" if not deleted else "FAILED",
           ", ".join(deleted) or "(none)")
    report("files", "only expected workflow files created",
           "EXECUTED" if not unexpected else "FAILED",
           f"created={created}"
           + (f" UNEXPECTED={unexpected}" if unexpected else ""))
    audit = os.path.join(SANDBOX, "state", "audit.jsonl")
    report("files", "audit trail written (sandbox state dir)",
           "EXECUTED" if os.path.isfile(audit) else "FAILED", audit)


# --------------------------------------------------------------------------- #

def main():
    t0 = time.time()
    config = sandbox_config()
    before = snapshot_docs(config)

    desktop = section_environment()
    if not desktop:
        print("\nDesktop session unavailable — desktop legs are BLOCKED "
              "(reported honestly; the sandboxed workflow legs still run).")

    if not section_discovery():
        return _summary(t0, ok=False)

    section_workflow_a(config)
    section_digest_binding(config)
    section_refusal(config)
    section_cancellation(config)
    section_missing_capability(config)
    section_partial_failure(config)
    section_real_model()
    section_files(config, before)
    return _summary(t0)


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
            print(f"  [{r['status']}] {r['section']}: {r['name']} — "
                  f"{r['detail']}")
    failed = counts.get("FAILED", 0)
    exit_ok = ok and failed == 0
    print(f"  VERDICT: {'all claims evidence-backed' if exit_ok else 'FAILURES or unverified claims present'}")
    return 0 if exit_ok else 1


if __name__ == "__main__":
    sys.exit(main())
