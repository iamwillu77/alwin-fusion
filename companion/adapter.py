"""Task-event adapter for the Alwin companion (v0.8.0).

This is the *only* bridge between the trusted task experience
(:class:`taskui.manager.TaskManager` / :class:`taskui.lifecycle.TaskRecord`)
and the character.  It converts real, already-trusted task lifecycle
states into character presentation states — and nothing else.

Hard rules (pinned by tests):

* the adapter **cannot grant a permission, approve a plan, or change a
  task outcome** — it holds no reference to the manager, no callback
  into the pipeline, and exposes no mutation surface; it is a pure
  function of a TaskRecord's curated fields,
* **only the trusted `completed` state** (which the lifecycle makes
  reachable only after verification) may drive ``celebrating``; a
  failed, cancelled or needs-attention task can never produce the
  success state — a task failure is mapped to ``error`` (or a calm
  ``idle`` for a user cancellation, which is a choice, not a failure),
* approval is mapped to ``waiting_for_approval`` and the adapter's
  speech line points the user to the trusted task window (where
  approval actually happens); the companion never approves,
* speech lines are drawn from curated fields (state label, plan
  summary, verification message) — bounded and sanitized; raw file
  content, model output and error internals are never spoken,
* a ``task_sequence`` reports the *running step's* kind (writing /
  coding / reading) when it can be identified from the validated step
  action, else the general ``working`` state.

The adapter is pure headless Python: no GTK, no threads, no I/O.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from companion.states import (
    STATE_CELEBRATING,
    STATE_CODING,
    STATE_ERROR,
    STATE_IDLE,
    STATE_LISTENING,
    STATE_READING,
    STATE_RESTING,
    STATE_SLEEPING,
    STATE_THINKING,
    STATE_VERIFYING,
    STATE_WAITING_FOR_APPROVAL,
    STATE_WALKING,
    STATE_WORKING,
    STATE_WRITING,
    CharacterController,
)

# --------------------------------------------------------------------------- #
# Speech (bounded, curated — never raw content)
# --------------------------------------------------------------------------- #

MAX_SPEECH_CHARS = 160

#: Calm, friendly lines per character state.  Placeholders:
#: ``{action}`` = the validated action name, ``{target}`` = a short
#: curated target label.  Nothing here interpolates raw user/model/file
#: text beyond those curated planner-produced fields.
STATE_LINES: Dict[str, str] = {
    STATE_LISTENING: "I'm listening!",
    STATE_THINKING: "Let me think about how to do that…",
    STATE_WALKING: "On it — heading over now.",
    STATE_WORKING: "Working on it…",
    STATE_WRITING: "Writing that for you…",
    STATE_CODING: "Coding that up…",
    STATE_READING: "Reading it now…",
    STATE_WAITING_FOR_APPROVAL: (
        "I need your approval before I change anything — "
        "please check the task window."
    ),
    STATE_VERIFYING: "Double-checking the result…",
    STATE_CELEBRATING: "Done and verified. Have a lovely day!",
    STATE_ERROR: "Something didn't work out — nothing was changed "
                 "that shouldn't be. Details are in the task window.",
    STATE_IDLE: "",
    STATE_SLEEPING: "",
    STATE_RESTING: "",
}

#: Speech shown when a plan is about to be inspected (planning).
PLANNING_LINE = "Planning the steps first…"

#: The kind of work implied by each validated action (character state).
#: Read-only actions map to reading; document/sequence work maps to
#: writing; a text file whose target looks like code maps to coding.
READING_ACTIONS = frozenset(
    {"read_file", "list_directory", "inspect_application", "find_control",
     "read_control", "explain"}
)
WRITING_ACTIONS = frozenset(
    {"create_report", "create_pdf", "create_spreadsheet", "save_document",
     "enter_text", "activate_control"}
)
CODING_TEXT_SUFFIXES = (
    ".py", ".sh", ".js", ".ts", ".c", ".h", ".cpp", ".hpp", ".rs", ".go",
    ".java", ".rb", ".pl", ".lua", ".sql", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg",
)


def _looks_like_code(path: str) -> bool:
    """True when a text-file target has a code/config extension.

    Deliberately conservative: only a recognised extension counts, so
    the ``coding`` state is used when it can be *reliably* identified —
    every other text file is honestly shown as writing.
    """
    lower = str(path or "").lower()
    return any(lower.endswith(suffix) for suffix in CODING_TEXT_SUFFIXES)


def action_state(action_name: str, *, target: str = "") -> str:
    """Character state for one validated action (best-effort, honest)."""
    name = str(action_name or "")
    if name in READING_ACTIONS:
        return STATE_READING
    if name == "create_text_file":
        return STATE_CODING if _looks_like_code(target) else STATE_WRITING
    if name in WRITING_ACTIONS:
        return STATE_WRITING
    # Everything else (create_directory, open_file, detect_applications,
    # unknown-but-validated future actions): a calm, honest "working".
    return STATE_WORKING


def _bounded(text: str, limit: int = MAX_SPEECH_CHARS) -> str:
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #

class TaskEventAdapter:
    """Pure mapping: TaskRecord -> character state (+ speech).

    Constructed with the character controller (or any callable sink);
    ``on_task_update(record)`` is safe to wire straight into
    ``TaskManager.on_update`` (which fires from the worker thread — the
    controller and view must therefore marshal to the UI thread, which
    the companion window does with ``GLib.idle_add``).

    The adapter keeps the last speech line for the view to render
    (``speech``) and the last record's honest outcome for status
    display — but it *never* stores or exposes plan digests, file
    content, or error internals beyond a short curated message.
    """

    def __init__(self, controller: CharacterController,
                 *, on_speech: Optional[Callable[[str], None]] = None) -> None:
        self.controller = controller
        #: Optional callback for new speech lines (view marshals it).
        self.on_speech = on_speech
        self.speech: str = ""
        self.last_task_id: str = ""
        self.last_task_state: str = ""
        self.last_action: str = ""

    #: Seconds of quiet before the character rests (configurable).
    rest_after_seconds: float = 300.0

    # ------------------------------------------------------------------ #
    # Events (all pure functions of the record)
    # ------------------------------------------------------------------ #

    def on_user_submit(self) -> None:
        """The user asked something: listening, then thinking begins."""
        self._say(STATE_LISTENING, STATE_LINES[STATE_LISTENING])

    def on_planning(self) -> None:
        self._say(STATE_THINKING, PLANNING_LINE)

    def on_task_update(self, record) -> None:
        """Map one TaskRecord onto a character state.  Never raises.

        This method accepts any object with the curated TaskRecord
        fields (duck-typed so tests can pass fakes); it reads only
        ``state``, ``plan``, ``steps``, ``report_status``, ``error``
        and ``result_message``.
        """
        try:
            self._map(record)
        except Exception as exc:  # noqa: BLE001 - presentation never breaks tasks
            # Honest degradation: stay calm, record nothing dangerous.
            self.controller.enter(STATE_IDLE, reason="adapter error")
            self._emit(f"(presentation hiccup: {type(exc).__name__})")

    def on_user_activity(self) -> None:
        """The user interacted (typed/clicked): wake to listening/idle."""
        state = self.controller.state
        if state in ("sleeping", "resting", "idle"):
            self.controller.enter(STATE_LISTENING, reason="user interaction")

    def on_idle(self, idle_seconds: float) -> None:
        """Long-quiet user: rest (never sleeping mid-task)."""
        state = self.controller.state
        if state == STATE_IDLE and idle_seconds >= self.rest_after_seconds:
            self.controller.enter("resting", reason="user quiet")

    # ------------------------------------------------------------------ #
    # Mapping core
    # ------------------------------------------------------------------ #

    def _map(self, record) -> None:
        task_state = getattr(record, "state", "") or ""
        self.last_task_id = str(getattr(record, "task_id", "") or "")
        self.last_task_state = task_state
        plan = getattr(record, "plan", None) or {}
        self.last_action = str(plan.get("action", "") or "")

        if task_state == "planning":
            self._enter(STATE_THINKING, STATE_LINES[STATE_THINKING])
            return
        if task_state == "awaiting_approval":
            # Approval stays actionable in the *trusted task window*;
            # the companion only points there.
            self._enter(STATE_WAITING_FOR_APPROVAL,
                        STATE_LINES[STATE_WAITING_FOR_APPROVAL])
            return
        if task_state == "approved":
            self._enter(STATE_WALKING, STATE_LINES[STATE_WALKING])
            return
        if task_state == "executing":
            self._enter(self._executing_state(record, plan),
                        self._executing_line(record, plan))
            return
        if task_state == "verifying":
            self._enter(STATE_VERIFYING, STATE_LINES[STATE_VERIFYING])
            return
        if task_state == "completed":
            # The ONLY path to celebrating: the lifecycle guarantees
            # completed is reachable only after verification passed.
            self._enter(STATE_CELEBRATING, STATE_LINES[STATE_CELEBRATING])
            return
        if task_state == "needs_attention":
            # The world may have changed but the result was not
            # verified: honest concern, never celebration.
            self._enter(STATE_ERROR,
                        "I couldn't fully verify that result — "
                        "please take a look in the task window.")
            return
        if task_state == "failed":
            self._enter(STATE_ERROR, STATE_LINES[STATE_ERROR])
            return
        if task_state == "cancelled":
            # A cancellation is the user's choice: calm, not an error,
            # and certainly not a success.
            self._enter(STATE_IDLE, "Okay, I've left everything as it was.")
            return
        # Unknown/foreign state: honest idle, no guessing.
        self._enter(STATE_IDLE, "")

    def _executing_state(self, record, plan: dict) -> str:
        # A running sequence reports its current step's kind.
        if str(plan.get("action", "")) == "task_sequence":
            steps = getattr(record, "steps", None) or []
            for step in steps:
                if isinstance(step, dict) and step.get("status") == "running":
                    return action_state(str(step.get("action", "")),
                                        target=str(step.get("summary", "")))
            return STATE_WORKING
        return action_state(str(plan.get("action", "")),
                            target=str(plan.get("target", "")))

    def _executing_line(self, record, plan: dict) -> str:
        state = self._executing_state(record, plan)
        return STATE_LINES.get(state, STATE_LINES[STATE_WORKING])

    # ------------------------------------------------------------------ #
    # Speech plumbing
    # ------------------------------------------------------------------ #

    def _enter(self, state: str, line: str) -> None:
        self.controller.enter(state, reason=line or state)
        if line:
            self._emit(line)

    def _say(self, state: str, line: str) -> None:
        self.controller.enter(state, reason=line or state)
        self._emit(line)

    def _emit(self, line: str) -> None:
        text = _bounded(line)
        if not text:
            return
        self.speech = text
        if self.on_speech is not None:
            try:
                self.on_speech(text)
            except Exception:  # noqa: BLE001 - a view bug never propagates
                pass


__all__ = [
    "CODING_TEXT_SUFFIXES",
    "MAX_SPEECH_CHARS",
    "STATE_LINES",
    "TaskEventAdapter",
    "action_state",
]
