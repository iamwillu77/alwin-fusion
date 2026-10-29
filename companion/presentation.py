"""Companion presentation: pure view-model for the character window (v0.9.1).

Turns the character controller's snapshot, the greeting, the voice
status and the minimal task status into small display-ready
dictionaries — deliberately **pure and headless** (no GTK, no threads,
no I/O), exactly like ``taskui.presenter`` for the task window, so the
whole companion behaviour is unit-testable without a display.

Trust rules enforced here:

* every rendered string is bounded and came from curated sources (the
  character state's fixed line, the adapter's sanitized speech, the
  sanitized greeting name, or the presenter's own labels),
* untrusted text never reaches the bubble: the adapter already bounds
  its lines, and this module bounds again (defence in depth) and never
  interprets text as markup,
* the minimal task status shows only: the task state label, the plan's
  one-line summary and the verification verdict — never file content,
  never raw error internals, never a plan digest,
* the "needs your approval" line is a *pointer* to the trusted task
  window; this presentation exposes no approve/reject affordance at
  all (approval lives only where the validated plan is shown).
"""

from __future__ import annotations

from typing import Dict, List, Optional

from companion.voice import voice_status, VoiceEngine

MAX_BUBBLE_CHARS = 200
MAX_STATUS_CHARS = 160
MAX_SUBTITLE_CHARS = 200

TONE_CALM = "calm"
TONE_ATTENTION = "attention"
TONE_POSITIVE = "positive"
TONE_NEGATIVE = "negative"

#: Presentation tone per character state (used for bubble styling).
STATE_TONES: Dict[str, str] = {
    "sleeping": TONE_CALM,
    "waking_up": TONE_CALM,
    "greeting": TONE_POSITIVE,
    "idle": TONE_CALM,
    "listening": TONE_CALM,
    "thinking": TONE_CALM,
    "walking": TONE_CALM,
    "writing": TONE_CALM,
    "coding": TONE_CALM,
    "reading": TONE_CALM,
    "waiting_for_approval": TONE_ATTENTION,
    "working": TONE_CALM,
    "verifying": TONE_CALM,
    "celebrating": TONE_POSITIVE,
    "error": TONE_NEGATIVE,
    "resting": TONE_CALM,
}


def _bounded(text, limit: int) -> str:
    """The only shape untrusted text may take before display."""
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def bubble_view(state: str, speech: str, *, tone: Optional[str] = None) -> dict:
    """The speech bubble: bounded text + tone (empty = no bubble)."""
    text = _bounded(speech, MAX_BUBBLE_CHARS)
    return {
        "text": text,
        "visible": bool(text),
        "tone": tone or STATE_TONES.get(state, TONE_CALM),
    }


def subtitle_view(snapshot: dict) -> str:
    """Accessible text alternative for the character (never colour-only)."""
    label = _bounded(snapshot.get("state_label", ""), MAX_SUBTITLE_CHARS)
    reason = _bounded(snapshot.get("reason", ""), MAX_SUBTITLE_CHARS)
    if reason and reason != label:
        return _bounded(f"Alwin is {label}.", MAX_SUBTITLE_CHARS)
    return _bounded(f"Alwin is {label}.", MAX_SUBTITLE_CHARS)


def task_status_view(record) -> dict:
    """Minimal, curated task status for the companion's status strip.

    Shows only the state label, the plan's one-line summary and the
    verification verdict.  ``record`` may be None (no task yet).
    """
    if record is None:
        return {"present": False, "label": "No task yet", "summary": "",
                "verification": "", "tone": TONE_CALM}
    plan = getattr(record, "plan", None) or {}
    verification = getattr(record, "verification", None) or {}
    vstatus = verification.get("status") if isinstance(verification, dict) else None
    vlabel = {"passed": "Verified", "failed": "Not verified"}.get(vstatus, "")
    state_label = _bounded(getattr(record, "state_label", "") or "", 60)
    summary = _bounded(plan.get("summary", "") or "", MAX_STATUS_CHARS)
    return {
        "present": True,
        "label": state_label,
        "summary": summary,
        "verification": vlabel,
        "tone": (TONE_POSITIVE if vstatus == "passed"
                 else TONE_NEGATIVE if vstatus == "failed"
                 else TONE_CALM),
    }


def controls_view(*, voice: Optional[VoiceEngine] = None,
                  always_on_top: bool = False,
                  greeting_enabled: bool = True) -> dict:
    """The companion's controls (plain descriptions; the view wires them).

    The approval control is deliberately absent: approval only exists
    in the trusted task window where the validated plan is shown.
    """
    voice_info = voice_status(voice)
    return {
        "controls": [
            {"id": "open_task_window", "label": "Open task window",
             "tooltip": "Open the ALWIN task interface (plan, approval, "
                        "history)."},
            {"id": "hide", "label": "Hide",
             "tooltip": "Hide the companion (it keeps running)."},
            {"id": "settings", "label": "Settings",
             "tooltip": "Companion settings (greeting, voice, always on "
                        "top)."},
        ],
        "voice": voice_info,
        "always_on_top": bool(always_on_top),
        "greeting_enabled": bool(greeting_enabled),
    }


def settings_view(settings: dict) -> dict:
    """Bounded, curated rendering of the persisted companion settings."""
    return {
        "greeting_enabled": bool(settings.get("greeting_enabled", True)),
        "greeting_name": _bounded(settings.get("greeting_name", ""), 40),
        "quiet_hours": _bounded(settings.get("quiet_hours", ""), 20),
        "always_on_top": bool(settings.get("always_on_top", False)),
        "show_subtitles": bool(settings.get("show_subtitles", True)),
        # v0.9 voice & language (all cosmetic; detection never overrides
        # a clear result, speech is opt-in).
        "mic_enabled": bool(settings.get("mic_enabled", True)),
        "language_auto": bool(settings.get("language_auto", True)),
        "language_preferred": _bounded(
            settings.get("language_preferred", ""), 8
        ),
        "spoken_responses": bool(settings.get("spoken_responses", False)),
    }


__all__ = [
    "MAX_BUBBLE_CHARS",
    "STATE_TONES",
    "TONE_ATTENTION",
    "TONE_CALM",
    "TONE_NEGATIVE",
    "TONE_POSITIVE",
    "bubble_view",
    "controls_view",
    "settings_view",
    "subtitle_view",
    "task_status_view",
]
