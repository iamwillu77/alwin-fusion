#!/usr/bin/env python3
"""v0.16.0 end-to-end verification: scheduler (15 legs).

Every storage leg runs against a REAL local SQLite store in a dedicated
temporary directory under /tmp/opencode/alwin-scheduler-verify (fresh
each run, test data only — the user's real state_dir, audit log, task
history and documents are never touched, and nothing is deleted outside
this script's own sandbox).  Time is driven by an injected fake clock:
no leg waits in real time.

Labels (honest, same convention as verify_memory.py):

  REAL      — real SQLite file, real migrations, real disk I/O, real
              PermissionChecker, real app registry, and (legs 9-10) the
              REAL TaskManager running the real permissioned pipeline
              with the real audit log
  MOCKED    — the task manager is a duck-typed fake in the lifecycle
              legs (the real-engine evidence lives in legs 9-10); the
              clock is always injected
  SCRIPTED  — the planning model is a deterministic fixture (the same
              convention as verify_desktop_workflows.py); it can only
              propose JSON the trusted validator re-checks
  BLOCKED   — the environment refused; reported as-is, never faked

Legs (from the milestone brief):
  1. create and persist a one-time reminder,
  2. restart the scheduler and confirm the job survives,
  3. trigger a due reminder through the controlled test clock,
  4. one occurrence produces exactly one notification,
  5. create and inspect a recurring reminder,
  6. pause and resume a job,
  7. cancel a job and prove it does not execute,
  8. simulate missed occurrences and verify bounded recovery,
  9. run an eligible scheduled task through the REAL permissioned
     pipeline (approval honoured),
 10. decline approval and prove the protected operation does not run,
 11. simulate a missing capability / unavailable engine and verify an
     honest failure,
 12. simulate a storage error and verify honest reporting,
 13. verify duplicate-occurrence prevention (including two scheduler
     instances racing on one store),
 14. verify shutdown and restart behaviour,
 15. run the existing memory, workflow and desktop-control scripts.

IMPORTANT (also stated in the docs): schedules run ONLY while an Alwin
application with a hosted manager is running on a powered-on machine.
There is no operating-system scheduler integration, and no leg claims
otherwise.

Exit code: 0 when every leg is EXECUTED or honestly BLOCKED; 1 when any
leg FAILED.
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

SANDBOX = "/tmp/opencode/alwin-scheduler-verify"
RESULTS = []


def report(section, name, status, detail=""):
    RESULTS.append({"section": section, "name": name,
                    "status": status, "detail": detail})
    print(f"[{status:>7}] {section}: {name}"
          + (f" — {detail}" if detail else ""), flush=True)


# --------------------------------------------------------------------------- #
# Fakes (MOCKED): clock and manager ducks; the store is always REAL SQLite.
# --------------------------------------------------------------------------- #


class FakeClock:
    def __init__(self, value):
        self.value = int(value)

    def __call__(self):
        return self.value


class FakeRecord:
    def __init__(self, task_id, state="planning", approval=""):
        self.task_id = task_id
        self.state = state
        self.approval = approval


class MockManager:
    """Duck-typed TaskManager (MOCKED) for the lifecycle legs."""

    def __init__(self):
        self.observers = []
        self.submitted = []
        self.cancelled = 0
        self._n = 0

    def add_observer(self, callback):
        self.observers.append(callback)

    @property
    def busy(self):
        return bool(self.submitted)

    def submit(self, command):
        self.submitted.append(command)
        self._n += 1
        return FakeRecord(f"task_{self._n}")

    def cancel(self):
        self.cancelled += 1
        return True


class ScriptedModel:
    """Deterministic planning fixture (SCRIPTED — same as workflows)."""

    def __init__(self, proposal):
        self.model = "scripted:scheduler-fixture"
        self.proposal = proposal

    def generate(self, *, prompt, system=None, json_mode=False,
                 timeout=None, **kwargs):
        if "summarize untrusted data" in (system or ""):
            return "Bounded summary of the referenced step output."
        if "JSON proposal:" in prompt:
            return self.proposal
        if "Propose ONLY" in prompt:
            return json.dumps({"decision": "decline",
                               "reason": "no replan scripted"})
        return "not a valid single action {"


def plan_json(steps, objective):
    return json.dumps({"decision": "plan", "objective": objective,
                       "steps": steps})


def step(step_id, action, **extra):
    item = {"id": step_id, "summary": f"step {step_id}", "action": action}
    item.update(extra)
    return item


def fresh_store(clock, wipe=False):
    from ai_core.jobstore import JobStore
    state = os.path.join(SANDBOX, "state")
    if wipe:
        # Isolation between legs: only ever removes this script's own
        # sandbox database files.
        for name in ("scheduler.db", "scheduler.db-journal",
                     "scheduler.db-wal", "scheduler.db-shm"):
            path = os.path.join(state, name)
            if os.path.exists(path):
                os.remove(path)
    return JobStore.open_for(state, now=clock)


def reminder_job(now, title="Review the report", kind="daily", **kw):
    from ai_core import schedule
    if kind == "weekly":
        sched = schedule.make_schedule("weekly", hour=9, minute=0,
                                       weekdays=(0,), tzid="UTC")
    elif kind == "once":
        sched = schedule.make_schedule("once", at_utc=now + 3600,
                                       now=now, tzid="UTC")
    else:
        sched = schedule.make_schedule("daily", hour=9, minute=30,
                                       tzid="UTC")
    return schedule.make_job(
        title, kw.pop("description", "Review the pending report."),
        job_type=kw.pop("job_type", "reminder"), schedule=sched,
        now=now, **kw)


# --------------------------------------------------------------------------- #
# 1-2. persistence and restart
# --------------------------------------------------------------------------- #

def leg1_create_persist(clock):
    print("== 1. one-time reminder persisted (REAL store) ==", flush=True)
    store = fresh_store(clock, wipe=True)
    from ai_core import schedule
    job = reminder_job(clock(), kind="once", title="One-off review")
    store.add(schedule.ScheduledJob(**{
        **job.__dict__,
        "next_occurrence": job.schedule.at_utc}))
    row = store.get(job.job_id)
    ok = row is not None and row.title == "One-off review"
    mode = oct(os.stat(store.path).st_mode & 0o777)
    report("1 persistence", "one-time reminder created and persisted",
           "EXECUTED" if ok else "FAILED",
           f"job_id={job.job_id} file={os.path.basename(store.path)} "
           f"mode={mode}")
    ok2 = mode == "0o600"
    report("1 persistence", "store file is owner-only", 
           "EXECUTED" if ok2 else "FAILED", f"mode={mode}")
    return store, job, ok and ok2


def leg2_restart(store, job, clock):
    print("== 2. restart survival (REAL store) ==", flush=True)
    store.close()
    reopened = fresh_store(clock)
    row = reopened.get(job.job_id)
    ok = row is not None and row.title == "One-off review" \
        and row.next_occurrence == job.schedule.at_utc
    report("2 restart", "job survives a scheduler restart", "EXECUTED"
           if ok else "FAILED", f"rows={len(reopened.list_jobs())}")
    return reopened, ok


# --------------------------------------------------------------------------- #
# 3-4. due reminder through the fake clock; one notification per occurrence
# --------------------------------------------------------------------------- #

def leg3_due_trigger(clock):
    print("== 3-4. fake-clock trigger + dedupe (MOCKED manager) ==",
          flush=True)
    store = fresh_store(clock, wipe=True)   # isolate from legs 1-2
    from ai_core.scheduler import Scheduler
    events = []
    scheduler = Scheduler(store, manager=MockManager(),
                          notifier=events.append, clock=clock)
    job = reminder_job(clock(), title="Daily review")
    job = scheduler.create(job)
    clock.value = job.next_occurrence + 10
    r1 = scheduler.tick()
    ok = r1.reminders == 1 and [e["kind"] for e in events] == ["reminder"]
    report("3 trigger", "due reminder fires via the test clock",
           "EXECUTED" if ok else "FAILED",
           f"reminders={r1.reminders} kinds={[e['kind'] for e in events]}")
    scheduler.tick()
    ok2 = len(events) == 1
    report("4 dedupe", "one occurrence produces one notification",
           "EXECUTED" if ok2 else "FAILED", f"notifications={len(events)}")
    row = store.occurrence(job.job_id, job.next_occurrence)
    ok3 = bool(row) and row["notified"] == 1 and row["dismissed"] == 0
    store.dismiss(job.job_id, job.next_occurrence)
    ok3 = ok3 and store.occurrence(
        job.job_id, job.next_occurrence)["dismissed"] == 1
    report("4 dedupe", "notification flag + dismissal persisted",
           "EXECUTED" if ok3 else "FAILED", "")
    scheduler.close()
    store.close()
    return ok and ok2 and ok3


# --------------------------------------------------------------------------- #
# 5-7. recurring inspection, pause/resume, cancel
# --------------------------------------------------------------------------- #

def leg5_recurring(clock):
    print("== 5. recurring reminder create + inspect (REAL) ==",
          flush=True)
    store = fresh_store(clock, wipe=True)   # legs 5-7 share this store
    from ai_core.scheduler import Scheduler
    scheduler = Scheduler(store, manager=MockManager(), clock=clock)
    job = scheduler.create(reminder_job(clock(), title="Weekly review",
                                        kind="weekly"))
    detail = scheduler.describe(job)
    listing = scheduler.listing()
    ok = "every Mon" in detail and "UTC" in detail \
        and job.next_occurrence > clock() and "Weekly review" in listing
    report("5 recurring", "recurring reminder created and inspectable",
           "EXECUTED" if ok else "FAILED", f"detail={detail[:70]}")
    return store, ok


def leg6_pause_resume(store, clock):
    print("== 6. pause and resume (REAL) ==", flush=True)
    from ai_core.scheduler import Scheduler
    scheduler = Scheduler(store, manager=MockManager(), clock=clock)
    job = [j for j in store.list_jobs() if j.title == "Weekly review"][0]
    scheduler.pause(job.job_id)
    paused = store.get(job.job_id)
    before = clock.value
    clock.value += 30 * 86400
    r = scheduler.tick()
    ok = paused.state == "paused" and paused.next_occurrence is None \
        and r.reminders == 0 and r.due == 0
    resumed = scheduler.resume(job.job_id)
    ok = ok and resumed.state == "enabled" \
        and resumed.next_occurrence > clock.value
    report("6 pause/resume", "paused job never fires; resume recomputes",
           "EXECUTED" if ok else "FAILED", f"skipped={(clock.value-before)//86400}d")
    return ok


def leg7_cancel(store, clock):
    print("== 7. cancelled job never executes (REAL+MOCKED) ==",
          flush=True)
    from ai_core.scheduler import Scheduler, SchedulerError
    manager = MockManager()
    scheduler = Scheduler(store, manager=manager, clock=clock)
    job = [j for j in store.list_jobs() if j.title == "Weekly review"][0]
    scheduler.cancel_job(job.job_id)
    clock.value += 60 * 86400
    r = scheduler.tick()
    refused = False
    try:
        scheduler.run_now(job.job_id)
    except SchedulerError:
        refused = True
    ok = store.get(job.job_id).state == "cancelled" \
        and r.reminders == 0 and refused and not manager.submitted
    report("7 cancel", "cancelled job never runs (tick + run-now refused)",
           "EXECUTED" if ok else "FAILED",
           f"state={store.get(job.job_id).state}")
    return ok


# --------------------------------------------------------------------------- #
# 8. missed occurrences: bounded coalescing, never a backlog
# --------------------------------------------------------------------------- #

def leg8_missed(clock):
    print("== 8. missed occurrences coalesce (REAL+MOCKED) ==",
          flush=True)
    from ai_core.scheduler import Scheduler
    store = fresh_store(clock, wipe=True)
    events = []
    scheduler = Scheduler(store, manager=MockManager(),
                          notifier=events.append, clock=clock)
    job = scheduler.create(reminder_job(clock(), title="Nightly note"))
    clock.value = job.next_occurrence + 30 * 86400 + 60
    r = scheduler.tick()
    missed = [e for e in events if e["kind"] == "missed"]
    history = store.history(job.job_id)
    updated = store.get(job.job_id)
    ok = r.missed == 1 and len(missed) == 1 \
        and history[0]["status"] == "missed" \
        and updated.next_occurrence > clock.value
    report("8 missed", "30 days away -> ONE coalesced notice, no backlog",
           "EXECUTED" if ok else "FAILED",
           f"missed={r.missed} notices={len(missed)} "
           f"next_in={(updated.next_occurrence-clock.value)//86400}d")
    r2 = scheduler.tick()
    ok2 = r2.due == 0 and r2.reminders == 0
    report("8 missed", "no replay on the following tick",
           "EXECUTED" if ok2 else "FAILED", f"due={r2.due}")
    scheduler.close()
    store.close()
    return ok and ok2


# --------------------------------------------------------------------------- #
# 9-10. REAL TaskManager + real permissioned pipeline (+ approval)
# --------------------------------------------------------------------------- #


def scheduled_task_goal():
    return "build the scheduled report file for the verification run"


def scripted_report_plan(path):
    return plan_json([
        step("s1", {"action": "create_report", "path": path,
                    "content": {"title": "Scheduled report",
                                "blocks": [{"type": "paragraph",
                                            "text": "Produced by a "
                                                    "scheduled task."}]}},
             expected="the report file exists"),
    ], "create the scheduled report file")


def drive_real_run(scheduler, manager, clock, *, approve):
    """Tick until the in-flight scheduled task reaches the gate; then
    approve or refuse, and drain the terminal state.  Returns the final
    job row.  Deterministic: the clock only moves if approval expires
    (never, here), and everything else is event-driven."""
    from ai_core import jobstore as js
    deadline = time.time() + 60
    decided = False
    job_id = list(scheduler.inflight)[0] if scheduler.inflight else ""
    while time.time() < deadline:
        scheduler.tick()
        record = manager.current
        if record is not None and record.state == "awaiting_approval" \
                and not decided:
            if approve:
                manager.approve(record.approval_digest)
            else:
                manager.reject()
            decided = True
        if decided and record is not None and record.is_terminal:
            scheduler.tick()          # drain the terminal record
            job = scheduler.store.get(job_id)
            if job and job.last_outcome:
                return job
        time.sleep(0.05)
    return None


def leg9_real_pipeline(config, clock):
    print("== 9. scheduled task through the REAL pipeline (REAL) ==",
          flush=True)
    from ai_core.scheduler import Scheduler
    from taskui.manager import TaskManager
    store = fresh_store(clock)
    report_path = os.path.join(SANDBOX, "home", "Documents",
                               "scheduled_report.txt")
    if os.path.exists(report_path):
        os.remove(report_path)
    model = ScriptedModel(scripted_report_plan("Documents/"
                                               "scheduled_report.txt"))
    manager = TaskManager(config, use_llm=True, llm_client=model)
    events = []
    scheduler = Scheduler(store, manager=manager,
                          notifier=events.append, clock=clock,
                          config=config)
    from ai_core import schedule
    job = scheduler.create(schedule.make_job(
        "Scheduled report", scheduled_task_goal(), job_type="task",
        schedule=schedule.make_schedule("once", at_utc=clock() + 60,
                                        now=clock(), tzid=""),
        now=clock()))
    clock.value = job.next_occurrence + 1
    scheduler.tick()
    final = drive_real_run(scheduler, manager, clock, approve=True)
    file_ok = os.path.exists(report_path)
    audit_ok = False
    audit_path = os.path.join(SANDBOX, "state", "audit.jsonl")
    if os.path.exists(audit_path):
        with open(audit_path, encoding="utf-8") as handle:
            audit_ok = "scheduled_report.txt" in handle.read()
    ok = final is not None and final.last_outcome == "task completed" \
        and file_ok and audit_ok
    report("9 real pipeline", "scheduled task ran via preview -> "
           "approval -> execute -> verify -> audit", "EXECUTED" if ok
           else "FAILED",
           f"outcome={final.last_outcome if final else 'none'} "
           f"file={file_ok} audit={audit_ok}")
    scheduler.close()
    store.close()
    return ok


def leg10_decline(config, clock):
    print("== 10. declined approval executes nothing (REAL) ==",
          flush=True)
    from ai_core import schedule
    from ai_core.scheduler import Scheduler
    from taskui.manager import TaskManager
    store = fresh_store(clock)
    refused_path = os.path.join(SANDBOX, "home", "Documents",
                                "refused_report.txt")
    if os.path.exists(refused_path):
        os.remove(refused_path)
    model = ScriptedModel(scripted_report_plan("Documents/"
                                               "refused_report.txt"))
    manager = TaskManager(config, use_llm=True, llm_client=model)
    scheduler = Scheduler(store, manager=manager, clock=clock,
                          config=config)
    job = scheduler.create(schedule.make_job(
        "Refused report", scheduled_task_goal(), job_type="task",
        schedule=schedule.make_schedule("once", at_utc=clock() + 60,
                                        now=clock(), tzid=""),
        now=clock()))
    clock.value = job.next_occurrence + 1
    scheduler.tick()
    final = drive_real_run(scheduler, manager, clock, approve=False)
    ok = final is not None and final.last_outcome == \
        "approval refused; nothing was executed" \
        and not os.path.exists(refused_path)
    report("10 refusal", "declined approval -> nothing executed, honest "
           "outcome", "EXECUTED" if ok else "FAILED",
           f"outcome={final.last_outcome if final else 'none'} "
           f"file_created={os.path.exists(refused_path)}")
    scheduler.close()
    store.close()
    return ok


# --------------------------------------------------------------------------- #
# 11-12. honest failures: capability/engine and storage
# --------------------------------------------------------------------------- #

def leg11_honest_capability(clock):
    print("== 11. missing capability -> honest failure (REAL "
          "registry + MOCKED engineless) ==", flush=True)
    from ai_core import schedule
    from ai_core.scheduler import Scheduler
    from ai_core.config import load_config
    config = load_config(home=os.path.join(SANDBOX, "home"),
                         roots=["Documents"],
                         state_dir=os.path.join(SANDBOX, "state"))
    store = fresh_store(clock, wipe=True)
    scheduler = Scheduler(store, manager=MockManager(), clock=clock,
                          config=config)
    job = scheduler.create(schedule.make_job(
        "App probe", "", job_type="check",
        schedule=schedule.make_schedule("once", at_utc=clock() + 30,
                                        now=clock(), tzid=""),
        now=clock(),
        condition=schedule.make_condition("app_installed",
                                          "definitely-not-an-app")))
    clock.value = job.next_occurrence + 1
    scheduler.tick()
    row = store.get(job.job_id)
    ok1 = "unknown application id" in row.last_outcome
    report("11 capability", "unknown application id reported honestly "
           "(REAL registry probe)", "EXECUTED" if ok1 else "FAILED",
           f"outcome={row.last_outcome}")
    scheduler2 = Scheduler(store, manager=None, clock=clock)
    job2 = scheduler2.create(schedule.make_job(
        "Engineless task", "Do the thing.", job_type="task",
        schedule=schedule.make_schedule("once", at_utc=clock() + 60,
                                        now=clock(), tzid=""),
        now=clock()))
    clock.value = job2.next_occurrence + 1
    r = scheduler2.tick()
    row2 = store.get(job2.job_id)
    ok2 = row2.approval == "needs_attention" and \
        "no execution engine" in (row2.last_outcome or "") + r.errors[0]
    report("11 capability", "unavailable engine -> honest "
           "needs-attention outcome (MOCKED)",
           "EXECUTED" if ok2 else "FAILED", f"outcome={row2.last_outcome}")
    scheduler.close()
    scheduler2.close()
    store.close()
    return ok1 and ok2


def leg12_storage_error(clock):
    print("== 12. storage error reported honestly (REAL) ==", flush=True)
    from ai_core.jobstore import JobStoreError
    from ai_core.scheduler import Scheduler
    store = fresh_store(clock, wipe=True)
    scheduler = Scheduler(store, manager=MockManager(), clock=clock)
    store.inject_failure()
    r = scheduler.tick()
    ok = bool(r.errors) and "unavailable" in r.errors[0].lower()
    report("12 storage", "storage failure surfaces as an honest error "
           "(never a silent skip)", "EXECUTED" if ok else "FAILED",
           f"errors={r.errors[:1]}")
    scheduler.close()
    return ok


# --------------------------------------------------------------------------- #
# 13-14. duplicate prevention and shutdown/restart
# --------------------------------------------------------------------------- #

def leg13_duplicates(clock):
    print("== 13. duplicate-occurrence prevention (REAL) ==", flush=True)
    from ai_core.scheduler import Scheduler
    store = fresh_store(clock, wipe=True)
    sched_a0 = Scheduler(store, manager=MockManager(), clock=clock)
    job = sched_a0.create(reminder_job(clock(), title="Dup probe"))
    first = store.claim(job.job_id, job.next_occurrence)
    second = store.claim(job.job_id, job.next_occurrence)
    ok = first is True and second is False
    report("13 duplicates", "same occurrence claimed exactly once",
           "EXECUTED" if ok else "FAILED",
           f"claim1={first} claim2={second}")
    sched_a0.close()
    store.close()
    # Two scheduler instances racing on one store: exactly one dispatch.
    store = fresh_store(clock, wipe=True)
    events_a, events_b = [], []
    manager_a, manager_b = MockManager(), MockManager()
    sched_a = Scheduler(store, manager=manager_a,
                        notifier=events_a.append, clock=clock)
    job = sched_a.create(reminder_job(clock(), title="Race probe"))
    sched_b = Scheduler(store, manager=manager_b,
                        notifier=events_b.append, clock=clock)
    clock.value = job.next_occurrence + 1
    sched_a.tick()
    sched_b.tick()
    total = len(events_a) + len(events_b)
    ok2 = total == 1
    report("13 duplicates", "two schedulers, one occurrence -> one "
           "dispatch", "EXECUTED" if ok2 else "FAILED",
           f"dispatches={total}")
    sched_a.close()
    sched_b.close()
    store.close()
    return ok and ok2


def leg14_shutdown(clock):
    print("== 14. shutdown and restart (REAL) ==", flush=True)
    from ai_core.jobstore import JobStore
    from ai_core.scheduler import Scheduler
    store = fresh_store(clock, wipe=True)
    scheduler = Scheduler(store, manager=MockManager(), clock=clock)
    scheduler.create(reminder_job(clock(), title="Shutdown probe"))
    scheduler.close()
    closed = scheduler.tick()
    store.close()
    ok = bool(closed.errors)
    report("14 shutdown", "closed scheduler ticks honestly (no work)",
           "EXECUTED" if ok else "FAILED", f"errors={closed.errors[:1]}")
    reopened = JobStore.open_for(os.path.join(SANDBOX, "state"),
                                 now=clock)
    ok2 = any(j.title == "Shutdown probe"
              for j in reopened.list_jobs())
    report("14 shutdown", "state intact after reopen (restart clean)",
           "EXECUTED" if ok2 else "FAILED",
           f"jobs={len(reopened.list_jobs())}")
    reopened.close()
    return ok and ok2


# --------------------------------------------------------------------------- #
# 15. run the existing verification scripts
# --------------------------------------------------------------------------- #

def leg15_existing_scripts():
    print("== 15. existing verification scripts (SCRIPTED) ==", flush=True)
    all_ok = True
    for script in ("verify_memory.py", "verify_desktop_workflows.py",
                   "verify_desktop_control.py"):
        path = os.path.join(REPO, "scripts", script)
        if not os.path.exists(path):
            report("15 existing", script, "BLOCKED", "script not found")
            continue
        proc = subprocess.run(
            [sys.executable, path], cwd=REPO, capture_output=True,
            text=True, timeout=1200)
        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        ok = proc.returncode == 0
        all_ok = all_ok and ok
        report("15 existing", script,
               "EXECUTED" if ok else "FAILED",
               f"exit={proc.returncode} · {tail[0][:80]}")
    return all_ok


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def main():
    t0 = time.time()
    if SANDBOX.startswith("/tmp/opencode/alwin-scheduler-verify"):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(os.path.join(SANDBOX, "state"), exist_ok=True)
    os.makedirs(os.path.join(SANDBOX, "home", "Documents"), exist_ok=True)

    from ai_core.config import load_config
    config = load_config(home=os.path.join(SANDBOX, "home"),
                         roots=["Documents"],
                         state_dir=os.path.join(SANDBOX, "state"))

    print("Alwin Fusion v0.16.0 scheduler verification "
          f"(sandbox: {SANDBOX}, test data only, fake clock)\n")

    clock = FakeClock(int(time.time()))
    store, job, ok1 = leg1_create_persist(clock)
    store, ok2 = leg2_restart(store, job, clock)
    store.close()
    ok3 = leg3_due_trigger(clock)
    store, ok5 = leg5_recurring(clock)
    ok6 = leg6_pause_resume(store, clock)
    ok7 = leg7_cancel(store, clock)
    store.close()
    ok8 = leg8_missed(clock)
    ok9 = leg9_real_pipeline(config, clock)
    ok10 = leg10_decline(config, clock)
    ok11 = leg11_honest_capability(clock)
    ok12 = leg12_storage_error(clock)
    ok13 = leg13_duplicates(clock)
    ok14 = leg14_shutdown(clock)
    ok15 = leg15_existing_scripts()

    ok = all((ok1, ok2, ok3, ok5, ok6, ok7, ok8, ok9, ok10, ok11,
              ok12, ok13, ok14, ok15))
    return _summary(t0, ok)


def _summary(t0, ok=True):
    counts = {}
    for entry in RESULTS:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print("\n" + "=" * 68)
    print("SCHEDULER VERIFICATION SUMMARY")
    print("=" * 68)
    for status in ("EXECUTED", "BLOCKED", "FAILED"):
        if counts.get(status):
            print(f"  {status:<9}: {counts[status]}")
    print(f"  elapsed   : {time.time() - t0:.1f}s")
    print("  labels    : REAL = real SQLite/PermissionChecker/registry/"
          "TaskManager+pipeline+audit;\n              MOCKED = fake "
          "manager duck; SCRIPTED = deterministic planner fixture;\n"
          "              the clock is injected in every leg.")
    print("  reminder  : schedules run ONLY while an Alwin app with a "
          "hosted manager is running.\n              No OS-scheduler "
          "integration exists or is claimed.")
    verdict = "VERIFICATION OK" if ok else "VERIFICATION FAILED"
    print("=" * 68)
    print(verdict)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
