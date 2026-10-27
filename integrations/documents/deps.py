"""Lazy access to the optional document dependencies.

``openpyxl`` and ``reportlab`` are *not* part of the trusted core: they
are imported only when a document action actually runs, and a missing
package becomes a clean, actionable error instead of an import crash
that would take the whole core down.
"""

from __future__ import annotations

import importlib

#: How the user may install each optional dependency (reported in the
#: error message; nothing is ever installed automatically).
INSTALL_HINTS = {
    "openpyxl": "python3 -m pip install --user openpyxl",
    "reportlab": "python3 -m pip install --user reportlab",
}


class MissingDependencyError(Exception):
    """An optional document library is not installed."""

    def __init__(self, package: str) -> None:
        self.package = package
        hint = INSTALL_HINTS.get(package, f"python3 -m pip install --user {package}")
        super().__init__(
            f"The '{package}' package is required for this operation but is "
            f"not installed. Install it yourself (with your own approval) "
            f"via: {hint}"
        )


def require(module_name: str):
    """Import *module_name* or raise :class:`MissingDependencyError`."""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise MissingDependencyError(module_name) from exc
