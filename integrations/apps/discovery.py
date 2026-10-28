"""Read-only discovery of installed desktop applications.

Detection uses only trusted, boring system interfaces:

* **PATH executables** — ``shutil.which`` over the bare executable
  names declared in the registry (no process is started),
* **desktop-entry metadata** — the *existence* of ``<id>.desktop``
  files in the fixed system and per-user applications directories.

Untrusted input, handled as such:

* desktop-entry files are never opened or executed — only their file
  names are used, and a file name is sanitized to a conservative
  character set before it is compared (nothing from the file's
  *contents* can influence detection),
* the directory listing is bounded (``MAX_DESKTOP_FILES``) and any
  filesystem error becomes an honest ``detection_failed`` signal
  instead of a crash or a silent guess,
* an application is reported available **only** when its launch
  executable is resolvable on PATH.  A name appearing in some
  user-supplied file never counts as evidence.

Discovery never installs, removes, updates or launches anything.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from integrations.apps.registry import IntegrationRegistry

#: Detection statuses (the four outcomes the milestone requires).
STATUS_AVAILABLE = "available"
STATUS_NOT_INSTALLED = "not_installed"
STATUS_UNSUPPORTED = "installed_but_unsupported"
STATUS_DETECTION_FAILED = "detection_failed"

#: Fixed system locations searched for desktop entries.  No other
#: directories are ever consulted (a hostile .desktop file elsewhere
#: on the disk cannot influence detection).
SYSTEM_DESKTOP_DIRS = ("/usr/share/applications", "/usr/local/share/applications")

#: Per-user location, derived from the (already validated) home dir.
USER_DESKTOP_SUBPATH = ".local/share/applications"

#: Bound on how many directory entries are considered.
MAX_DESKTOP_FILES = 5000

_DESKTOP_SUFFIX = ".desktop"
_SAFE_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

#: Desktop-entry ids of applications we can *see* but deliberately do
#: not control in this milestone ("installed but unsupported").
KNOWN_UNSUPPORTED_APPS: Dict[str, str] = {
    "libreoffice-impress": "LibreOffice Impress",
    "libreoffice-draw": "LibreOffice Draw",
    "libreoffice-base": "LibreOffice Base",
    "libreoffice-math": "LibreOffice Math",
    "atril": "Atril document viewer",
    "xreader": "Xreader document viewer",
    "zathura": "Zathura document viewer",
}


@dataclass(frozen=True)
class DetectionResult:
    """Detection outcome for one registered integration."""

    integration_id: str
    name: str
    status: str
    detail: str
    executables_found: Tuple[str, ...] = ()
    desktop_entries_found: Tuple[str, ...] = ()

    def as_dict(self) -> Dict:
        return {
            "id": self.integration_id,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "executables_found": list(self.executables_found),
            "desktop_entries_found": list(self.desktop_entries_found),
        }


def sanitize_desktop_id(raw: str) -> str:
    """Sanitize a desktop file *name* into a comparable identifier.

    Untrusted: the name comes from a directory listing.  Anything
    outside the conservative character set is replaced, and the result
    is length-capped, so no exotic file name can influence matching.
    """
    stem = raw[: -len(_DESKTOP_SUFFIX)] if raw.endswith(_DESKTOP_SUFFIX) else raw
    cleaned = "".join(c if c in _SAFE_ID_CHARS else "_" for c in stem)
    return cleaned[:128]


def default_desktop_dirs(home: Path) -> Tuple[Path, ...]:
    """The fixed search locations: system dirs + the user's dir."""
    return tuple(Path(d) for d in SYSTEM_DESKTOP_DIRS) + (
        Path(home) / USER_DESKTOP_SUBPATH,
    )


def list_desktop_ids(desktop_dirs: Sequence[Path]) -> Tuple[Set[str], List[str]]:
    """List desktop-entry ids found in *desktop_dirs* (names only).

    Returns ``(ids, errors)``.  Reads directory listings only — never
    file contents — and bounds the number of considered entries.
    """
    ids: Set[str] = set()
    errors: List[str] = []
    for directory in desktop_dirs:
        try:
            with os.scandir(directory) as entries:
                count = 0
                for entry in entries:
                    count += 1
                    if count > MAX_DESKTOP_FILES:
                        errors.append(
                            f"{directory}: more than {MAX_DESKTOP_FILES} entries; "
                            f"the rest were ignored."
                        )
                        break
                    name = entry.name
                    if not name.endswith(_DESKTOP_SUFFIX):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=True):
                            continue
                    except OSError:
                        continue
                    desktop_id = sanitize_desktop_id(name)
                    if desktop_id:
                        ids.add(desktop_id)
        except FileNotFoundError:
            continue  # a missing search directory simply contributes nothing
        except OSError as exc:
            errors.append(f"{directory}: cannot list ({exc.strerror or exc}).")
    return ids, errors


def _resolve_executable(name: str, which: Callable[[str], Optional[str]]) -> Optional[str]:
    try:
        found = which(name)
    except Exception:  # noqa: BLE001 - a broken which() must not crash detection
        return None
    return found if isinstance(found, str) and found else None


def detect_integrations(
    registry: IntegrationRegistry,
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
    desktop_ids: Optional[Set[str]] = None,
) -> List[DetectionResult]:
    """Detect every registered integration (read-only).

    Status rules:

    * ``available`` — a launch executable resolves on PATH,
    * ``detection_failed`` — a desktop entry exists but no executable
      resolves (a broken/partial install is reported honestly, never
      as available),
    * ``not_installed`` — no signal at all.
    """
    results: List[DetectionResult] = []
    for definition in registry.definitions:
        found_executables: List[str] = []
        for executable in definition.detection.executables:
            resolved = _resolve_executable(executable, which)
            if resolved is not None and resolved not in found_executables:
                found_executables.append(resolved)

        found_entries = tuple(
            entry
            for entry in definition.detection.desktop_entries
            if desktop_ids is not None and entry in desktop_ids
        )

        if found_executables:
            results.append(
                DetectionResult(
                    integration_id=definition.id,
                    name=definition.name,
                    status=STATUS_AVAILABLE,
                    detail=f"executable found: {found_executables[0]}",
                    executables_found=tuple(found_executables),
                    desktop_entries_found=found_entries,
                )
            )
        elif found_entries:
            results.append(
                DetectionResult(
                    integration_id=definition.id,
                    name=definition.name,
                    status=STATUS_DETECTION_FAILED,
                    detail=(
                        f"desktop entry '{found_entries[0]}' exists but no launch "
                        f"executable "
                        f"({' or '.join(definition.detection.executables)}) is on "
                        f"PATH; not reported as available."
                    ),
                    desktop_entries_found=found_entries,
                )
            )
        else:
            results.append(
                DetectionResult(
                    integration_id=definition.id,
                    name=definition.name,
                    status=STATUS_NOT_INSTALLED,
                    detail=(
                        "no executable on PATH and no desktop entry found; "
                        "Alwin Fusion never installs applications automatically."
                    ),
                )
            )
    return results


def find_unsupported_apps(desktop_ids: Set[str]) -> List[Dict]:
    """Known installed applications that have no integration (yet)."""
    found = []
    for desktop_id, display_name in sorted(KNOWN_UNSUPPORTED_APPS.items()):
        if desktop_id in desktop_ids:
            found.append(
                {
                    "name": display_name,
                    "status": STATUS_UNSUPPORTED,
                    "detail": "detected via desktop entry; no integration exists yet.",
                }
            )
    return found


def detect_report(
    registry: IntegrationRegistry,
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
    desktop_dirs: Sequence[Path],
) -> Dict:
    """Full detection report for the dispatcher/CLI.

    Returns ``{"integrations": [...], "unsupported": [...], "errors":
    [...]}`` — statuses per registered integration, known-but-
    unsupported applications, and any detection errors (surfaced, never
    swallowed).
    """
    desktop_ids, errors = list_desktop_ids(desktop_dirs)
    integrations = [
        r.as_dict()
        for r in detect_integrations(registry, which=which, desktop_ids=desktop_ids)
    ]
    unsupported = find_unsupported_apps(desktop_ids)
    return {
        "integrations": integrations,
        "unsupported": unsupported,
        "errors": errors,
    }
