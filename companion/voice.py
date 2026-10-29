"""Voice interface for the Alwin companion (v0.9.0) — a clean seam for the future.

This module defines a **small, replaceable interface** that a future
local text-to-speech and speech-recognition engine can implement.

What exists today:

* :class:`VoiceEngine` — the abstract interface (speak/stop/status),
* :class:`NullVoice` — the honest "no engine connected" implementation:
  it does not synthesize sound, does not pretend to speak, and reports
  ``available = False`` with a clear status line for the UI,
* :func:`voice_status` — a pure, bounded view-model helper for the
  status pill.

What deliberately does NOT exist (and will not be faked):

* no fake voice engine that pretends to talk,
* no audio playback of pre-recorded clips,
* no microphone capture, no continuous listening, no wake word,
* no model installation, no cloud service, no large background process.

When a real engine is connected in a future milestone, it will
implement :class:`VoiceEngine` and be passed to the companion window;
everything else (states, adapter, greeting, speech content) stays
unchanged.  Until then the companion is honest: it speaks through text
bubbles only, and the status line says so.
"""

from __future__ import annotations

import abc
from typing import Optional

# Bounds (speech content is curated elsewhere; this is a final guard).
MAX_STATUS_CHARS = 200


class VoiceEngine(abc.ABC):
    """Abstract text-to-speech interface for a future local engine."""

    #: Short human name for the status line ("none", "piper", ...).
    name: str = "none"

    @property
    @abc.abstractmethod
    def available(self) -> bool:
        """Honest capability flag: False = text-only presentation."""

    @property
    @abc.abstractmethod
    def status(self) -> str:
        """One bounded line describing the voice situation."""

    @abc.abstractmethod
    def speak(self, text: str) -> bool:
        """Speak *text*; returns True only if it was actually spoken."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop any current speech (no-op when nothing is speaking)."""


class NullVoice(VoiceEngine):
    """The honest "no engine connected" voice.

    It never plays a sound and never claims to have spoken: ``speak``
    always returns False.  The UI therefore keeps showing the text
    bubble as the primary channel, which is the truth.
    """

    name = "none"

    @property
    def available(self) -> bool:
        return False

    @property
    def status(self) -> str:
        return (
            "Voice: off — no speech engine is connected. "
            "Alwin speaks through text bubbles only."
        )

    def speak(self, text: str) -> bool:
        # Honest: nothing is spoken, so nothing is claimed.
        return False

    def stop(self) -> None:
        return None


def voice_status(voice: Optional[VoiceEngine]) -> dict:
    """Bounded, pure status view for the companion's status area."""
    if voice is None or not voice.available:
        status = (voice.status if voice is not None
                  else NullVoice().status)
        return {
            "available": False,
            "label": "Voice off",
            "detail": status[:MAX_STATUS_CHARS],
        }
    return {
        "available": True,
        "label": f"Voice on ({voice.name})",
        "detail": voice.status[:MAX_STATUS_CHARS],
    }


__all__ = [
    "NullVoice",
    "VoiceEngine",
    "voice_status",
]
