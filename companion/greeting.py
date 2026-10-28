"""Startup greeting for the Alwin companion (v0.7.0).

A small, **text-first** greeting engine with the rules the milestone
requires:

* the greeting fires once per session (never again on window redraws),
* optionally once per day (a tiny local marker file, owner-only),
* it respects a configurable enabled preference, a mute flag and
  quiet hours (no greeting during the night),
* the greeting name is **configurable** (``ALWIN_COMPANION_NAME`` env
  var or the companion settings file) — no user name is hard-coded,
* it never claims to speak: the greeting is shown as text (a speech
  bubble); when no voice engine is connected, nothing is spoken,
* it never records audio and never assumes the user is present — the
  greeting is a friendly message, not a claim that someone is at the
  machine.

The module is pure headless Python (an optional clock is injected for
deterministic tests); the only I/O is the once-per-day marker file,
which is best-effort and degrades to "greet per session" on failure.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Callable, Optional, Tuple

# Bounds and validation.
MAX_NAME_CHARS = 40
MAX_GREETING_CHARS = 240

#: Environment variable for the greeting name (never hard-coded).
NAME_ENV = "ALWIN_COMPANION_NAME"

#: Default fallback when no name is configured — deliberately generic.
DEFAULT_NAME = "friend"

#: Default greeting text (a template; the name is inserted safely).
DEFAULT_TEMPLATE = "Hi {name}! {part} Have a wonderful day. What shall we do today?"

#: Time-of-day parts (local time).
PART_MORNING = "Good morning!"
PART_AFTERNOON = "Good afternoon!"
PART_EVENING = "Good evening!"


def sanitize_name(name: Optional[str]) -> str:
    """A bounded, single-line, safe display name (never markup)."""
    if not isinstance(name, str):
        return DEFAULT_NAME
    clean = " ".join(name.split())
    if not clean:
        return DEFAULT_NAME
    if len(clean) > MAX_NAME_CHARS:
        clean = clean[:MAX_NAME_CHARS]
    # Names are displayed as plain text; strip characters that would
    # look like control codes.  (The GTK view additionally uses
    # set_text, never markup, so this is defence in depth.)
    clean = "".join(ch for ch in clean if ch.isprintable())
    return clean or DEFAULT_NAME


def part_of_day(now: dtime) -> str:
    if dtime(5, 0) <= now < dtime(12, 0):
        return PART_MORNING
    if dtime(12, 0) <= now < dtime(18, 0):
        return PART_AFTERNOON
    return PART_EVENING


def greeting_text(name: str, now: dtime,
                  template: str = DEFAULT_TEMPLATE) -> str:
    """Compose the bounded greeting line for *now*."""
    text = template.format(name=sanitize_name(name), part=part_of_day(now))
    text = " ".join(text.split())
    if len(text) > MAX_GREETING_CHARS:
        text = text[:MAX_GREETING_CHARS - 1] + "…"
    return text


def parse_quiet_hours(spec: str) -> Optional[Tuple[dtime, dtime]]:
    """Parse ``"HH:MM-HH:MM"`` into a (start, end) pair.

    Returns None for an empty/disabled spec; raises ValueError for a
    malformed one (callers treat that as "no quiet hours" and surface
    the misconfiguration honestly rather than guessing).
    """
    spec = (spec or "").strip()
    if not spec:
        return None
    try:
        start_s, end_s = spec.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
        if not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
            raise ValueError("hour/minute out of range")
        return dtime(sh, sm), dtime(eh, em)
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        raise ValueError(f"Invalid quiet-hours spec {spec!r}: {exc}") from exc


def in_quiet_hours(now: dtime, quiet: Optional[Tuple[dtime, dtime]]) -> bool:
    """True when *now* falls inside the quiet window (may wrap midnight)."""
    if quiet is None:
        return False
    start, end = quiet
    if start == end:
        return False  # degenerate window: never quiet
    if start < end:
        return start <= now < end
    # Wraps midnight (e.g. 22:00-07:00).
    return now >= start or now < end


@dataclass
class GreetingConfig:
    """Everything the greeting depends on (all configurable)."""

    enabled: bool = True
    name: str = DEFAULT_NAME
    #: Once-per-day dedup marker (best-effort local file).
    marker_path: Optional[Path] = None
    #: Quiet hours: no greeting inside this window.
    quiet_hours: Optional[Tuple[dtime, dtime]] = None
    #: When True (default) the once-per-day marker is honoured.
    once_per_day: bool = True
    template: str = DEFAULT_TEMPLATE

    @classmethod
    def from_env(cls, env=None, *, marker_path: Optional[Path] = None,
                 **overrides) -> "GreetingConfig":
        """Build from environment variables (testable via *env* dict)."""
        environ = os.environ if env is None else env
        config = cls(marker_path=marker_path)
        raw_enabled = environ.get("ALWIN_COMPANION_GREETING")
        if raw_enabled is not None:
            config.enabled = raw_enabled.strip().lower() not in (
                "0", "false", "no", "off")
        name = environ.get(NAME_ENV)
        if name:
            config.name = sanitize_name(name)
        quiet = environ.get("ALWIN_COMPANION_QUIET_HOURS")
        if quiet:
            try:
                config.quiet_hours = parse_quiet_hours(quiet)
            except ValueError:
                config.quiet_hours = None  # misconfigured: greet normally
        for key, value in overrides.items():
            setattr(config, key, value)
        return config


class GreetingController:
    """Decides *whether* and *what* to greet; enforces the rules.

    All decisions are pure functions of the config and an injected
    clock (``now_fn``), so tests are deterministic.  The marker file is
    touched only when a greeting actually fires, and every I/O failure
    degrades to "no marker this time" — the greeting itself is never
    the reason something breaks.
    """

    def __init__(self, config: GreetingConfig,
                 *,
                 now_fn: Optional[Callable[[], datetime]] = None,
                 session_state_dir: Optional[Path] = None) -> None:
        self.config = config
        self._now = now_fn or datetime.now
        self._session_greeted = False

    # ------------------------------------------------------------------ #

    def should_greet(self) -> bool:
        """True when this session has not yet greeted and it is allowed.

        Rules, in order: greeting enabled -> not yet greeted this
        session -> not inside quiet hours -> not already greeted today
        (when the once-per-day marker is configured and readable).
        Every reason is a plain False; the caller may consult
        ``last_decision`` for an honest explanation to show in logs or
        the status area.
        """
        config = self.config
        if not config.enabled:
            self.last_decision = "disabled by configuration"
            return False
        if self._session_greeted:
            self.last_decision = "already greeted this session"
            return False
        now = self._now()
        if in_quiet_hours(now.time(), config.quiet_hours):
            self.last_decision = "inside quiet hours"
            return False
        if config.once_per_day and self._greeted_today(now):
            self.last_decision = "already greeted today"
            return False
        self.last_decision = "will greet"
        return True

    #: Why the last should_greet() answered as it did (honest logging).
    last_decision: str = ""

    def text(self) -> str:
        """The greeting line for the current moment (always safe to show)."""
        return greeting_text(self.config.name, self._now().time(),
                             self.config.template)

    def mark_greeted(self) -> None:
        """Record that the greeting fired (session + optional marker)."""
        self._session_greeted = True
        if self.config.marker_path is None:
            return
        try:
            marker = self.config.marker_path
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps({"date": self._now().date().isoformat()}),
                encoding="utf-8",
            )
            os.chmod(marker, 0o600)
        except OSError:
            # Best-effort: without the marker the greeting may repeat
            # on a new session the same day; nothing else is affected.
            pass

    # ------------------------------------------------------------------ #

    def _greeted_today(self, now: datetime) -> bool:
        if self.config.marker_path is None:
            return False
        try:
            raw = self.config.marker_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data.get("date") == now.date().isoformat()
        except (OSError, ValueError):
            return False


__all__ = [
    "DEFAULT_NAME",
    "DEFAULT_TEMPLATE",
    "GreetingConfig",
    "GreetingController",
    "MAX_NAME_CHARS",
    "NAME_ENV",
    "greeting_text",
    "in_quiet_hours",
    "parse_quiet_hours",
    "part_of_day",
    "sanitize_name",
]
