"""Desktop-automation integration (v0.4).

Layout:

* :mod:`integrations.automation.base` — interfaces, states, capability
  constants (no I/O),
* :mod:`integrations.automation.processes` — read-only ``/proc`` probe,
* :mod:`integrations.automation.atspi_backend` — AT-SPI session (lazy
  ``gi`` import, bounded walks),
* :mod:`integrations.automation.libreoffice` — the Writer/Calc adapter,
* :mod:`integrations.automation.tools` — tool handlers bound to the
  dispatcher,
* :mod:`integrations.automation.sequence` — validated multi-step task
  execution,
* :mod:`integrations.automation.mcp` — future-MCP tool descriptors
  (manifest only; no server in this milestone).
"""

from __future__ import annotations

from integrations.automation.base import (
    APPLICATION_STATES,
    CAPABILITIES,
    AutomationAdapter,
    CapabilityUnavailable,
    ControlInfo,
    ControlLocator,
    ActionResult,
    AppSnapshot,
    UnavailableAdapter,
)
from integrations.automation.sequence import TaskSequenceRunner
from integrations.automation.tools import AutomationTools, make_automation_handlers

__all__ = [
    "APPLICATION_STATES",
    "CAPABILITIES",
    "AutomationAdapter",
    "AutomationTools",
    "ActionResult",
    "AppSnapshot",
    "CapabilityUnavailable",
    "ControlInfo",
    "ControlLocator",
    "TaskSequenceRunner",
    "UnavailableAdapter",
    "make_automation_handlers",
]
