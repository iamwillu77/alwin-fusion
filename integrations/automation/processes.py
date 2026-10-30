"""Read-only process observation (v0.4).

Distinguishes "installed but not running" from "running" by reading
``/proc`` directly.  Nothing here ever *starts* a process: there is no
process-starting module, no shell, no launch — only ``open()`` on the
per-process ``comm`` files, which are world-readable kernel state.

A fake ``/proc`` root can be injected for tests (a temporary directory
with ``<pid>/comm`` files), so automated tests never inspect the real
desktop's processes either.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional, Sequence

#: Default kernel process directory (Linux).
DEFAULT_PROC_ROOT = "/proc"

#: The kernel caps ``/proc/<pid>/comm`` at 15 characters; a longer
#: executable name is silently truncated there ('gnome-text-editor'
#: appears as 'gnome-text-edit').
KERNEL_COMM_CHARS = 15


def running_comm_names(proc_root: str = DEFAULT_PROC_ROOT) -> frozenset:
    """Names (``/proc/<pid>/comm``) of every process currently running.

    Returns an empty set when the directory cannot be read: an unknown
    process state is reported as "not observed", never as "running".
    """
    names = set()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return frozenset()
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(
                os.path.join(proc_root, entry, "comm"), "r", encoding="utf-8"
            ) as handle:
                name = handle.read(256).strip()
        except OSError:
            continue  # process exited while we looked — ignore
        if name:
            names.add(name)
    return frozenset(names)


def any_running(
    executables: Sequence[str],
    *,
    proc_root: str = DEFAULT_PROC_ROOT,
    comm_names: Optional[Iterable[str]] = None,
) -> Optional[bool]:
    """Is any of *executables* running?

    Returns ``True``/``False`` when the probe worked, or ``None`` when
    ``/proc`` could not be read at all (unknown state — callers must
    not claim "running" from an unreadable probe).
    """
    names = frozenset(comm_names) if comm_names is not None else (
        running_comm_names(proc_root)
    )
    if comm_names is None:
        try:
            os.listdir(proc_root)
        except OSError:
            return None
    wanted = [e for e in executables if e]
    if not wanted:
        return False
    for name in names:
        lowered = name.lower()
        for exe in wanted:
            token = exe.lower()
            # 'soffice' matches comm 'soffice.bin'; an exact match
            # always counts.  Truncation-safe in BOTH directions (real
            # bug found by the v0.13 live verification): the kernel caps
            # comm at 15 characters, so a long executable name appears
            # truncated ('gnome-text-editor' -> comm 'gnome-text-edit').
            # A comm shorter than the executable counts only when it is
            # exactly at the kernel cap (so an unrelated short comm like
            # 'bash' can never match 'bashful-app').
            if (
                lowered == token
                or lowered.startswith(token)
                or (len(lowered) == KERNEL_COMM_CHARS and token.startswith(lowered))
            ):
                return True
    return False
