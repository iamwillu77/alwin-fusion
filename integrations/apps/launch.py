"""Controlled application launching.

The adapter turns a *registered* integration plus an already
permission-checked file path into one process start:

* argv is built from the reviewed template in the registry:
  ``[resolved_executable] + arguments`` where the only dynamic token
  is the literal ``{path}`` replaced by the resolved file path —
  as a **separate argv element**, never concatenated into a command
  string,
* ``shell=False`` explicitly, ``close_fds=True``,
  ``start_new_session=True``, std streams on ``/dev/null`` — no
  shell ever interprets any part of the path or the flags,
* the executable is resolved through ``PATH`` at launch time and the
  resolved absolute path is executed (no relative-name ambiguity),
* the executable names and flags come exclusively from the trusted
  registry; a user, a file, or an LLM can choose an *integration id*
  but never an executable or a command-line option.

An honest result: :class:`LaunchOutcome` reports that the *process was
started* (or exited immediately).  It says nothing about what the
application then displayed, edited or computed.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from integrations.apps.registry import (
    PATH_TOKEN,
    IntegrationDefinition,
    RegistryError,
)


class LaunchError(Exception):
    """The application could not be started."""


@dataclass(frozen=True)
class LaunchOutcome:
    """What a launch attempt actually proved."""

    started: bool
    integration_id: str
    application_name: str
    executable: str
    pid: Optional[int]
    returncode: Optional[int]
    detail: str

    def as_dict(self) -> dict:
        return {
            "started": self.started,
            "integration": self.integration_id,
            "application": self.application_name,
            "executable": self.executable,
            "pid": self.pid,
            "returncode": self.returncode,
            "detail": self.detail,
        }


class LaunchAdapter:
    """Starts registered applications with argv-only launches.

    ``popen`` and ``which`` are injectable so tests never start a real
    process; the defaults use the standard library.
    """

    def __init__(
        self,
        *,
        popen: Callable = subprocess.Popen,
        which: Callable[[str], Optional[str]] = shutil.which,
    ) -> None:
        self._popen = popen
        self._which = which

    # ------------------------------------------------------------------ #

    def resolve_executable(self, definition: IntegrationDefinition) -> Optional[str]:
        """Absolute path of the launch executable, or ``None``."""
        for candidate in (
            definition.launch.executable,
            definition.launch.fallback_executable,
        ):
            if not candidate:
                continue
            try:
                found = self._which(candidate)
            except Exception:  # noqa: BLE001 - a broken which() means unavailable
                found = None
            if isinstance(found, str) and found:
                return found
        return None

    @staticmethod
    def build_argv(
        definition: IntegrationDefinition, resolved_executable: str, file_path: Path
    ) -> list:
        """Build the argv list from the reviewed template.

        Raises :class:`LaunchError` if the template is somehow unsafe
        (defence in depth — registry validation already guaranteed it).
        """
        argv = [resolved_executable]
        path_used = False
        for template in definition.launch.arguments:
            if template == PATH_TOKEN:
                argv.append(str(file_path))
                path_used = True
            else:
                argv.append(template)
        if not path_used:
            # A template without {path} would launch the app without the
            # file — refuse rather than do something surprising.
            raise LaunchError(
                f"The launch template for '{definition.id}' does not contain "
                f"the {{path}} placeholder; refusing to launch."
            )
        for element in argv:
            if not isinstance(element, str) or "\x00" in element:
                raise LaunchError("Refusing to launch: malformed argv element.")
        return argv

    # ------------------------------------------------------------------ #

    @staticmethod
    def build_argv_app(
        definition: IntegrationDefinition, resolved_executable: str
    ) -> list:
        """Build the argv for starting the application *without* a file (v0.13).

        The argv is the reviewed template with the ``{path}`` token
        dropped (flags are still registry literals).  A launch-only
        integration (no ``open_file`` capability) has a template
        without ``{path}`` and is used verbatim.  Raises
        :class:`LaunchError` on any malformed element.
        """
        if not definition.launches_bare:
            raise LaunchError(
                f"'{definition.id}' is not launchable without a file "
                f"(kind: {definition.kind})."
            )
        argv = [resolved_executable]
        for template in definition.launch.arguments:
            if template != PATH_TOKEN:
                argv.append(template)
        for element in argv:
            if not isinstance(element, str) or "\x00" in element:
                raise LaunchError("Refusing to launch: malformed argv element.")
        return argv

    def launch_app(self, definition: IntegrationDefinition) -> LaunchOutcome:
        """Start *definition*'s application with no file (v0.13).

        Same honesty contract as :meth:`launch`: reports only that a
        process was started (or exited immediately).  Nothing is ever
        installed, and no signal is ever sent to any process.
        """
        executable = self.resolve_executable(definition)
        if executable is None:
            wanted = " or ".join(
                n
                for n in (
                    definition.launch.executable,
                    definition.launch.fallback_executable,
                )
                if n
            )
            raise LaunchError(
                f"{definition.name} does not appear to be installed "
                f"(executable '{wanted}' not found on PATH). Alwin Fusion "
                f"never installs applications automatically."
            )

        argv = self.build_argv_app(definition, executable)

        try:
            process = self._popen(
                argv,
                shell=False,  # explicit: no part of this is ever a shell string
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise LaunchError(
                f"Failed to start {definition.name}: "
                f"{getattr(exc, 'strerror', None) or exc}"
            ) from exc

        returncode = process.poll()
        pid = getattr(process, "pid", None)
        if returncode is None:
            detail = (
                f"process started (pid {pid}) and still running; "
                f"Alwin Fusion does not control the application."
            )
        elif returncode == 0:
            detail = (
                f"process started and exited cleanly (status 0, pid {pid}); "
                f"the running instance or desktop session took over."
            )
        else:
            raise LaunchError(
                f"{definition.name} exited immediately with status "
                f"{returncode}; the application was not started."
            )
        return LaunchOutcome(
            started=True,
            integration_id=definition.id,
            application_name=definition.name,
            executable=executable,
            pid=pid,
            returncode=returncode,
            detail=detail,
        )

    def launch(self, definition: IntegrationDefinition, file_path: Path) -> LaunchOutcome:
        """Start *definition*'s application for *file_path*.

        Raises :class:`LaunchError` when the application is not
        installed, the process could not be started, or it exited
        immediately with an error status.  Nothing is ever installed.
        """
        executable = self.resolve_executable(definition)
        if executable is None:
            wanted = " or ".join(
                n
                for n in (
                    definition.launch.executable,
                    definition.launch.fallback_executable,
                )
                if n
            )
            raise LaunchError(
                f"{definition.name} does not appear to be installed "
                f"(executable '{wanted}' not found on PATH). Alwin Fusion "
                f"never installs applications automatically."
            )

        argv = self.build_argv(definition, executable, file_path)

        try:
            process = self._popen(
                argv,
                shell=False,  # explicit: no part of this is ever a shell string
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise LaunchError(
                f"Failed to start {definition.name}: "
                f"{getattr(exc, 'strerror', None) or exc}"
            ) from exc

        returncode = process.poll()
        pid = getattr(process, "pid", None)
        if returncode is None:
            detail = (
                f"process started (pid {pid}) and still running; "
                f"Alwin Fusion does not wait for or control the application."
            )
        elif returncode == 0:
            # Typical for xdg-open, which hands the file off and exits.
            detail = (
                f"process started and exited cleanly (status 0, pid {pid}); "
                f"the desktop handler took over."
            )
        else:
            raise LaunchError(
                f"{definition.name} exited immediately with status "
                f"{returncode}; the file was not opened."
            )
        return LaunchOutcome(
            started=True,
            integration_id=definition.id,
            application_name=definition.name,
            executable=executable,
            pid=pid,
            returncode=returncode,
            detail=detail,
        )
