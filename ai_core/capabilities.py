"""Capability discovery for plan-time honesty (v0.12.0).

Answers one question before any planning happens: *what can this
installation actually do right now?*  Every answer is a probe of real
state (imports, registries, attached services) — never an assumption,
never an invented tool name.

* ``discover_capabilities`` — a small, JSON-able snapshot:
  the closed executable action set, optional document dependencies
  (openpyxl/reportlab), registered application ids, AT-SPI bindings
  presence (import-check only, same honesty rule as ``taskui.status``),
  attached LLM availability, and the MCP manifest's real status
  (**descriptors only — no MCP server or Blender tool exists**, and
  this module says so rather than pretending),
* ``CapabilityCache`` — memoizes a snapshot for a short TTL so plan
  validation does not re-import-probe on every step; ``invalidate()``
  drops it when integrations may have changed.  TTL = 0 disables
  caching; probes are cheap but not free.

Discovery output feeds (a) the planning prompt (the model may only
propose what is listed), (b) plan validation hints, and (c) honest
"capability unavailable" reporting.  Nothing here executes anything.
"""

from __future__ import annotations

import importlib.util
import time
from typing import Any, Callable, Dict, Optional

from ai_core.planner import ACTION_SCHEMA
from integrations.apps.registry import BUILTIN_REGISTRY

#: How long a snapshot stays fresh (seconds).
DEFAULT_CAPABILITY_TTL = 30.0

#: Bound for the registered-application id list we show/prompt with.
MAX_APP_IDS = 40


def _module_available(name: str,
                      finder: Callable[[str], Any]) -> bool:
    try:
        return finder(name) is not None
    except (ImportError, ValueError):   # finder may raise on odd names
        return False


def _atspi_bindings_present(finder: Callable[[str], Any]) -> bool:
    """Import-availability only — never opens a desktop session."""
    return _module_available("gi", finder)


def discover_capabilities(
    *,
    llm_available: bool = False,
    spec_finder: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Probe the real installation and return a JSON-able snapshot."""
    finder = spec_finder or importlib.util.find_spec

    actions = sorted(ACTION_SCHEMA)
    documents = {
        "create_spreadsheet": _module_available("openpyxl", finder),
        "create_pdf": _module_available("reportlab", finder),
    }
    app_ids = [str(a) for a in getattr(BUILTIN_REGISTRY, "ids", ())]

    # MCP truth: the manifest itself declares "descriptors only".
    mcp_status = "descriptors_only_no_server"
    mcp_descriptors = 0
    try:
        from integrations.automation.mcp import MCP_STATUS, mcp_manifest

        mcp_status = MCP_STATUS
        mcp_descriptors = len(mcp_manifest().get("tools") or [])
    except Exception:  # noqa: BLE001 - manifest is optional metadata
        pass

    return {
        "actions": actions,
        "documents": documents,
        "applications": app_ids[:MAX_APP_IDS],
        "applications_total": len(app_ids),
        "automation_bindings": _atspi_bindings_present(finder),
        "llm": bool(llm_available),
        "mcp": {
            "status": mcp_status,
            # No transport exists, so NOTHING is executable *via MCP*
            # (the underlying automation actions are plain pipeline
            # actions, discoverable there instead).
            "executable_tools": 0,
            "declared_descriptors": mcp_descriptors,
            "note": ("descriptors only: no MCP server or transport "
                     "exists, so no MCP/Blender tool can be planned "
                     "or executed"),
        },
        "discovered_at": time.time(),
    }


def missing_for(action: str, caps: Dict[str, Any]) -> Optional[str]:
    """Return an honest reason when *action* cannot run, else None."""
    if action not in caps.get("actions", []):
        return f"'{action}' is not an allowed action in this version."
    if action == "create_spreadsheet" and not caps["documents"].get(
            "create_spreadsheet"):
        return ("create_spreadsheet needs the 'openpyxl' package "
                "(python3 -m pip install --user openpyxl).")
    if action == "create_pdf" and not caps["documents"].get("create_pdf"):
        return "create_pdf needs the 'reportlab' package (pip install reportlab)."
    if action in ("inspect_application", "find_control", "read_control",
                  "activate_control", "enter_text", "save_document") \
            and not caps.get("automation_bindings"):
        return ("desktop automation needs the python3-gi + "
                "gir1.2-atspi-2.0 bindings; they are not installed.")
    return None


class CapabilityCache:
    """TTL cache around :func:`discover_capabilities` (invalidate on
    integration changes; TTL=0 disables)."""

    def __init__(self, ttl: float = DEFAULT_CAPABILITY_TTL,
                 clock: Optional[Callable[[], float]] = None,
                 **probe_kwargs: Any) -> None:
        self.ttl = float(ttl)
        self._clock = clock or time.monotonic
        self._probe_kwargs = probe_kwargs
        self._snapshot: Optional[Dict[str, Any]] = None
        self._fresh_at = float("-inf")

    def get(self) -> Dict[str, Any]:
        now = self._clock()
        if (self._snapshot is not None and self.ttl > 0
                and now - self._fresh_at < self.ttl):
            return self._snapshot
        self._snapshot = discover_capabilities(**self._probe_kwargs)
        self._fresh_at = now
        return self._snapshot

    def invalidate(self) -> None:
        self._snapshot = None
        self._fresh_at = float("-inf")


__all__ = [
    "DEFAULT_CAPABILITY_TTL",
    "CapabilityCache",
    "discover_capabilities",
    "missing_for",
]
