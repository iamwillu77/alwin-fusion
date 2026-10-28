"""ALWIN FUSION task experience (v0.5).

Layering (top to bottom) — each layer talks only to the one below it:

* ``taskui.gtk_app``  — thin GTK4 view (renders presenter data, forwards
  user intents; executes nothing itself),
* ``taskui.presenter`` — pure, headless view-model (fully unit-tested),
* ``taskui.manager``   — task/session management: one worker thread per
  task, driving the *existing* trusted pipeline,
* ``taskui.lifecycle``— validated task state machine,
* ``taskui.history``   — persistent, validated, display-only task history,
* ``taskui.status``    — honest capability probes (AI, automation).

Everything that actually plans, permission-checks, approves, executes,
verifies and audits lives in ``ai_core``/``integrations`` — this package
adds no second execution engine and no second permission system.
"""

from taskui.lifecycle import (
    ALL_STATES,
    TERMINAL_STATES,
    InvalidTransition,
    TaskRecord,
)
from taskui.history import HistoryError, TaskHistory
from taskui.manager import ManagerError, TaskManager
from taskui.status import (
    AutomationStatus,
    AIStatus,
    SystemStatus,
    collect_status,
    probe_ai,
    probe_automation,
)

__all__ = [
    "AIStatus",
    "ALL_STATES",
    "AutomationStatus",
    "HistoryError",
    "InvalidTransition",
    "ManagerError",
    "SystemStatus",
    "TERMINAL_STATES",
    "TaskHistory",
    "TaskManager",
    "TaskRecord",
    "collect_status",
    "probe_ai",
    "probe_automation",
]
