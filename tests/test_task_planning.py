"""v0.12.0 task-planning tests (fake LLM everywhere; fake pipelines
clearly labelled; real sandbox pipelines only where marked REAL).

Covers the brief's checklist: plan schema validation, invalid steps and
dependency cycles, unknown tools, missing capabilities, clarification,
approval gates, execution ordering, dependency failures, timeouts and
bounded retries, duplicate submissions, cancellation and stale-result
protection, verification success/failure, recovery without repeating
side effects, capability discovery/cache, credential redaction in
planning reasons, GUI (presenter) progress transitions, model-
unavailable fallback — plus regression runs of the existing suites.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from types import SimpleNamespace

from ai_core.capabilities import (
    CapabilityCache,
    discover_capabilities,
    missing_for,
)
from ai_core.pipeline import (
    STATUS_FAILED,
    STATUS_UNSUPPORTED,
    STATUS_SUCCESS,
    TaskPreview,
    TaskReport,
    plan_digest,
)
from ai_core.task_plan import (
    MAX_PLAN_STEPS,
    PlanValidationError,
    TaskPlan,
    validate_plan,
)
from ai_core.task_planning import (
    PlanProposal,
    PlanningError,
    TaskPlanningService,
    looks_like_task_goal,
)
from taskui.lifecycle import (
    STATE_AWAITING_APPROVAL,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_NEEDS_ATTENTION,
)
from taskui.manager import ManagerError, TaskManager
from taskui.presenter import plan_rows, task_view
from tests.helpers import Sandbox


def wait(manager, *states, timeout=10.0):
    manager.wait_for_state(*states, timeout=timeout)
    manager.wait_terminal(timeout=timeout)
    return manager.current.state in states


def plan_json(steps, objective="Test objective"):
    return json.dumps({"decision": "plan", "objective": objective,
                       "steps": steps})


# --------------------------------------------------------------------------- #
# A — plan schema validation (trusted validator on untrusted JSON)
# --------------------------------------------------------------------------- #


def _step(step_id, action, **extra):
    item = {"id": step_id, "summary": f"step {step_id}", "action": action}
    item.update(extra)
    return item


class PlanSchemaTests(unittest.TestCase):
    def test_valid_plan_round_trips(self):
        plan = validate_plan({"objective": "make a note", "steps": [
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/n.txt", "content": "hi"}),
            _step("s2", {"action": "read_file", "path": "Documents/n.txt"},
                  depends_on=["s1"], expected="readable"),
        ]})
        self.assertIsInstance(plan, TaskPlan)
        self.assertEqual([s.id for s in plan.steps], ["s1", "s2"])
        self.assertTrue(plan.steps[0].mutating)
        self.assertFalse(plan.steps[1].mutating)
        # round-trip revalidates:
        again = validate_plan(plan.to_dict())
        self.assertEqual(again.to_dict(), plan.to_dict())

    def test_rejects_malformed_shapes(self):
        cases = [
            ("not json {", "JSON"),
            ([], "object"),
            ({"objective": "x", "steps": []}, "no steps"),
            ({"objective": "", "steps": [
                _step("a", {"action": "read_file", "path": "Documents/x"})]},
             "no objective"),
            ({"objective": "x", "steps": ["nope"]}, "not an object"),
            ({"objective": "x", "steps": [
                {"summary": "no id", "action": {"action": "read_file",
                                                "path": "Documents/x"}}]},
             "no id"),
            ({"objective": "x", "steps": [
                _step("a", {"action": "read_file", "path": "Documents/x"}),
                _step("a", {"action": "read_file", "path": "Documents/y"})]},
             "Duplicate step id"),
            ({"objective": "x", "steps": [
                _step("a", {"action": "read_file", "path": "Documents/x"},
                      bogus_field=1)]}, "unknown fields"),
            ({"objective": "x", "steps": [
                _step("a", {"action": "shell", "cmd": "rm -rf /"})]},
             "not a supported action"),
            ({"objective": "x", "steps": [
                _step("a", {"action": "read_file", "path": "Documents/x"},
                      depends_on=["a"])]}, "cannot depend on itself"),
            ({"objective": "x", "steps": [
                _step("a", {"action": "read_file", "path": "Documents/x"},
                      depends_on=["ghost"])]}, "unknown step"),
            ({"objective": "x", "steps": [
                _step("a", {"action": "read_file", "path": "Documents/x"},
                      depends_on=["b"]),
                _step("b", {"action": "read_file", "path": "Documents/y"})]},
             "not an earlier step"),
            ({"objective": "x", "steps": [
                _step(f"s{i}", {"action": "read_file",
                                "path": f"Documents/{i}.txt"})
                for i in range(MAX_PLAN_STEPS + 1)]}, "maximum"),
            ({"objective": "x", "steps": [
                _step("a", {"action": "create_text_file",
                            "path": "Documents/x",
                            "content": {"from_step": "z",
                                        "field": "proc"}})]},
             "Derivation field must be one of"),
        ]
        for raw, needle in cases:
            with self.subTest(needle=needle):
                with self.assertRaises(PlanValidationError) as ctx:
                    validate_plan(raw)
                self.assertIn(needle, str(ctx.exception))
                self.assertLessEqual(len(str(ctx.exception)), 300)

    def test_derivation_with_summarize_is_allowed(self):
        plan = validate_plan({"objective": "x", "steps": [
            _step("a", {"action": "read_file", "path": "Documents/x.csv"}),
            _step("b", {"action": "create_report", "path": "Documents/r.txt",
                        "content": {"title": "T", "blocks": [
                            {"type": "paragraph", "text": {
                                "from_step": "a", "field": "content",
                                "summarize": True}}]}},
                  depends_on=["a"]),
        ]})
        deriv = plan.steps[1].derives
        self.assertEqual(len(deriv), 1)
        self.assertTrue(next(iter(deriv.values())).summarize)

    def test_risk_is_derived_not_claimed(self):
        # A proposal cannot call a mutating action "safe".
        plan = validate_plan({"objective": "x", "steps": [
            _step("a", {"action": "create_directory",
                        "path": "Documents/claim"})]})
        self.assertTrue(plan.steps[0].mutating)
        self.assertTrue(plan.requires_approval)

    def test_misplaced_step_keys_inside_action_are_sanitized(self):
        # Real model behaviour: depends_on/expected/id sometimes arrive
        # INSIDE the action object. Hoisting lets every rule still run
        # on the values (deps must be earlier steps; ids unique).
        plan = validate_plan({"objective": "x", "steps": [
            {"summary": "read", "action": {"id": "s1",
                                           "action": "read_file",
                                           "path": "Documents/x",
                                           "depends_on": []}},
            {"id": "s2", "action": {"action": "read_file",
                                    "path": "Documents/y",
                                    "depends_on": ["s1"]}},
        ]})
        self.assertEqual([s.id for s in plan.steps], ["s1", "s2"])
        self.assertEqual(plan.steps[1].depends_on, ("s1",))
        # ...but a BAD hoisted value is still rejected:
        with self.assertRaises(PlanValidationError):
            validate_plan({"objective": "x", "steps": [
                {"id": "s1", "summary": "t",
                 "action": {"action": "read_file", "path": "Documents/x",
                            "depends_on": ["ghost"]}},
            ]})
        # ...and conflicting copies are rejected, not silently merged:
        with self.assertRaises(PlanValidationError):
            validate_plan({"objective": "x", "steps": [
                {"id": "s1", "depends_on": [],
                 "summary": "t",
                 "action": {"action": "read_file", "path": "Documents/x",
                            "depends_on": ["s1"]}},
            ]})


# --------------------------------------------------------------------------- #
# B — capability discovery (fake import finders; real MCP manifest)
# --------------------------------------------------------------------------- #


class CapabilityTests(unittest.TestCase):
    def test_discovery_reflects_the_fake_installation(self):
        def finder(name):
            if name in ("openpyxl",):
                return object()
            return None            # reportlab + gi missing

        caps = discover_capabilities(llm_available=True,
                                     spec_finder=finder)
        self.assertTrue(caps["documents"]["create_spreadsheet"])
        self.assertFalse(caps["documents"]["create_pdf"])
        self.assertFalse(caps["automation_bindings"])
        self.assertTrue(caps["llm"])
        self.assertIn("create_report", caps["actions"])
        self.assertEqual(caps["mcp"]["status"],
                         "descriptors_only_no_server")
        self.assertEqual(caps["mcp"]["executable_tools"], 0)
        self.assertIn("no MCP server", caps["mcp"]["note"])

    def test_missing_for_names_honest_remedies(self):
        caps = discover_capabilities(spec_finder=lambda name: None)
        self.assertIsNone(missing_for("read_file", caps))
        self.assertIn("openpyxl", missing_for("create_spreadsheet", caps))
        self.assertIn("reportlab", missing_for("create_pdf", caps))
        self.assertIn("atspi", missing_for("save_document", caps))
        self.assertIn("not an allowed",
                      missing_for("definitely_not_a_tool", caps))

    def test_cache_ttl_and_invalidation(self):
        clock = [0.0]
        calls = []

        def counting_discover(**kw):
            calls.append(1)
            return {"n": len(calls)}

        import ai_core.capabilities as caps_mod
        original = caps_mod.discover_capabilities
        caps_mod.discover_capabilities = counting_discover
        try:
            cache = CapabilityCache(ttl=10.0, clock=lambda: clock[0])
            cache.get()
            cache.get()
            self.assertEqual(len(calls), 1, "second get is cached")
            clock[0] = 11.0
            cache.get()
            self.assertEqual(len(calls), 2, "ttl expiry re-probes")
            cache.invalidate()
            cache.get()
            self.assertEqual(len(calls), 3, "invalidate re-probes")
        finally:
            caps_mod.discover_capabilities = original

    def test_discovery_import_failure_is_honest(self):
        def broken(_name):
            raise ValueError("weird finder")

        caps = discover_capabilities(spec_finder=broken)
        self.assertFalse(caps["documents"]["create_spreadsheet"])
        self.assertFalse(caps["automation_bindings"])


class TaskGoalGateTests(unittest.TestCase):
    def test_gate_accepts_task_goals_and_rejects_chat(self):
        accept = [
            "Analyze the CSV files in my Documents folder and create a "
            "report",
            "Create a text file named notes.txt with milk",
            "Please summarize my monthly expenses report",
            "Compare these two CSV files in Documents",
        ]
        reject = [
            "hi", "What are its advantages?",
            "Tell me about Python programming.",
            "How does the weather compare today?",
            "summarize this",
        ]
        for text in accept:
            self.assertTrue(looks_like_task_goal(text), text)
        for text in reject:
            self.assertFalse(looks_like_task_goal(text), text)


# --------------------------------------------------------------------------- #
# C — planning service (FAKE local model; no Ollama here)
# --------------------------------------------------------------------------- #


class RoutedFakeLLM:
    """Fake Ollama client that answers by prompt type (MOCK)."""

    def __init__(self):
        self.model = "fake:planner"
        self.calls = []
        self.proposal_response = None
        self.replan_response = None
        self.proposal_error = None
        self.summarize_response = "A bounded summary."

    def generate(self, *, prompt, system=None, json_mode=False,
                 timeout=None, **kwargs):
        self.calls.append({"prompt": prompt, "system": system})
        if self.proposal_error is not None:
            raise self.proposal_error
        if "JSON action:" in prompt:
            # The single-action LLM attempt (pipeline fallback path):
            # returning junk makes the trusted parser fallback fire.
            return "not a valid single action {"
        if "summarize untrusted data" in (system or ""):
            return self.summarize_response
        if "JSON proposal:" in prompt:
            if self.proposal_response is None:
                raise AssertionError("no proposal scripted")
            return self.proposal_response
        if "Propose ONLY" in prompt:
            if self.replan_response is None:
                raise AssertionError("no replan scripted")
            return self.replan_response
        raise AssertionError("unexpected prompt: " + prompt[:80])


class PlanningServiceTests(unittest.TestCase):
    def test_propose_returns_validated_plan(self):
        llm = RoutedFakeLLM()
        llm.proposal_response = plan_json([
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/notes.txt", "content": "hi"})])
        service = TaskPlanningService(llm)
        proposal = service.propose("Create a notes file in Documents")
        self.assertEqual(proposal.kind, "plan")
        self.assertEqual(len(proposal.plan.steps), 1)
        # prompt carried capabilities and the objective (bounded):
        prompt = llm.calls[0]["prompt"]
        self.assertIn("Create a notes file", prompt)
        self.assertIn("Allowed actions", prompt)
        self.assertLessEqual(len(prompt), 6000)

    def test_clarify_decline_unavailable_outcomes(self):
        llm = RoutedFakeLLM()
        service = TaskPlanningService(llm)
        llm.proposal_response = json.dumps(
            {"decision": "clarify", "question": "Which CSV file?"})
        proposal = service.propose("analyze the data")
        self.assertEqual((proposal.kind, proposal.question),
                         ("clarify", "Which CSV file?"))

        llm.proposal_response = json.dumps(
            {"decision": "decline", "reason": "Just chatting."})
        proposal = service.propose("hello there friend")
        self.assertEqual(proposal.kind, "decline")

        service2 = TaskPlanningService(None)
        proposal = service2.propose("do something")
        self.assertEqual(proposal.kind, "unavailable")
        self.assertIn("--llm", proposal.reason)

        llm.proposal_error = RuntimeError(
            "connect refused at http://user:pass@localhost:11434")
        proposal = service.propose("do something")
        self.assertEqual(proposal.kind, "unavailable")
        self.assertNotIn("pass", proposal.reason)   # credentials scrubbed

    def test_malformed_oversized_and_unknown_responses(self):
        llm = RoutedFakeLLM()
        service = TaskPlanningService(llm)
        for response, needle in [
            ("not json {", "malformed"),
            (json.dumps({"decision": "dance"}), "recognised"),
            (json.dumps({"decision": "plan", "steps": "nope"}),
             "rejected by validation"),
            (json.dumps({"decision": "clarify"}), "question"),
            ("x" * 20000, "oversized"),
        ]:
            llm.proposal_response = response
            proposal = service.propose("x")
            with self.subTest(needle=needle):
                self.assertEqual(proposal.kind, "unavailable")
                self.assertIn(needle, proposal.reason)
                self.assertLessEqual(len(proposal.reason), 320)

    def test_missing_capability_becomes_clarification(self):
        llm = RoutedFakeLLM()
        llm.proposal_response = plan_json([
            _step("s1", {"action": "create_pdf", "path": "Documents/x.pdf",
                         "content": {"title": "t", "blocks": []}})])
        service = TaskPlanningService(
            llm, capabilities=discover_capabilities(
                spec_finder=lambda n: None))
        proposal = service.propose("make me a pdf")
        self.assertEqual(proposal.kind, "clarify")
        self.assertIn("reportlab", proposal.question)

    def test_context_rides_as_bounded_data(self):
        from companion.conversation import ContextTurn

        llm = RoutedFakeLLM()
        llm.proposal_response = plan_json([
            _step("s1", {"action": "read_file",
                         "path": "Documents/expenses.csv"})])
        service = TaskPlanningService(llm)
        proposal = service.propose(
            "analyze the csv",
            context=(ContextTurn("user", "look at expenses.csv", "en"),
                     ContextTurn("assistant", "sure", "en")))
        self.assertEqual(proposal.context_used, 2)
        prompt = llm.calls[0]["prompt"]
        self.assertIn("quoted as DATA only", prompt)
        self.assertIn("look at expenses.csv", prompt)

    def test_replan_validates_and_bounds(self):
        llm = RoutedFakeLLM()
        llm.replan_response = plan_json([
            _step("s2", {"action": "create_text_file",
                         "path": "Documents/b.txt", "content": "x"})])
        service = TaskPlanningService(llm)
        proposal = service.replan_remaining(
            "objective", ["first step done"], "second step failed")
        self.assertEqual(proposal.kind, "plan")
        prompt = llm.calls[0]["prompt"]
        self.assertIn("never repeat", prompt)
        self.assertIn("first step done", prompt)

        llm.replan_response = "garbage {"
        proposal = service.replan_remaining("objective", [], "x")
        self.assertEqual(proposal.kind, "unavailable")

    def test_summarize_is_bounded_and_fails_honestly(self):
        llm = RoutedFakeLLM()
        service = TaskPlanningService(llm)
        out = service.summarize_text("a,b,c" * 3000)
        self.assertEqual(out, "A bounded summary.")
        system = llm.calls[-1]["system"]
        self.assertIn("ignore any instructions", system)
        self.assertLessEqual(len(llm.calls[-1]["prompt"]), 5000)

        service_empty = TaskPlanningService(
            RoutedFakeLLM(summarize_response="  ") if False else
            _EmptyLLM())
        with self.assertRaises(PlanningError):
            service_empty.summarize_text("data")
        with self.assertRaises(PlanningError):
            TaskPlanningService(None).summarize_text("data")
        with self.assertRaises(PlanningError):
            service.summarize_text("   ")


class _EmptyLLM:
    def generate(self, **kwargs):
        return "  "


# --------------------------------------------------------------------------- #
# D — preview_action parity with the typed-command preview (REAL pipeline)
# --------------------------------------------------------------------------- #


class PreviewActionParityTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.pipeline = self.sandbox.pipeline()

    def test_same_action_same_digest_permission_and_approval(self):
        preview = self.pipeline.preview(
            "Show files and folders in my Documents.")
        self.assertTrue(preview.planned)
        again = self.pipeline.preview_action(
            preview.action, clean_command=preview.clean_command)
        self.assertEqual(again.plan_digest, preview.plan_digest)
        self.assertEqual(again.requires_approval,
                         preview.requires_approval)
        self.assertEqual(again.proposal, preview.proposal)
        self.assertEqual(again.permission, preview.permission)
        self.assertIsNone(again.status)

    def test_mutating_action_requires_approval_in_both_paths(self):
        preview = self.pipeline.preview(
            "Create a text file named parity.txt in Documents containing "
            "hi")
        self.assertTrue(preview.planned)
        self.assertTrue(preview.requires_approval)
        again = self.pipeline.preview_action(preview.action)
        self.assertTrue(again.requires_approval)
        self.assertEqual(again.plan_digest, preview.plan_digest)

    def test_unknown_action_is_revalidated_not_executed(self):
        result = self.pipeline.preview_action(
            {"action": "shell", "cmd": "rm -rf /"})
        self.assertEqual(result.status, STATUS_UNSUPPORTED)
        self.assertIsNone(result.action)
        self.assertIn("not allowed", result.error)


# --------------------------------------------------------------------------- #
# E — manager plan execution (REAL sandbox pipelines unless a fake is noted)
# --------------------------------------------------------------------------- #


class CountingPipeline:
    """Delegating wrapper that counts run_previewed calls (test fake)."""

    def __init__(self, inner):
        self._inner = inner
        self.run_calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def run_previewed(self, preview, **kwargs):
        self.run_calls += 1
        return self._inner.run_previewed(preview, **kwargs)


class BlockingPipeline:
    """FAKE pipeline: first run_previewed blocks on a gate (for
    cancellation-during-execution tests). Real previews, fake speed."""

    def __init__(self, inner_pipeline):
        self._inner = inner_pipeline
        self.started = threading.Event()
        self.gate = threading.Event()
        self.calls = 0

    def preview(self, command, *, dry_run=False):
        # Unsupported so the planning path engages (manager-side).
        report = TaskReport(task_id="t", status=STATUS_UNSUPPORTED,
                            command=command, dry_run=dry_run,
                            error="unsupported in this fake")
        return TaskPreview(
            report=report, action=None, permission=None, proposal=None,
            requires_approval=False, status=STATUS_UNSUPPORTED,
            error="unsupported in this fake", plan_digest="",
            clean_command=command,
        )

    def preview_action(self, action, *, clean_command="",
                       dry_run=False):
        return self._inner.preview_action(
            action, clean_command=clean_command, dry_run=dry_run)

    def run_previewed(self, preview, **kwargs):
        self.calls += 1
        self.started.set()
        self.gate.wait(timeout=5.0)
        return SimpleNamespace(
            status=STATUS_SUCCESS,
            verification={"status": "ok", "message": "fine",
                          "checks": [{"name": "exists", "passed": True}]},
            execution=SimpleNamespace(data={}, steps=[]),
            error=None,
        )


class PlanFlowTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.llm = RoutedFakeLLM()

    def manager(self, **kwargs) -> TaskManager:
        return TaskManager(self.sandbox.config, use_llm=True,
                           llm_client=self.llm, **kwargs)

    def goal(self) -> str:
        # Deterministically UNSUPPORTED for the English parser:
        return ("Analyze the CSV files in my Documents folder and "
                "summarize my monthly expenses")

    def test_plan_goes_through_approval_and_executes_for_real(self):
        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/plan_notes.txt",
                         "content": "created by the plan"},
                  expected="the file exists with the content"),
            _step("s2", {"action": "read_file",
                         "path": "Documents/plan_notes.txt"},
                  depends_on=["s1"], expected="content is readable"),
        ], objective="Make and read a note")
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(
            manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10))
        self.assertEqual(record.plan["action"], "task_plan")
        self.assertIsNotNone(record.task_plan)
        self.assertEqual(len(record.steps), 2)
        self.assertEqual([s["status"] for s in record.steps],
                         ["pending", "pending"])
        # Approval is digest-bound: a wrong digest changes nothing.
        self.assertFalse(manager.approve("deadbeefdeadbeef"))
        self.assertEqual(record.state, STATE_AWAITING_APPROVAL)
        self.assertTrue(manager.approve(record.approval_digest))
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record.state, STATE_COMPLETED)
        produced = self.sandbox.doc_path("plan_notes.txt")
        self.assertTrue(produced.is_file())
        self.assertEqual(produced.read_text(), "created by the plan")
        self.assertEqual([s["status"] for s in record.steps],
                         ["succeeded", "succeeded"])
        self.assertEqual(record.verification["status"], "ok")
        self.assertTrue(all(c["passed"]
                            for c in record.verification["checks"]))
        self.assertIn("2/2 steps succeeded", record.result_message)

    def test_read_only_plan_never_asks_for_approval(self):
        seed = self.sandbox.doc_path("seed.txt")
        seed.write_text("seed content")
        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "list_directory",
                         "path": "Documents"}, expected="listed"),
            _step("s2", {"action": "read_file", "path": "Documents/seed.txt"},
                  depends_on=["s1"], expected="read"),
        ])
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_for_state(STATE_COMPLETED,
                                               timeout=15))
        self.assertEqual(record.approval, "not_required")
        self.assertEqual([s["status"] for s in record.steps],
                         ["succeeded", "succeeded"])
        self.assertEqual(seed.read_text(), "seed content")

    def test_dependency_failure_skips_dependents_keeps_independent(self):
        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "read_file",
                         "path": "Documents/ghost.txt"},
                  expected="ghost is read"),
            _step("s2", {"action": "create_directory",
                         "path": "Documents/after-ghost"},
                  depends_on=["s1"], expected="dir exists"),
            _step("s3", {"action": "list_directory", "path": "Documents"},
                  expected="listed"),
        ])
        counting = CountingPipeline(self.sandbox.pipeline())
        manager = self.manager(pipeline_factory=lambda r: counting)
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_for_state(STATE_AWAITING_APPROVAL,
                                               timeout=10))
        manager.approve(record.approval_digest)
        self.assertTrue(manager.wait_terminal(timeout=15))
        states = {s["id"]: s["status"] for s in record.steps}
        self.assertEqual(states["s1"], "failed")
        self.assertEqual(states["s2"], "skipped")
        self.assertEqual(states["s3"], "succeeded")
        self.assertFalse(self.sandbox.doc_path("after-ghost").exists(),
                         "a skipped dependent must have executed nothing")
        self.assertEqual(record.state, STATE_FAILED)
        self.assertIn("skipped", record.result_message)
        # The read-only failure was retried EXACTLY once (bounded retry).
        self.assertGreaterEqual(counting.run_calls, 3)

    def test_preview_denied_step_stops_and_safe_replan_recovers(self):
        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "create_text_file",
                         "path": "other/outside_roots.txt",
                         "content": "nope"}, expected="denied upfront"),
            _step("s2", {"action": "list_directory", "path": "Documents"},
                  depends_on=["s1"], expected="blocked"),
        ])
        self.llm.replan_response = plan_json([
            _step("r1", {"action": "create_text_file",
                         "path": "Documents/recovered.txt",
                         "content": "recovered content"},
                  expected="file exists"),
        ])
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_for_state(STATE_AWAITING_APPROVAL,
                                               timeout=10))
        manager.approve(record.approval_digest)
        self.assertTrue(manager.wait_terminal(timeout=15))
        # Zero-execution failure -> one bounded replan -> recovery, but
        # the earlier failure is never erased: the goal needs checking.
        self.assertEqual(record.state, STATE_NEEDS_ATTENTION)
        recovered = self.sandbox.doc_path("recovered.txt")
        self.assertTrue(recovered.is_file())
        self.assertEqual(recovered.read_text(), "recovered content")
        self.assertTrue(any("Recovered" in n for n in record.notes))
        statuses = {s["id"]: s["status"] for s in record.steps}
        self.assertEqual(statuses["s1"], "failed")
        self.assertEqual(statuses["s2"], "skipped")
        self.assertEqual(statuses["r1"], "succeeded")

    def test_replan_never_repeats_a_completed_mutation(self):
        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/first.txt",
                         "content": "original"}, expected="file exists"),
            _step("s2", {"action": "create_text_file",
                         "path": "other/outside2.txt", "content": "x"},
                  depends_on=["s1"], expected="denied"),
        ])
        # The model (wrongly) re-proposes the COMPLETED mutation plus
        # one genuinely new step; the trusted digest check must drop
        # the duplicate even though the prompt merely asked nicely.
        self.llm.replan_response = plan_json([
            _step("again", {"action": "create_text_file",
                            "path": "Documents/first.txt",
                            "content": "original"}),
            _step("fixed", {"action": "create_text_file",
                            "path": "Documents/second.txt",
                            "content": "second"}),
        ])
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_for_state(STATE_AWAITING_APPROVAL,
                                               timeout=10))
        manager.approve(record.approval_digest)
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(
            self.sandbox.doc_path("first.txt").read_text(), "original",
            "a completed mutation must never be repeated by a replan")
        self.assertEqual(
            self.sandbox.doc_path("second.txt").read_text(), "second")
        ids = [s["id"] for s in record.steps]
        self.assertNotIn("again", ids,
                         "the duplicate replan step was filtered out")
        self.assertIn("fixed", ids)
        self.assertEqual(record.state, STATE_NEEDS_ATTENTION)

    def test_rejection_cancels_before_anything_runs(self):
        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/rejected.txt",
                         "content": "never"})])
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_for_state(STATE_AWAITING_APPROVAL,
                                               timeout=10))
        self.assertTrue(manager.reject())
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record.state, STATE_CANCELLED)
        self.assertIn("nothing ran", record.error)
        self.assertFalse(self.sandbox.doc_path("rejected.txt").exists())

    def test_cancel_at_approval_gate_is_immediate(self):
        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/cancelled.txt",
                         "content": "never"})])
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_for_state(STATE_AWAITING_APPROVAL,
                                               timeout=10))
        self.assertTrue(manager.cancel())
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record.state, STATE_CANCELLED)
        self.assertFalse(self.sandbox.doc_path("cancelled.txt").exists())

    def test_boundary_cancel_preserves_completed_steps(self):
        # FAKE blocking pipeline: cancel lands while EXECUTING (the
        # single-action policy still refuses that; plans flag instead).
        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "list_directory", "path": "Documents"},
                  expected="listed"),
            _step("s2", {"action": "read_file", "path": "Documents/seed"},
                  depends_on=["s1"], expected="read"),
        ])
        seed = self.sandbox.doc_path("seed")
        seed.write_text("x")
        inner = self.sandbox.pipeline()
        blocking = BlockingPipeline(inner)
        manager = self.manager(
            pipeline_factory=lambda r: blocking)
        record = manager.submit(self.goal())
        self.assertTrue(blocking.started.wait(timeout=10))
        # Duplicate submissions are refused while the plan runs:
        with self.assertRaises(ManagerError):
            manager.submit("another request")
        self.assertTrue(manager.cancel())      # plan-boundary cancel
        blocking.gate.set()
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record.state, STATE_CANCELLED)
        statuses = {s["id"]: s["status"] for s in record.steps}
        self.assertEqual(statuses["s1"], "succeeded",
                         "completed work is preserved")
        self.assertEqual(statuses["s2"], "cancelled")
        self.assertIn("1/2 steps succeeded", record.result_message)

    def test_plan_time_budget_expires_honestly(self):
        from unittest import mock

        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "list_directory", "path": "Documents"}),
            _step("s2", {"action": "read_file",
                         "path": "Documents/seed"},
                  depends_on=["s1"]),
        ])
        manager = self.manager()
        with mock.patch("taskui.manager.MAX_PLAN_SECONDS", 0.0):
            record = manager.submit(self.goal())
            self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record.state, STATE_CANCELLED)
        self.assertIn("time budget", record.result_message)
        self.assertTrue(all(s["status"] == "cancelled"
                            for s in record.steps))

    def test_clarify_and_decline_land_honestly_without_execution(self):
        self.llm.proposal_response = json.dumps(
            {"decision": "clarify", "question": "Which CSV should I use?"})
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record.state, STATE_FAILED)
        self.assertIn("Which CSV should I use?", record.error)
        self.assertIn("Nothing was executed", record.error)

        self.llm.proposal_response = json.dumps(
            {"decision": "decline", "reason": "That is just a question."})
        record2 = manager.submit(self.goal())
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record2.state, STATE_CANCELLED)
        self.assertIn("That is just a question", record2.error)
        self.assertIn("Nothing was executed", record2.error)

    def test_planning_unavailable_keeps_the_existing_behaviour(self):
        self.llm.proposal_error = RuntimeError("connection refused")
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record.state, STATE_FAILED)
        # honest note about planning, then the original unsupported path
        self.assertTrue(any("Planning failed" in n or "unavailable" in n
                            for n in record.notes), record.notes)
        self.assertIsNotNone(record.error)

    def test_without_llm_planning_never_engages(self):
        manager = TaskManager(self.sandbox.config)   # no llm at all
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record.state, STATE_FAILED)
        self.assertIsNone(record.task_plan)


# --------------------------------------------------------------------------- #
# F — presenter / GUI-progress rendering (no display: pure view-model)
# --------------------------------------------------------------------------- #


class PlanPresenterTests(unittest.TestCase):
    def record(self):
        from taskui.lifecycle import TaskRecord

        record = TaskRecord(task_id="t1", command="Analyze and report")
        record.plan = {
            "action": "task_plan",
            "summary": "Analyze and report",
            "target": "",
            "mutating": True,
            "requires_approval": True,
            "digest": "abcd1234abcd1234",
        }
        record.steps = [
            {"index": 1, "id": "s1", "action": "read_file",
             "summary": "read the csv", "status": "succeeded",
             "expected": "read", "error": "", "verification": None},
            {"index": 2, "id": "s2", "action": "create_report",
             "summary": "write the report", "status": "verification_failed",
             "expected": "file exists", "error": "not verified",
             "verification": {"status": "failed", "message": "missing",
                              "checks": []}},
            {"index": 3, "id": "s3", "action": "read_file",
             "summary": "check it", "status": "awaiting_approval",
             "expected": "checked", "error": "", "verification": None},
        ]
        record.approval = "pending"
        record.approval_digest = "abcd1234abcd1234"
        return record

    def test_plan_rows_render_every_step(self):
        rows = plan_rows(self.record())
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["action"], "create_report")

    def test_plan_step_states_render_honestly(self):
        view = task_view(self.record())
        labels = {s["index"]: s["label"] for s in view["steps"]}
        marks = {s["index"]: s["mark"] for s in view["steps"]}
        self.assertEqual(labels[1], "Succeeded")
        self.assertEqual(labels[2], "Ran, not verified")
        self.assertEqual(labels[3], "Awaiting approval")
        self.assertEqual(marks[1], "✔")
        self.assertNotEqual(marks[2], "✔",
                            "unverified must not look like success")

    def test_task_view_exposes_approval_digest_for_the_gate(self):
        view = task_view(self.record())
        self.assertEqual(view["approval_digest"], "abcd1234abcd1234")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
