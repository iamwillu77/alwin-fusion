"""Application integration engine (Alwin Fusion v0.3.0).

This package lets Alwin Fusion discover installed desktop
applications and open an approved, permission-checked file with a
supported application:

* ``registry.py``   — the trusted, closed registry of integrations
  (identifier, capabilities, detection/launch/verification strategies)
  with strict schema validation,
* ``discovery.py``  — read-only detection via PATH executables and
  desktop-entry metadata (nothing is launched, installed or updated),
* ``launch.py``     — the launch adapter: fixed argv templates,
  ``shell=False``, no user-controlled flags,
* ``tools.py``      — ``ToolResult``-returning handlers the executor
  dispatches to (permission re-checks, honest results).

Design rules, mirroring ``integrations.filesystem`` and
``integrations.documents``:

* an application is referenced only by a *registered identifier*;
  executables and command-line flags come from the reviewed builtin
  registry, never from a user, an LLM or a manifest,
* the file to open is resolved through the permission checker first
  (same traversal/symlink/roots rules as every other access) and must
  already exist — discovery and launching never create anything,
* launching requires explicit approval (``open_file`` is a mutating,
  approval-gated action), detection does not (it is read-only),
* a successful *launch* is reported as exactly that: the process was
  started.  Alwin Fusion never claims the application displayed,
  edited or recalculated anything.
"""

from __future__ import annotations

from integrations.apps.registry import (
    BUILTIN_REGISTRY,
    IntegrationDefinition,
    IntegrationRegistry,
    RegistryError,
    resolve_integration_id,
)
from integrations.apps.tools import ApplicationTools, make_app_handlers

__all__ = [
    "ApplicationTools",
    "BUILTIN_REGISTRY",
    "IntegrationDefinition",
    "IntegrationRegistry",
    "RegistryError",
    "make_app_handlers",
    "resolve_integration_id",
]
