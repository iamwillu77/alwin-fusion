"""Document and spreadsheet integration (Alwin Fusion v0.2).

This package turns *validated* structured content into files on disk:

* ``report.py``  — deterministic plain-text report rendering (stdlib),
* ``pdf.py``     — PDF rendering through reportlab (optional, lazy),
* ``spreadsheet.py`` — .xlsx building/inspection through openpyxl
  (optional, lazy),
* ``tools.py``   — the ``ToolResult``-returning handlers the executor
  dispatches to.

Design rules, mirroring ``integrations.filesystem``:

* every handler re-resolves its path through the permission checker
  (defence in depth) and re-validates its content against
  ``ai_core.specs`` before rendering,
* files are created with ``O_EXCL`` — an existing file is never
  overwritten, even in a race,
* the optional dependencies are imported lazily, so the core keeps
  working (with a clear error message) when they are missing.
"""

from __future__ import annotations

from integrations.documents.tools import (
    DocumentTools,
    MissingDependencyError,
    make_doc_handlers,
)

__all__ = ["DocumentTools", "MissingDependencyError", "make_doc_handlers"]
