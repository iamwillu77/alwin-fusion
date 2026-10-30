"""Typed, validated task plans for Alwin Fusion (v0.13.0).

A *plan* is an ordered, dependency-carrying list of **already-closed-
set actions** — never arbitrary code, never model-generated shell.  The
model (if any) only *proposes* a plan as JSON; this module is the
trusted validator: anything malformed, cyclic, unknown, oversized or
unsupported is rejected here before a single step can be previewed.

Design rules (enforced by tests):

* every step's ``action`` must pass :func:`ai_core.planner.
  validate_action` — the same closed-set validator the single-action
  pipeline uses (unknown tools/operations are rejected, model output
  never becomes executable code),
* risk is **derived** from the trusted ``MUTATING_ACTIONS`` set — a
  proposal cannot claim its own step is "safe",
* dependencies may only reference earlier steps (the plan executes in
  order), duplicates and cycles are rejected, bounds are configurable
  but hard-capped (steps, objective, model output size),
* step inputs may carry ONE explicit derivation (``from_step`` +
  whitelisted field) resolved at execution time from a *verified*
  step's result — bounded and re-validated like any other input;
  since v0.14 the derivation source must be an **earlier** step
  (unknown/self/forward references are rejected at validation) and is
  hoisted into ``depends_on`` when the step omitted it,
* plans are plain data: round-trippable dicts, no I/O, no threads, no
  imports of integrations — safe to unit-test headless.

Suggested-by-brief step states are used verbatim (lowercase):
``pending``, ``awaiting_approval``, ``ready``, ``running``,
``succeeded``, ``failed``, ``skipped``, ``cancelled``,
``verification_failed``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from ai_core.planner import DERIVE_PREFIX, MUTATING_ACTIONS, validate_action

#: Hard plan bounds (the LLM's output budget is bounded separately by
#: the planning service; these are the trusted ceilings).
MAX_PLAN_STEPS = 12
MAX_PLAN_OBJECTIVE_CHARS = 500
MAX_PLAN_STEP_SUMMARY_CHARS = 200
MAX_PLAN_JSON_CHARS = 12_000
#: A derivation may inject at most this much text into a later step.
MAX_DERIVED_CHARS = 5_000
#: Whitelisted result fields a later step may derive from.
DERIVABLE_FIELDS = ("content", "path", "bytes", "text")

#: Plan-level step states (brief-suggested names, lowercase).
STEP_PENDING = "pending"
STEP_AWAITING_APPROVAL = "awaiting_approval"
STEP_READY = "ready"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_CANCELLED = "cancelled"
STEP_VERIFICATION_FAILED = "verification_failed"

STEP_STATES = (
    STEP_PENDING, STEP_AWAITING_APPROVAL, STEP_READY, STEP_RUNNING,
    STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED, STEP_CANCELLED,
    STEP_VERIFICATION_FAILED,
)

#: Terminal-in-failure states that cause dependents to be skipped.
FAILED_STATES = frozenset({STEP_FAILED, STEP_SKIPPED, STEP_CANCELLED,
                           STEP_VERIFICATION_FAILED})

# --------------------------------------------------------------------------- #
# v0.20: idempotency classification (semantics-based, closed, pinned)
# --------------------------------------------------------------------------- #
# What repeating an already-executed action means for RECOVERY
# decisions.  Classifications are derived from the actual semantics of
# each closed action — never from "it usually looks harmless":
#
# * read_only          — no state change possible; safe to repeat.
# * repeatable_now     — repeating cannot destroy anything (the tool
#                        layer refuses to clobber; a no-op repeat is a
#                        harmless, honest failure).
# * reconcile          — a mutation whose CURRENT STATE must be
#                        checked before any retry (is the app open? the
#                        control still there? the file already the
#                        wanted end-state?).
# * never_auto_repeat  — a mutation whose repetition is never safe to
#                        do automatically (text entry, saves, created
#                        documents, whole sequences).
IDEM_READ_ONLY = "read_only"
IDEM_REPEATABLE = "repeatable_now"
IDEM_RECONCILE = "reconcile_before_retry"
IDEM_NEVER = "never_auto_repeat"
IDEMPOTENCY_CLASSES = (IDEM_READ_ONLY, IDEM_REPEATABLE, IDEM_RECONCILE,
                       IDEM_NEVER)

IDEMPOTENCY: Dict[str, str] = {
    # read-only observations
    "list_directory": IDEM_READ_ONLY,
    "read_file": IDEM_READ_ONLY,
    "detect_applications": IDEM_READ_ONLY,
    "inspect_application": IDEM_READ_ONLY,
    "find_control": IDEM_READ_ONLY,
    "read_control": IDEM_READ_ONLY,
    "list_controls": IDEM_READ_ONLY,
    "application_status": IDEM_READ_ONLY,
    "active_window": IDEM_READ_ONLY,
    # mkdir on an existing directory is refused by the tool layer —
    # repeating it cannot destroy anything.
    "create_directory": IDEM_REPEATABLE,
    # launch/open: verify the process/window state first.
    "open_file": IDEM_RECONCILE,
    "launch_application": IDEM_RECONCILE,
    "focus_application": IDEM_RECONCILE,
    "close_application": IDEM_RECONCILE,
    "activate_control": IDEM_RECONCILE,
    # replacement: confirm the current bytes before deciding.
    "replace_file_content": IDEM_RECONCILE,
    # creations overwrite nothing (O_EXCL) but repeating them is
    # never an automatic decision — and typed text/saves/sequences
    # must never be replayed without a human.
    "create_text_file": IDEM_NEVER,
    "create_report": IDEM_NEVER,
    "create_pdf": IDEM_NEVER,
    "create_spreadsheet": IDEM_NEVER,
    "enter_text": IDEM_NEVER,
    "save_document": IDEM_NEVER,
    "task_sequence": IDEM_NEVER,
}


def idempotency_of(action: Dict[str, Any]) -> str:
    """Recovery classification of a validated action (closed table).

    Unknown names are conservative: ``never_auto_repeat`` — a new or
    malformed action can never be treated as safe to repeat.
    """
    name = str((action or {}).get("action") or "")
    return IDEMPOTENCY.get(name, IDEM_NEVER)


#: Step states a resume must treat as UNCERTAIN for a dispatched
#: mutation (executed, but the outcome is not verified): continuing
#: automatically could repeat or corrupt the mutation.
UNCERTAIN_MUTATION_STATES = frozenset({STEP_FAILED,
                                       STEP_VERIFICATION_FAILED})


def build_resume_plan(
    original: TaskPlan,
    *,
    verified_results: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[Optional[TaskPlan], List[str], List[str]]:
    """Residual plan for a safe resume (pure; v0.20).

    Returns ``(residual_plan_or_None, notes, reconciliation)``:

    * steps already COMPLETED-and-verified are excluded and their
      verified results are replayed (the caller pre-seeds them so
      derivations from earlier steps still resolve),
    * mutating steps with an UNCERTAIN outcome (dispatched, then failed
      or verification-failed) are excluded and listed in
      ``reconciliation`` — a human must confirm the real state before
      they may run again,
    * mutating steps that never dispatched (failed at resolution or
      preview) and every not-started/skipped step are included,
    * the residual keeps the original order and dependency ids; the
      caller must request a FRESH plan-level approval for its digest.

    ``None`` plan means there is nothing left to resume (honest).
    """
    verified_results = verified_results or {}
    residual: List[PlanStep] = []
    notes: List[str] = []
    reconciliation: List[str] = []
    skipped_done = 0
    for step in original.steps:
        if step.state == STEP_SUCCEEDED and step.id in verified_results:
            skipped_done += 1
            continue
        if (step.mutating
                and step.state in UNCERTAIN_MUTATION_STATES
                and getattr(step, "dispatched", False)):
            reconciliation.append(
                f"{step.id} ({step.summary}) — the mutation was "
                f"dispatched but its outcome is not verified; confirm "
                f"the real state before running it again.")
            continue
        residual.append(replace(step, state=STEP_PENDING, result=None,
                                error=None, verification=None))
    if skipped_done:
        notes.append(
            f"{skipped_done} completed, verified step(s) will NOT be "
            f"repeated.")
    if reconciliation:
        notes.append(
            f"{len(reconciliation)} step(s) need reconciliation before "
            f"they may run again (uncertain outcome — never retried "
            f"automatically).")
    if not residual:
        return None, notes, reconciliation
    if len(residual) > MAX_PLAN_STEPS:  # pragma: no cover - defensive
        residual = residual[:MAX_PLAN_STEPS]
        notes.append("Resume plan truncated to the step limit.")
    return (TaskPlan(objective=original.objective, steps=residual),
            notes, reconciliation)


_PLAN_KEYS = {"objective", "steps"}
_STEP_KEYS = {"id", "summary", "action", "depends_on", "expected"}


class PlanValidationError(Exception):
    """A proposed plan is malformed, unsafe or unsupported."""


@dataclass
class Derivation:
    """Pull one whitelisted field from an earlier step's verified result.

    ``summarize=True`` (opt-in, shown in the plan) additionally runs
    the pulled content through the bounded, injection-resistant
    summarizer before it is injected — for report-style steps.
    """

    from_step: str
    field: str
    summarize: bool = False

    @classmethod
    def parse(cls, raw: Any) -> Optional["Derivation"]:
        if not isinstance(raw, dict):
            return None
        keys = set(raw)
        if keys not in ({"from_step", "field"},
                        {"from_step", "field", "summarize"}):
            raise PlanValidationError(
                "A derivation must have exactly 'from_step' and 'field' "
                "(plus optional 'summarize')."
            )
        step = raw.get("from_step")
        field_name = raw.get("field")
        if not isinstance(step, str) or not step:
            raise PlanValidationError("Derivation 'from_step' must be an id.")
        if field_name not in DERIVABLE_FIELDS:
            raise PlanValidationError(
                f"Derivation field must be one of {DERIVABLE_FIELDS}."
            )
        summarize = raw.get("summarize", False)
        if not isinstance(summarize, bool):
            raise PlanValidationError("Derivation 'summarize' must be boolean.")
        return cls(step, field_name, summarize)


@dataclass
class PlanStep:
    """One ordered step of a plan (trusted after validate_plan)."""

    id: str
    summary: str
    action: Dict[str, Any]
    depends_on: Tuple[str, ...] = ()
    expected: str = ""
    #: derived from MUTATING_ACTIONS — never taken from model output.
    mutating: bool = False
    #: runtime state (starts pending).
    state: str = STEP_PENDING
    #: execution outcome (curated; never raw file contents).
    result: Optional[str] = None
    error: Optional[str] = None
    verification: Optional[Dict[str, Any]] = None
    #: derivation resolved at execution time (None = fully static).
    derives: Dict[str, Derivation] = field(default_factory=dict)
    #: v0.20 runtime flag: True once the step was actually dispatched
    #: to the pipeline (distinguishes "failed before executing" from
    #: "failed after the mutation was attempted" for recovery).
    dispatched: bool = False

    @property
    def requires_approval(self) -> bool:
        return self.mutating


@dataclass
class TaskPlan:
    """A validated, executable plan (plain data + runtime state)."""

    objective: str
    steps: List[PlanStep]

    def step_by_id(self, step_id: str) -> Optional[PlanStep]:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    @property
    def mutating_steps(self) -> List[PlanStep]:
        return [s for s in self.steps if s.mutating]

    @property
    def requires_approval(self) -> bool:
        return any(s.mutating for s in self.steps)

    # ------------------------------------------------------------------ #
    # Serialization (plans cross the LLM as JSON and the UI as dicts)
    # ------------------------------------------------------------------ #

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objective": self.objective,
            "steps": [
                {
                    "id": s.id,
                    "summary": s.summary,
                    "action": dict(s.action),
                    "depends_on": list(s.depends_on),
                    "expected": s.expected,
                }
                for s in self.steps
            ],
        }

    def status_summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for step in self.steps:
            counts[step.state] = counts.get(step.state, 0) + 1
        return counts


def _bounded(text: Any, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) > limit:
        return clean[: limit - 1] + "…"
    return clean


def _validate_step_order(steps: List[PlanStep]) -> None:
    """Dependencies must reference earlier steps (ordered execution)."""
    seen: List[str] = []
    for step in steps:
        for dep in step.depends_on:
            if dep == step.id:
                raise PlanValidationError(
                    f"Step '{step.id}' cannot depend on itself."
                )
            if dep not in seen:
                if any(dep == s.id for s in steps):
                    raise PlanValidationError(
                        f"Step '{step.id}' depends on '{dep}', which is "
                        "not an earlier step (plans execute in order)."
                    )
                raise PlanValidationError(
                    f"Step '{step.id}' depends on unknown step '{dep}'."
                )
        seen.append(step.id)


def _validate_and_hoist_derivations(steps: List[PlanStep]) -> None:
    """v0.14: derivation sources must be *earlier* steps; hoist them
    into ``depends_on``.

    A derivation pulls a field from an earlier step's VERIFIED result,
    so a source that is unknown, the step itself, or a later step can
    never be satisfied — reject it at validation time (the brief's
    "outputs referenced before their producing steps") instead of
    letting it fail at execution.  When the reference is valid but the
    step did not declare ``depends_on`` on it, the source id is hoisted
    into ``depends_on`` (sanitising normalisation, same precedent as
    the misplaced-key hoist): the execution gate already treated it as
    a prerequisite, so this only makes the DISPLAYED dependency
    accurate — it never adds an executable capability.
    """
    seen: List[str] = []
    for step in steps:
        for deriv in step.derives.values():
            source = deriv.from_step
            if source == step.id:
                raise PlanValidationError(
                    f"Step '{step.id}' derives from itself."
                )
            if source not in seen:
                if any(source == s.id for s in steps):
                    raise PlanValidationError(
                        f"Step '{step.id}' derives from '{source}', which "
                        "is not an earlier step (a step can only use "
                        "results of steps that already ran)."
                    )
                raise PlanValidationError(
                    f"Step '{step.id}' derives from unknown step "
                    f"'{source}'."
                )
            if source not in step.depends_on:
                step.depends_on = step.depends_on + (source,)
        seen.append(step.id)


def validate_plan(raw: Any) -> TaskPlan:
    """Validate untrusted (often model-produced) plan JSON → TaskPlan.

    Raises :class:`PlanValidationError` with a user-presentable,
    bounded reason on the first problem found.  Never executes,
    imports or contacts anything.
    """
    if isinstance(raw, (bytes, str)):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if len(raw) > MAX_PLAN_JSON_CHARS:
            raise PlanValidationError(
                f"The proposed plan is too large "
                f"(> {MAX_PLAN_JSON_CHARS} characters)."
            )
        import json

        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise PlanValidationError(
                f"The proposed plan is not valid JSON: {exc}"
            ) from exc

    if not isinstance(raw, dict):
        raise PlanValidationError("A plan must be a JSON object.")
    unknown = set(raw) - _PLAN_KEYS
    if unknown:
        raise PlanValidationError(
            f"Unknown plan fields: {sorted(unknown)}."
        )

    objective = _bounded(raw.get("objective"), MAX_PLAN_OBJECTIVE_CHARS)
    if not objective:
        raise PlanValidationError("The plan has no objective.")

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanValidationError("The plan has no steps.")
    if len(raw_steps) > MAX_PLAN_STEPS:
        raise PlanValidationError(
            f"The plan has {len(raw_steps)} steps "
            f"(maximum {MAX_PLAN_STEPS})."
        )

    steps: List[PlanStep] = []
    seen_ids = set()
    for index, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            raise PlanValidationError(f"Step {index} is not an object.")
        unknown = set(item) - _STEP_KEYS
        if unknown:
            raise PlanValidationError(
                f"Step {index} has unknown fields: {sorted(unknown)}."
            )

        action = item.get("action")
        if not isinstance(action, dict):
            raise PlanValidationError(
                f"Step {index} action must be an object."
            )

        # Tolerate one mechanical misplacement models actually make:
        # step-level keys inside the action object (including the id).
        # Hoisting is sanitisation, NOT relaxation — the hoisted values
        # run through exactly the same step validation below (unique
        # ids, dependency rules, closed-set actions), and the action
        # object itself is still strictly checked against the allowlist.
        action = dict(action)
        item = dict(item)
        for key in ("id", "summary", "depends_on", "expected"):
            if key in action:
                misplaced_value = action.pop(key)
                if key in item:
                    if item.get(key) != misplaced_value:
                        raise PlanValidationError(
                            f"Step {index} has conflicting '{key}' "
                            "inside its action and at step level."
                        )
                else:
                    item[key] = misplaced_value

        step_id = item.get("id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise PlanValidationError(f"Step {index} has no id.")
        step_id = _bounded(step_id, 40)
        if step_id in seen_ids:
            raise PlanValidationError(f"Duplicate step id '{step_id}'.")
        seen_ids.add(step_id)

        depends_on = item.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(
                isinstance(d, str) for d in depends_on):
            raise PlanValidationError(
                f"Step '{step_id}' has a malformed depends_on list."
            )
        depends_on = tuple(_bounded(d, 40) for d in depends_on)

        # Derivations live INSIDE string fields as {"from_step",
        # "field"} objects and become *string* markers, so the closed-
        # set validator (string-typed content/path fields) still passes;
        # the controller substitutes real values at execution time and
        # re-validates like any other input.
        derives: Dict[str, Derivation] = {}

        def _extract(value: Any) -> Any:
            if isinstance(value, dict) and "from_step" in value:
                deriv = Derivation.parse(value)
                key = f"{DERIVE_PREFIX}{step_id}:{len(derives)}"
                derives[key] = deriv
                return key
            if isinstance(value, dict):
                return {k: _extract(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_extract(v) for v in value]
            return value

        action = _extract(action)
        try:
            action = validate_action(action)
        except Exception as exc:  # noqa: BLE001 - closed-set rejection
            raise PlanValidationError(
                f"Step '{step_id}' is not a supported action or has "
                f"invalid inputs: {str(exc)[:200]}"
            ) from exc

        steps.append(PlanStep(
            id=step_id,
            summary=_bounded(item.get("summary"), MAX_PLAN_STEP_SUMMARY_CHARS)
                    or f"Step {index}",
            action=action,
            depends_on=depends_on,
            expected=_bounded(item.get("expected"), 300),
            mutating=action["action"] in MUTATING_ACTIONS,
            derives=derives,
        ))

    # Derivation references are validated (and hoisted into
    # depends_on) BEFORE the dependency-order check so the final
    # depends_on — including hoisted sources — is fully validated.
    _validate_and_hoist_derivations(steps)
    _validate_step_order(steps)
    return TaskPlan(objective=objective, steps=steps)


__all__ = [
    "DERIVABLE_FIELDS",
    "Derivation",
    "FAILED_STATES",
    "MAX_DERIVED_CHARS",
    "MAX_PLAN_JSON_CHARS",
    "MAX_PLAN_OBJECTIVE_CHARS",
    "MAX_PLAN_STEPS",
    "MAX_PLAN_STEP_SUMMARY_CHARS",
    "PlanStep",
    "PlanValidationError",
    "STEP_AWAITING_APPROVAL",
    "STEP_CANCELLED",
    "STEP_FAILED",
    "STEP_PENDING",
    "STEP_READY",
    "STEP_RUNNING",
    "STEP_SKIPPED",
    "STEP_SUCCEEDED",
    "STEP_STATES",
    "STEP_VERIFICATION_FAILED",
    "TaskPlan",
    "validate_plan",
]
