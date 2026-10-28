"""Honest capability status for the task experience (v0.5).

Everything here is **read-only probing of local state** — no model is
started, no application is launched, nothing is installed, and no
cloud endpoint is ever contacted.  Each probe answers exactly one
question and reports "unknown/unavailable" honestly instead of
guessing:

* is the *local* Ollama runtime reachable, and is a model configured
  and installed?  (falls back to typed ``OllamaError`` details — the
  deterministic parser keeps working regardless),
* are the AT-SPI desktop-automation bindings importable?  (import
  check only — never a live session, so probing cannot touch the
  desktop),
* general offline posture: the core never needs the internet; this is
  stated as a fact about the design, not as a network probe (Alwin
  Fusion performs no external network operations at all).

All dependencies are injectable so tests run entirely on fakes.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ai_runtime.ollama_client import OllamaClient, OllamaError

#: Probe timeout for the local runtime (seconds) — short on purpose so
#: the UI can never hang on a wedged server.
AI_PROBE_TIMEOUT = 2.0

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN = "unknown"

AI_LABELS = {
    STATUS_OK: "Local AI ready",
    STATUS_DEGRADED: "Local AI partial",
    STATUS_UNAVAILABLE: "Local AI unavailable",
    STATUS_UNKNOWN: "Local AI unknown",
}


@dataclass
class AIStatus:
    """What we honestly know about the local Ollama runtime."""

    status: str = STATUS_UNKNOWN
    runtime_reachable: Optional[bool] = None  # None = not probed yet
    model_configured: str = ""
    model_installed: Optional[bool] = None
    detail: str = "Not probed yet."
    models: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return AI_LABELS.get(self.status, self.status)

    @property
    def can_use_llm(self) -> bool:
        """True only when the runtime is up *and* the model is there."""
        return bool(
            self.runtime_reachable
            and self.model_configured
            and self.model_installed
        )

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "label": self.label,
            "runtime_reachable": self.runtime_reachable,
            "model_configured": self.model_configured,
            "model_installed": self.model_installed,
            "detail": self.detail,
            "can_use_llm": self.can_use_llm,
        }


def probe_ai(
    base_url: str,
    model: str,
    *,
    timeout: float = AI_PROBE_TIMEOUT,
    client_factory: Optional[Callable[..., OllamaClient]] = None,
) -> AIStatus:
    """Probe the local Ollama runtime.  Maps every *expected* runtime
    failure to an honest status; never fabricates readiness.

    Distinguishes the honest failure modes the CLI already knows:
    runtime unreachable, runtime up but no model configured, model not
    installed.  When AI is unusable the deterministic parser still
    works — the status says so instead of pretending.  Unexpected bugs
    (programming errors) are deliberately *not* swallowed: they
    surface, because masking them as 'offline' would be a lie.
    """
    factory = client_factory or OllamaClient
    client = factory(base_url=base_url, model=model, timeout=timeout)
    result = AIStatus(model_configured=model or "")

    try:
        installed = client.list_models(timeout=timeout)
    except OllamaError as exc:
        result.runtime_reachable = False
        result.status = STATUS_UNAVAILABLE
        result.detail = (
            f"{exc} The built-in deterministic parser keeps working — "
            f"no AI is required for any supported command."
        )
        return result

    result.runtime_reachable = True
    result.models = sorted(installed)
    if not model:
        result.status = STATUS_DEGRADED
        result.detail = (
            "The local AI runtime is reachable, but no model is "
            "configured (set OLLAMA_MODEL). The deterministic parser "
            "is being used."
        )
        return result
    if any(m == model or m.split(":")[0] == model for m in installed):
        result.model_installed = True
        result.status = STATUS_OK
        result.detail = (
            f"Local model '{model}' is available for --llm proposals "
            f"(always re-validated by the trusted allowlist)."
        )
        return result
    result.model_installed = False
    result.status = STATUS_DEGRADED
    result.detail = (
        f"Model '{model}' is not installed "
        f"(installed: {', '.join(installed) or 'none'}). Install it "
        f"yourself with 'ollama pull {model}' if you want it — Alwin "
        f"Fusion never downloads models."
    )
    return result


#: Labels for the desktop-automation binding probe.
AUTOMATION_LABELS = {
    STATUS_OK: "Desktop automation available",
    STATUS_UNAVAILABLE: "Desktop automation unavailable",
    STATUS_UNKNOWN: "Desktop automation unknown",
}


@dataclass
class AutomationStatus:
    """Import-level availability of the AT-SPI automation backend."""

    status: str = STATUS_UNKNOWN
    bindings_importable: Optional[bool] = None
    detail: str = "Not probed yet."

    @property
    def label(self) -> str:
        return AUTOMATION_LABELS.get(self.status, self.status)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "label": self.label,
            "bindings_importable": self.bindings_importable,
            "detail": self.detail,
        }


def probe_automation(
    *,
    spec_finder: Callable[[str], Optional[object]] = None,
) -> AutomationStatus:
    """Check whether the AT-SPI bindings can be imported.

    Deliberately an *import* check only: it never opens a session, so
    status probing cannot inspect or influence the desktop.  Whether a
    particular application actually exposes its interface is answered
    honestly per task by ``inspect_application`` (v0.4 state machine),
    not guessed here.
    """
    finder = spec_finder or importlib.util.find_spec
    result = AutomationStatus()
    try:
        spec = finder("gi")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        result.bindings_importable = False
        result.status = STATUS_UNAVAILABLE
        result.detail = (
            "The 'gi' bindings (python3-gi + gir1.2-atspi-2.0) are not "
            "installed, so desktop automation is unavailable. Everything "
            "else keeps working; Alwin Fusion never installs packages."
        )
        return result
    result.bindings_importable = True
    result.status = STATUS_OK
    result.detail = (
        "AT-SPI bindings are present. Whether a specific application "
        "exposes its interface is reported per task (honestly, including "
        "'state unknown' when the desktop hides it)."
    )
    return result


OFFLINE_DETAIL = (
    "Alwin Fusion runs fully offline: no internet access is needed, "
    "and no external network request is ever made (the only network "
    "client in the codebase talks to a local Ollama server on this "
    "machine)."
)


@dataclass
class SystemStatus:
    """Everything the UI shows in its status area."""

    ai: AIStatus = field(default_factory=AIStatus)
    automation: AutomationStatus = field(default_factory=AutomationStatus)
    offline_detail: str = OFFLINE_DETAIL

    def as_dict(self) -> dict:
        return {
            "ai": self.ai.as_dict(),
            "automation": self.automation.as_dict(),
            "offline_detail": self.offline_detail,
        }


def collect_status(
    base_url: str,
    model: str,
    *,
    timeout: float = AI_PROBE_TIMEOUT,
    client_factory: Optional[Callable[..., OllamaClient]] = None,
    spec_finder: Callable[[str], Optional[object]] = None,
) -> SystemStatus:
    """One-shot collection of all status probes (never raises)."""
    return SystemStatus(
        ai=probe_ai(
            base_url,
            model,
            timeout=timeout,
            client_factory=client_factory,
        ),
        automation=probe_automation(spec_finder=spec_finder),
    )
