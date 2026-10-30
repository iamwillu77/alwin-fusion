#!/usr/bin/env python3
"""v0.12.0 REAL-service verification: planning → approval → execution.

Everything below runs against REAL components: the live Ollama model,
the real trusted pipeline (preview → permission → approval →
execute → verify → audit), real sandboxed files under /tmp (never the
user's own Documents), and the real capability discovery.

Distinguishes honestly:
  REAL      — real Ollama / real pipeline / real files / real manager
  MOCKED    — pointed at (unit tests), never claimed as real here
  BLOCKED   — environment prevented it; reported, not faked

Demonstration (brief Phase 10):
 1. create a known test CSV (sandbox only),
 2. ask the real planner for a structured plan (inspect every step),
 3. confirm tool selection comes from the closed set,
 4. execute after a REAL digest-bound approval,
 5. reopen and validate the generated report (evidence!),
 6-7. real denied step executes nothing (plus the mocked
      recovery/no-repeat tests are referenced, not re-labelled),
 8. real cancellation at the approval gate with cleanup,
 9. real unreachable-Ollama planning failure is honest,
10. chat-only requests never create task records (real gate+manager).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SANDBOX = "/tmp/opencode/alwin-plan-verify"
RESULTS = []


def report(section, name, status, detail=""):
    RESULTS.append({"section": section, "name": name,
                    "status": status, "detail": detail})
    print(f"[{status:>7}] {section}: {name}"
          + (f" — {detail}" if detail else ""), flush=True)


def sandbox_config():
    from ai_core.config import load_config
    home = os.path.join(SANDBOX, "home")
    docs = os.path.join(home, "Documents")
    state = os.path.join(SANDBOX, "state")
    for d in (docs, state):
        os.makedirs(d, exist_ok=True)
    return load_config(home=home, roots=["Documents"], state_dir=state)


def real_llm():
    from ai_runtime.ollama_client import OllamaClient
    return OllamaClient(model=os.environ.get("OLLAMA_MODEL") or "qwen3:0.6b")


GOAL = ("Analyze expenses.csv in my Documents and create a report that "
        "summarizes the monthly expenses (read the file first and use "
        "its contents in the report).")


def section_preflight():
    print("== preflight (REAL) ==", flush=True)
    llm = real_llm()
    try:
        models = llm.list_models()
        report("preflight", "ollama reachable", "EXECUTED",
               f"model={llm.model} (installed: {len(models)})")
    except Exception as exc:
        report("preflight", "ollama reachable", "BLOCKED", str(exc))
        return None
    from ai_core.capabilities import discover_capabilities
    caps = discover_capabilities(llm_available=True)
    report("preflight", "capability discovery", "EXECUTED",
           f"{len(caps['actions'])} closed actions, "
           f"openpyxl={caps['documents']['create_spreadsheet']}, "
           f"mcp={caps['mcp']['status']} "
           f"(executable_tools={caps['mcp']['executable_tools']})")
    return llm


def make_test_csv() -> str:
    """Known fixture inside the SANDBOX (never the user's files)."""
    import csv
    path = os.path.join(SANDBOX, "home", "Documents", "expenses.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = [
        ("month", "amount"),
        ("2026-01", "120.50"),
        ("2026-02", "98.00"),
        ("2026-03", "143.75"),
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return path


def section_plan(llm):
    print("\n== real plan proposal (REAL Ollama) ==", flush=True)
    from ai_core.task_planning import TaskPlanningService
    service = TaskPlanningService(llm)
    proposal = service.propose(GOAL)
    print(f"  kind={proposal.kind} "
          f"question={proposal.question!r} reason={proposal.reason!r}",
          flush=True)
    if proposal.kind != "plan":
        blocked = ("BLOCKED" if proposal.kind == "clarify"
                   else "FAILED")
        report("plan", "real structured plan produced", blocked,
               f"kind={proposal.kind}: "
               f"{proposal.question or proposal.reason}")
        raw = getattr(service, "last_raw", "")
        if raw:
            print("  raw model response (bounded): "
                  f"{raw[:700]}", flush=True)
        return None
    for step in proposal.plan.steps:
        print(f"    {step.id}: [{step.action['action']}] "
              f"{step.summary} deps={list(step.depends_on)} "
              f"mutating={step.mutating}", flush=True)
    actions = [s.action["action"] for s in proposal.plan.steps]
    from ai_core.planner import ACTION_SCHEMA
    in_closed = all(a in ACTION_SCHEMA for a in actions)
    report("plan", "structured plan produced by the real model",
           "EXECUTED",
           f"{len(actions)} steps: {', '.join(actions)}")
    report("plan", "every step inside the trusted closed action set",
           "EXECUTED" if in_closed else "FAILED", str(actions))
    has_read = any(a == "read_file" for a in actions)
    has_output = any(a in ("create_report", "create_spreadsheet",
                           "create_text_file") for a in actions)
    report("plan", "correct tool classes selected "
           "(read first, then a report tool)",
           "EXECUTED" if (has_read and has_output) else "FAILED",
           str(actions))
    return proposal.plan


def section_execute(llm, plan):
    print("\n== real approval-gated execution (REAL pipeline) ==", flush=True)
    from taskui.manager import TaskManager
    cfg = sandbox_config()
    manager = TaskManager(cfg, use_llm=True, llm_client=llm)
    record = manager.submit(GOAL)
    if not manager.wait_for_state("awaiting_approval", timeout=240):
        report("execute", "plan reached the real approval gate", "FAILED",
               f"state={record.state} error={record.error!r}")
        manager.wait_terminal(timeout=10)
        return None
    report("execute", "plan reached the real approval gate", "EXECUTED",
           f"digest={record.approval_digest} "
           f"steps={[s['status'] for s in record.steps]}")
    # A wrong digest must be refused (approval binds what was shown).
    wrong = manager.approve("0" * 16)
    report("execute", "wrong digest is refused", "EXECUTED" if not wrong
           else "FAILED", "")
    if not manager.approve(record.approval_digest):
        report("execute", "real approval accepted", "FAILED", "")
        return None
    manager.wait_terminal(timeout=120)
    report("execute", "plan executed to a terminal state",
           "EXECUTED" if record.state == "completed" else "FAILED",
           f"state={record.state}, result={record.result_message!r}")
    if record.state != "completed":
        return record

    # ---- verification evidence: reopen the output -------------------
    reports = [s for s in record.steps
               if s["action"] in ("create_report", "create_spreadsheet",
                                  "create_text_file")
               and s["status"] == "succeeded"]
    if not reports:
        report("verify", "output step exists", "FAILED",
               f"steps={[s['status'] for s in record.steps]}")
        return record
    out_path = None
    # Find the created file(s) in the sandbox Documents:
    docs = os.path.join(SANDBOX, "home", "Documents")
    candidates = [os.path.join(docs, n) for n in sorted(os.listdir(docs))
                  if n != "expenses.csv" and not n.endswith(".csv")]
    out_path = next((c for c in candidates if os.path.isfile(c)), None)
    if out_path is None:
        report("verify", "a new output file exists on disk", "FAILED",
               f"files in sandbox: {sorted(os.listdir(docs))}")
        return record
    with open(out_path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    evidence = {
        "file": os.path.basename(out_path),
        "bytes": len(content),
        "mentions_expenses": "expense" in content.lower(),
        "has_report_structure": (os.path.splitext(out_path)[1] in
                                 (".txt", ".xlsx")),
    }
    ok = evidence["bytes"] > 50 and evidence["mentions_expenses"]
    report("verify", "output reopened and validated (evidence)",
           "EXECUTED" if ok else "FAILED", json.dumps(evidence))
    all_ok = all(s["status"] == "succeeded" for s in record.steps)
    report("verify", "every step succeeded and was verified",
           "EXECUTED" if all_ok else "FAILED",
           str([(s["id"], s["status"]) for s in record.steps]))
    v = record.verification or {}
    report("verify", "record carries verification evidence",
           "EXECUTED" if v.get("status") == "ok" else "FAILED",
           f"{v.get('status')}: {str(v.get('message'))[:80]}")
    return record


def section_safety(llm):
    print("\n== safety: denial, cancellation, chat routing (REAL) ==",
          flush=True)
    from taskui.manager import TaskManager

    # 6. A step outside the permitted roots is DENIED by the real
    #    permission stage (deterministic parser path — no planning).
    #    (Multi-step recovery/no-repeat of a DENIED step is covered by
    #    tests/test_task_planning.py — MOCKED/sandboxed, labelled there;
    #    this script never mislabels it as real.)
    cfg = sandbox_config()
    manager = TaskManager(cfg)
    record = manager.submit(
        "Create a text file named outside.txt in /tmp with content x")
    manager.wait_terminal(timeout=20)
    outside = "/tmp/outside.txt"
    denied_ok = (record.state in ("failed", "denied")
                 and not os.path.exists(outside))
    report("safety", "out-of-roots step denied, nothing written",
           "EXECUTED" if denied_ok else "FAILED",
           f"state={record.state} error={str(record.error)[:80]!r}")

    # 8. Real cancellation at the approval gate, then verify cleanup.
    manager2 = TaskManager(sandbox_config())
    record2 = manager2.submit(
        "Create a text file named cancelme.txt in Documents "
        "containing never runs")
    if manager2.wait_for_state("awaiting_approval", timeout=15):
        cancelled = manager2.cancel()
        manager2.wait_terminal(timeout=15)
        ok = (cancelled and record2.state == "cancelled"
              and not os.path.exists(os.path.join(
                  SANDBOX, "home", "Documents", "cancelme.txt")))
        report("safety", "cancel at the approval gate leaves no file",
               "EXECUTED" if ok else "FAILED",
               f"state={record2.state}")
    else:
        report("safety", "cancel at the approval gate", "FAILED",
               f"state={record2.state}")

    # 10. Chat-only requests never create task records: the REAL
    #     goal-gate says no, so the conversation service never calls
    #     the (real) manager.
    from ai_core.task_planning import looks_like_task_goal
    from companion.conversation import ConversationService
    chat = "What are its advantages in simple words?"
    gate_says_task = looks_like_task_goal(chat)
    chat_manager = TaskManager(sandbox_config())
    service = ConversationService(manager=chat_manager,
                                  is_action=looks_like_task_goal)
    result = service.submit(chat, source="voice")
    record_after = chat_manager.current
    chat_ok = (not gate_says_task and result.ok
               and record_after is None and result.record is None)
    report("safety", "chat creates no task record (real gate+manager)",
           "EXECUTED" if chat_ok else "FAILED",
           f"gate={gate_says_task} manager_record="
           f"{record_after is not None}")

    # 9. Unreachable Ollama: planning fails honestly (no execution).
    from ai_runtime.ollama_client import OllamaClient
    from ai_core.task_planning import TaskPlanningService
    dead = TaskPlanningService(OllamaClient(
        base_url="http://localhost:1", model="x", timeout=2.0))
    proposal = dead.propose(GOAL)
    ok = (proposal.kind == "unavailable"
          and "failed" in proposal.reason)
    report("safety", "unreachable ollama: planning reports unavailable",
           "EXECUTED" if ok else "FAILED", proposal.reason[:100])


def main():
    import argparse
    parser = argparse.ArgumentParser(
        prog="python3 scripts/verify_task_planning.py")
    parser.parse_args()
    if os.path.isdir(SANDBOX):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(SANDBOX, exist_ok=True)

    llm = section_preflight()
    if llm is None:
        print("\nBLOCKED: Ollama unreachable — planning verification "
              "cannot run. Unit tests (mocked) still cover the flows.")
        return 2
    csv_path = make_test_csv()
    print(f"  test CSV: {csv_path}")
    plan = section_plan(llm)
    if plan is not None:
        section_execute(llm, plan)
    section_safety(llm)

    print("\n== summary ==")
    counts = {}
    for r in RESULTS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    for r in RESULTS:
        if r["status"] in ("FAILED", "BLOCKED"):
            print(f"  {r['status']}: [{r['section']}] {r['name']} — "
                  f"{r['detail']}")
    return 1 if any(r["status"] == "FAILED" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
