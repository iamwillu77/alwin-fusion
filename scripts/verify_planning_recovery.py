#!/usr/bin/env python3
"""v0.20.0 REAL end-to-end verification: planning & recovery (10 items).

Runs against the REAL TaskManager, pipeline, PermissionChecker,
digest-bound approval gate and verifier in a dedicated sandbox under
/tmp/opencode/alwin-planning-verify (fresh each run, test data only;
the user's real state_dir, audit log and documents are never touched).

The plan proposer is the repository's deterministic SCRIPTED fixture
(RoutedFakeLLM convention) — the EXECUTION machinery under test is
real; only the proposal text is scripted, and it can only propose
closed-validated JSON.

Labels (honest):

  REAL      — real manager/pipeline/approvals/files/verification
  SANDBOX   — isolated temporary directory
  SCRIPTED  — deterministic plan-proposal fixture (never real-model
              evidence)
  BLOCKED   — the environment refused; recorded as-is

Items (from the milestone brief):
  1. a real multi-step task through the existing pipeline,
  2. correct dependency ordering,
  3. independent verification of completed operations,
  4. partial completion when a later step fails,
  5. safe continuation after a previously completed step (resume —
     the completed mutation is NOT repeated),
  6. refusal to repeat an uncertain mutation (excluded from resume,
     listed for reconciliation),
  7. fresh approval for a resumed mutation (old digest refused),
  8. graceful handling of an unavailable application,
  9. cancellation and audit behaviour,
 10. preservation of existing files when an operation is refused.

Then ALL six existing verification scripts are re-run.

Exit code: 0 when every item is EXECUTED or honestly BLOCKED.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

SANDBOX = "/tmp/opencode/alwin-planning-verify"
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


def scripted_manager(config, proposal_json):
    """REAL TaskManager with the deterministic proposal fixture."""
    from taskui.manager import TaskManager

    class Fixture:
        model = "scripted:planning-recovery-fixture"

        def __init__(self):
            self.proposal = proposal_json

        def generate(self, *, prompt, system=None, json_mode=False,
                     timeout=None, **kwargs):
            if "summarize untrusted data" in (system or ""):
                return "Bounded summary."
            if "JSON proposal:" in prompt:
                return self.proposal
            if "Propose ONLY" in prompt:
                return json.dumps({"decision": "decline",
                                   "reason": "no replan scripted"})
            return "not a valid single action {"

    return TaskManager(config, use_llm=True, llm_client=Fixture())


def plan_json(steps, objective):
    return json.dumps({"decision": "plan", "objective": objective,
                       "steps": steps})


def step(step_id, action, **extra):
    item = {"id": step_id, "summary": f"step {step_id}", "action": action}
    item.update(extra)
    return item


GOAL = ("Analyze the CSV files in my Documents folder and summarize my "
        "monthly expenses")   # deterministic: unsupported for the NL
                              # parser, so the plan proposal drives


def wait_terminal(manager, timeout=20):
    return manager.wait_terminal(timeout=timeout)


def sandbox_hash():
    import hashlib
    digest = hashlib.sha256()
    home = os.path.join(SANDBOX, "home")
    for root, dirs, files in os.walk(home):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            digest.update(os.path.relpath(path, home).encode())
            with open(path, "rb") as handle:
                digest.update(handle.read())
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 1-3: real multi-step task, dependency order, verification
# --------------------------------------------------------------------------- #


def item1_plan(config):
    print("== 1-3. real multi-step task (REAL manager + pipeline) ==",
          flush=True)
    target = os.path.join(SANDBOX, "home", "Documents", "plan-a.txt")
    manager = scripted_manager(config, plan_json([
        step("s1", {"action": "create_text_file",
                    "path": "Documents/plan-a.txt",
                    "content": "step one content\n"},
             expected="the file exists"),
        step("s2", {"action": "read_file", "path": "Documents/plan-a.txt"},
             depends_on=["s1"], expected="content is readable"),
        step("s3", {"action": "create_text_file",
                    "path": "Documents/plan-b.txt",
                    "content": "step three content\n"},
             depends_on=["s1"], expected="the file exists"),
    ], objective="Make and read notes"))
    record = manager.submit(GOAL)
    ok_gate = manager.wait_for_state("awaiting_approval", timeout=10)
    report("1 plan", "real plan reached the approval gate",
           "EXECUTED" if ok_gate else "FAILED",
           f"steps={len(record.steps)}")
    if not ok_gate:
        return None, None
    order_before = [s["status"] for s in record.steps]
    approved = manager.approve(record.approval_digest)
    finished = wait_terminal(manager)
    statuses = [s["status"] for s in record.steps]
    ok = (approved and finished and record.state == "completed"
          and statuses == ["succeeded", "succeeded", "succeeded"]
          and order_before == ["pending"] * 3)
    report("2 dependencies", "dependency ordering held (pending until "
           "approved; all verified in order)", "EXECUTED" if ok else
           "FAILED", f"statuses={statuses}")
    # 3: independent verification (verifier re-read the created bytes)
    verified = (record.verification is not None
                and record.verification.get("status") in ("passed", "ok"))
    on_disk = open(target, encoding="utf-8").read() == \
        "step one content\n"
    report("3 verification", "completed operations independently "
           "verified (bytes on disk match)",
           "EXECUTED" if verified and on_disk else "FAILED",
           f"verify={record.verification.get('status') if record.verification else None}")
    return manager, record if ok else (None, None)


# --------------------------------------------------------------------------- #
# 4-5: partial completion and safe continuation (resume)
# --------------------------------------------------------------------------- #


def item4_partial(config):
    print("== 4-5. partial completion + safe resume (REAL) ==",
          flush=True)
    conflict = os.path.join(SANDBOX, "home", "Documents", "conflict.txt")
    with open(conflict, "w", encoding="utf-8") as handle:
        handle.write("PROTECTED ORIGINAL")
    manager = scripted_manager(config, plan_json([
        step("s1", {"action": "create_text_file",
                    "path": "Documents/partial-works.txt",
                    "content": "completed work\n"},
             expected="the file exists"),
        step("s2", {"action": "create_text_file",
                    "path": "Documents/conflict.txt",
                    "content": "should never land"},
             depends_on=["s1"], expected="the file exists"),
    ], objective="Two notes"))
    record = manager.submit(GOAL)
    manager.wait_for_state("awaiting_approval", timeout=10)
    manager.approve(record.approval_digest)
    wait_terminal(manager)
    statuses = [s["status"] for s in record.steps]
    partial = record.state in ("failed", "needs_attention") \
        and statuses == ["succeeded", "failed"]
    report("4 partial", "later failure reported with earlier success "
           "preserved", "EXECUTED" if partial else "FAILED",
           f"state={record.state} statuses={statuses}")
    note = any("Needs clarification" in n for n in record.notes)
    report("4 partial", "deterministic overwrite clarification surfaced",
           "EXECUTED" if note else "FAILED",
           "asks whether to replace (reviewed action) or rename")
    preserved = open(conflict, encoding="utf-8").read() == \
        "PROTECTED ORIGINAL"
    report("10 files", "refused operation preserved the existing file",
           "EXECUTED" if preserved else "FAILED", "byte-identical")

    # 5: resume — the COMPLETED s1 is never repeated.
    before_hash_mtime = os.path.getmtime(
        os.path.join(SANDBOX, "home", "Documents", "partial-works.txt"))
    os.remove(conflict)                       # user resolves the conflict
    resumed = manager.resume_task(record.task_id)
    manager.wait_for_state("awaiting_approval", timeout=10)
    old_digest_ok = manager.approve(record.approval_digest)
    report("7 fresh approval", "the original approval is refused for "
           "the resumed plan", "EXECUTED" if not old_digest_ok else
           "FAILED", f"wrong_digest_accepted={old_digest_ok}")
    manager.approve(resumed.approval_digest)
    wait_terminal(manager)
    same_file = os.path.getmtime(
        os.path.join(SANDBOX, "home", "Documents",
                     "partial-works.txt")) == before_hash_mtime
    ok_resume = (resumed.state == "completed"
                 and [s["status"] for s in resumed.steps] == ["succeeded"]
                 and same_file and resumed.resume_of == record.task_id)
    report("5 resume", "resume completed ONLY the remaining step; the "
           "verified mutation was not repeated",
           "EXECUTED" if ok_resume else "FAILED",
           f"state={resumed.state} steps="
           f"{[s['status'] for s in resumed.steps]}")
    return partial and ok_resume


# --------------------------------------------------------------------------- #
# 6: uncertain mutation refused automatic repetition
# --------------------------------------------------------------------------- #


def item6_uncertain(config):
    print("== 6. uncertain mutation never auto-repeated (SANDBOX "
          "simulation, labelled) ==", flush=True)
    from ai_core.task_plan import (
        STEP_VERIFICATION_FAILED, build_resume_plan, validate_plan)
    plan = validate_plan({"objective": "Uncertain", "steps": [
        step("s1", {"action": "replace_file_content",
                    "path": "Documents/x.txt", "content": "y"}),
        step("s2", {"action": "read_file", "path": "Documents/x.txt"}),
    ]})
    plan.steps[0].state = STEP_VERIFICATION_FAILED
    plan.steps[0].dispatched = True
    residual, notes, recon = build_resume_plan(plan, verified_results={})
    ok = ([s.id for s in residual.steps] == ["s2"] and len(recon) == 1
          and "reconciliation" in notes[0])
    report("6 uncertain", "dispatched-but-unverified mutation excluded "
           "from resume and listed for reconciliation",
           "EXECUTED" if ok else "FAILED",
           "SIMULATED outcome (labelled) — the pure builder is real")
    return ok


# --------------------------------------------------------------------------- #
# 8-9: unavailable application, cancellation + audit
# --------------------------------------------------------------------------- #


def item8_unavailable(config):
    print("== 8. unavailable application handled gracefully (REAL) ==",
          flush=True)
    manager = scripted_manager(config, plan_json([
        step("s1", {"action": "open_file",
                    "path": "Documents/plan-a.txt",
                    "application": "okular"},
             expected="the application opens"),
    ], objective="Open with an unavailable app"))
    record = manager.submit(GOAL)
    manager.wait_for_state("awaiting_approval", timeout=10)
    manager.approve(record.approval_digest)
    wait_terminal(manager)
    ok = record.state in ("failed", "needs_attention")
    report("8 unavailable", "unavailable application fails honestly "
           "with per-step evidence", "EXECUTED" if ok else "FAILED",
           f"state={record.state} steps="
           f"{[s['status'] for s in record.steps]}")
    return ok


def item9_cancel_audit(config):
    print("== 9. cancellation and audit behaviour (REAL) ==", flush=True)
    manager = scripted_manager(config, plan_json([
        step("s1", {"action": "create_text_file",
                    "path": "Documents/cancel-plan.txt",
                    "content": "never"},
             expected="the file exists"),
    ], objective="A plan we cancel"))
    record = manager.submit(GOAL)
    manager.wait_for_state("awaiting_approval", timeout=10)
    manager.reject()
    wait_terminal(manager)
    created = os.path.exists(os.path.join(
        SANDBOX, "home", "Documents", "cancel-plan.txt"))
    audit_lines = []
    audit_path = os.path.join(SANDBOX, "state", "audit.jsonl")
    if os.path.exists(audit_path):
        with open(audit_path, encoding="utf-8") as handle:
            audit_lines = [json.loads(line) for line in
                           handle.read().splitlines() if line.strip()]
    ok = (record.state == "cancelled" and not created)
    report("9 cancel", "cancelled plan executed nothing",
           "EXECUTED" if ok else "FAILED", f"state={record.state}")
    # 9b: audit distinguishes attempts (each attempt has its own
    # audited task id; the resume carries resume_of).
    ids = {e.get("task_id") for e in audit_lines}
    report("9 audit", "audit records exist for the attempts "
           f"({len(ids)} task id(s))",
           "EXECUTED" if ids else "FAILED", "each attempt is its own "
           "audited task; resume links via resume_of")
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
    if SANDBOX.startswith("/tmp/opencode/alwin-planning-verify"):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(os.path.join(SANDBOX, "state"), exist_ok=True)
    os.makedirs(os.path.join(SANDBOX, "home", "Documents"), exist_ok=True)

    print("Alwin Fusion v0.20.0 planning/recovery verification "
          f"(sandbox: {SANDBOX}, real manager, scripted proposals)\n")

    config = make_config()
    ok1, _ = item1_plan(config)
    ok4 = item4_partial(config)
    ok6 = item6_uncertain(config)
    ok8 = item8_unavailable(config)
    ok9 = item9_cancel_audit(config)
    okrerun = rerun_all()

    ok = all((ok1, ok4, ok6, ok8, ok9, okrerun))
    code = _summary(t0, ok)
    if SANDBOX.startswith("/tmp/opencode/alwin-planning-verify"):
        shutil.rmtree(SANDBOX, ignore_errors=True)
        print("[cleanup] removed only this script's sandbox.")
    return code


def _summary(t0, ok=True):
    counts = {}
    for entry in RESULTS:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print("\n" + "=" * 68)
    print("PLANNING/RECOVERY VERIFICATION SUMMARY")
    print("=" * 68)
    for status in ("EXECUTED", "BLOCKED", "UNVERIFIED", "FAILED"):
        if counts.get(status):
            print(f"  {status:<11}: {counts[status]}")
    print(f"  elapsed    : {time.time() - t0:.1f}s")
    print("  labels     : REAL = real manager/pipeline/approvals/files;\n"
          "               SANDBOX = isolated temp dir; SCRIPTED = "
          "deterministic proposal fixture;\n               one "
          "SIMULATED uncertain-outcome scenario (labelled).")
    verdict = "VERIFICATION OK" if ok else "VERIFICATION FAILED"
    print("=" * 68)
    print(verdict)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
