"""Companion settings: small, validated, local (v0.7.0).

Persists the companion's preferences (greeting on/off, name, quiet
hours, mute, always-on-top, subtitles) to a tiny JSON file in the
state directory — the same directory the audit log and task history
use, so it inherits the owner-only layout (0600 file, 0700 dir).

Design rules:

* **stdlib only** (json/pathlib/os — no new dependencies),
* the schema is **closed**: unknown keys are ignored on read, missing
  keys fall back to defaults, and a type mismatch is corrected to the
  default rather than trusted (a tampered settings file can at worst
  change cosmetic preferences — it can never grant a permission,
  enable execution, or bypass approval; the trusted core does not read
  this file at all),
* the file is read once at startup and written on explicit save; the
  companion never watches or re-reads it in a loop,
* all writes are best-effort with honest surfaced errors (the window
  shows a note; nothing crashes).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

SETTINGS_FILENAME = "companion_settings.json"

#: The closed schema: key -> (type, default).
SCHEMA: Dict[str, tuple] = {
    "greeting_enabled": (bool, True),
    "greeting_name": (str, ""),
    "quiet_hours": (str, ""),        # "HH:MM-HH:MM", empty = disabled
    "mute_voice": (bool, True),      # voice engine absent anyway (honest)
    "always_on_top": (bool, False),  # opt-in; Wayland may ignore it
    "show_subtitles": (bool, True),  # accessible text alternative
}


class SettingsError(Exception):
    """Raised only for diagnosis; callers use load/save best-effort."""


def defaults() -> dict:
    return {key: default for key, (_type, default) in SCHEMA.items()}


def sanitize(raw: dict) -> dict:
    """Coerce *raw* onto the closed schema (unknown keys dropped)."""
    out = defaults()
    if not isinstance(raw, dict):
        return out
    for key, (expected, _default) in SCHEMA.items():
        if key not in raw:
            continue
        value = raw[key]
        if expected is bool:
            # Accept the common JSON encodings of a bool; anything else
            # falls back to the default (fail cosmetic, never trusted).
            if isinstance(value, bool):
                out[key] = value
            elif isinstance(value, str):
                out[key] = value.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(value, (int, float)):
                out[key] = bool(value)
        elif expected is str and isinstance(value, str):
            out[key] = value[:80]  # bounded
    return out


def load(path: Path) -> dict:
    """Load settings; a missing/corrupt/tampered file yields defaults."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults()
    return sanitize(raw)


def save(path: Path, settings: dict) -> None:
    """Write settings (0600); raises SettingsError with the real cause."""
    data = sanitize(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:
        raise SettingsError(f"Could not save companion settings: {exc}") from exc


def path_for(state_dir: Path) -> Path:
    return Path(state_dir) / SETTINGS_FILENAME


__all__ = [
    "SCHEMA",
    "SETTINGS_FILENAME",
    "SettingsError",
    "defaults",
    "load",
    "path_for",
    "sanitize",
    "save",
]
