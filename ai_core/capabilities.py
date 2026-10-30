"""Capability discovery for plan-time honesty (v0.13.0).

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
import shutil
import time
from typing import Any, Callable, Dict, Optional, Sequence

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


def _executable_present(definition,
                        which: Callable[[str], Optional[str]]) -> bool:
    for candidate in (
        definition.launch.executable,
        definition.launch.fallback_executable,
    ):
        if not candidate:
            continue
        try:
            found = which(candidate)
        except Exception:  # noqa: BLE001 - a broken which() means unknown
            found = None
        if isinstance(found, str) and found:
            return True
    return False


def application_tiers(
    *,
    which: Optional[Callable[[str], Optional[str]]] = None,
    automation_bindings: bool,
    registry=BUILTIN_REGISTRY,
) -> Dict[str, Dict[str, Any]]:
    """Per-application control tiers, derived from evidence (v0.13).

    The five distinctions the milestone requires — never claimed,
    always derived:

    * ``discover`` — the id exists in the reviewed registry (the
      discovery probe is ``detect_applications``),
    * ``launch`` — can be started *without* a file (bare argv) and its
      launch executable resolves on PATH right now,
    * ``inspect`` — its running state can be observed via ``/proc``
      (every registered application qualifies),
    * ``interact`` — may be driven through accessibility APIs (the
      reviewed ``automate`` capability AND the bindings are present),
    * everything else is **open-only for control**: it may be opened
      and left for the user to drive.  Launching never implies
      controllability.

    ``window_state`` (a11y window metadata) is added when the bindings
    exist and the definition names its accessibility-bus identity;
    whether a specific window is observable is runtime evidence
    reported by ``application_status``, not a plan-time claim.
    """
    finder = which if which is not None else shutil.which
    out: Dict[str, Dict[str, Any]] = {}
    for definition in registry.definitions:
        installed = _executable_present(definition, finder)
        tiers = ["discover"]
        if definition.launches_bare and installed:
            tiers.append("launch")
        if definition.opens_files and installed:
            tiers.append("open_file")
        # /proc process observation works for every registered app.
        tiers.append("inspect")
        if automation_bindings and definition.a11y_names \
                and installed:
            # v0.19: window metadata requires the app to actually BE
            # installable/present — bindings + registry names alone
            # proved nothing about a specific application.
            tiers.append("window_state")
        if automation_bindings and installed \
                and "automate" in definition.capabilities:
            # v0.19: 'interact' is a PLAN-TIME permission fact only.
            # It must never be derived from installation alone in the
            # other direction either — and the RUNTIME truth (the app
            # may be installed yet blocked from the accessibility bus,
            # exactly like LibreOffice on the reference host) comes
            # from application_readiness() below / application_status.
            tiers.append("interact")
        out[definition.id] = {
            "tiers": tuple(tiers),
            "installed": installed,
            "control": (
                "interact" if "interact" in tiers else "open_only"
            ),
        }
    return out


#: Facet statuses (v0.19): the four honest answers, reused everywhere.
FACET_AVAILABLE = "available"
FACET_UNAVAILABLE = "unavailable"
FACET_BLOCKED = "blocked"
FACET_UNKNOWN = "unknown"
FACET_STATUSES = (FACET_AVAILABLE, FACET_UNAVAILABLE, FACET_BLOCKED,
                  FACET_UNKNOWN)


def application_readiness(
    definition,
    *,
    bindings: bool = True,
    which: Optional[Callable[[str], Optional[str]]] = None,
    running_probe: Optional[Callable[[dict], Optional[bool]]] = None,
    bus_probe: Optional[Callable[[dict], Sequence[str]]] = None,
) -> Dict[str, Any]:
    """Structured, evidence-based readiness for one registered app (v0.19).

    Three facets, each one of AVAILABLE / UNAVAILABLE / BLOCKED /
    UNKNOWN — never a guess:

    * ``installed`` — AVAILABLE/UNAVAILABLE from the executable probe;
      UNKNOWN when no probe is available or it raises (a transient
      probe failure is UNKNOWN, never permanent unavailability).
    * ``running`` — True/False/UNKNOWN from the injected /proc probe
      (None or exception => UNKNOWN).
    * ``a11y`` — AVAILABLE when the injected bus probe lists the
      application's reviewed accessibility identity; BLOCKED only on
      POSITIVE evidence (the app is RUNNING, the bindings exist, it
      declares an a11y identity, yet the bus does not list it —
      exactly the reference host's LibreOffice); UNKNOWN when the app
      is not running, no bindings exist, or the probe fails;
      UNAVAILABLE when the bindings themselves are absent.

    ``control`` derives: interact | open_only | unknown — an app that
    is installed but bus-blocked is NEVER 'interact'.  All probes are
    bounded by the caller (this function performs no I/O itself).
    """
    finder = which if which is not None else shutil.which

    # -- installed -------------------------------------------------- #
    try:
        # Call the finder DIRECTLY (not through the swallowing helper):
        # a probe that RAISES is a transient failure => UNKNOWN, never
        # permanent UNAVAILABLE.
        candidates = [
            getattr(definition.launch, "executable", ""),
            getattr(definition.launch, "fallback_executable", ""),
        ] + list(getattr(getattr(definition, "detection", None),
                         "executables", ()) or ())
        installed_present = False
        for candidate in candidates:
            if not candidate:
                continue
            if finder(candidate):
                installed_present = True
                break
        installed = (FACET_AVAILABLE if installed_present
                     else FACET_UNAVAILABLE)
    except Exception:  # noqa: BLE001 - probe failure is UNKNOWN
        installed_present = None
        installed = FACET_UNKNOWN

    # -- running ----------------------------------------------------- #
    running: Any = False
    if running_probe is not None:
        try:
            running = running_probe(definition.__dict__
                                    if hasattr(definition, "__dict__")
                                    else {})
            if running is None:
                running = None                  # unknown, not False
        except Exception:  # noqa: BLE001 - transient probe failure
            running = None
    else:
        running = None

    # -- a11y -------------------------------------------------------- #
    a11y_names = tuple(getattr(definition, "a11y_names", ()) or ())
    if not bindings:
        a11y = FACET_UNAVAILABLE
    elif bus_probe is None or not a11y_names or not installed_present:
        a11y = FACET_UNKNOWN
    else:
        try:
            listed = tuple(str(n).lower()
                           for n in (bus_probe(definition) or ()))
        except Exception:  # noqa: BLE001 - bus hiccup is UNKNOWN
            listed = None
        if listed is None:
            a11y = FACET_UNKNOWN
        elif any(name in listed for name in a11y_names):
            a11y = FACET_AVAILABLE
        elif running is True:
            # Positive evidence of a block: the process IS running,
            # the bindings exist, the app declares its bus identity —
            # and the bus does not list it.
            a11y = FACET_BLOCKED
        else:
            a11y = FACET_UNKNOWN

    # -- derived control ---------------------------------------------- #
    if a11y == FACET_AVAILABLE and "automate" in (
            getattr(definition, "capabilities", ()) or ()):
        control = "interact"
    elif a11y == FACET_BLOCKED:
        control = "open_only"
    else:
        control = "unknown"

    return {
        "installed": installed,
        "running": running,          # True | False | None (unknown)
        "a11y": a11y,
        "control": control,
        "tiers": tuple(
            t for t, keep in (
                ("discover", True),
                ("launch", bool(installed_present)
                 and getattr(definition, "launches_bare", False)),
                ("open_file", bool(installed_present)
                 and getattr(definition, "opens_files", False)),
                ("inspect", True),
                ("interact", control == "interact"),
            ) if keep
        ),
    }


def discover_capabilities(
    *,
    llm_available: bool = False,
    spec_finder: Optional[Callable[[str], Any]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Probe the real installation and return a JSON-able snapshot."""
    finder = spec_finder or importlib.util.find_spec

    actions = sorted(ACTION_SCHEMA)
    documents = {
        "create_spreadsheet": _module_available("openpyxl", finder),
        "create_pdf": _module_available("reportlab", finder),
    }
    app_ids = [str(a) for a in getattr(BUILTIN_REGISTRY, "ids", ())]
    bindings = _atspi_bindings_present(finder)
    tiers = application_tiers(which=which, automation_bindings=bindings)

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
        "application_tiers": {
            k: v for k, v in list(tiers.items())[:MAX_APP_IDS]
        },
        "automation_bindings": bindings,
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
                  "activate_control", "enter_text", "save_document",
                  "list_controls") \
            and not caps.get("automation_bindings"):
        return ("desktop automation needs the python3-gi + "
                "gir1.2-atspi-2.0 bindings; they are not installed.")
    if action in ("active_window", "focus_application", "close_application") \
            and not caps.get("automation_bindings"):
        return (f"'{action}' needs the accessibility bus (python3-gi + "
                "gir1.2-atspi-2.0); the bindings are not installed, so the "
                "operation cannot be performed or verified honestly.")
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
    "application_tiers",
    "discover_capabilities",
    "missing_for",
]
