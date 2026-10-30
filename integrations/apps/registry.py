"""Trusted registry of desktop-application integrations.

An integration definition is *data*, reviewed in this file.  Nothing
here imports code from a definition, runs commands, or reads files at
runtime: the engine resolves an application *identifier* against this
closed set, and the launch adapter builds an argv list from the
reviewed template.

An LLM, a user, or an untrusted manifest can never supply an
executable, a command-line flag or an import:

* :func:`validate_integration` enforces a closed schema (unknown keys
  rejected) with explicitly safe value patterns — executables are
  bare PATH names (no slashes, no absolute paths, no ``..``), launch
  arguments are literal tokens plus at most one ``{path}`` placeholder,
  and shell metacharacters are outside the accepted alphabet,
* there is deliberately **no** manifest loading from disk or from
  model output in this milestone — the builtin registry is the only
  source of definitions; validation exists so future, reviewed
  definitions get the same scrutiny.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Tuple

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class RegistryError(Exception):
    """An integration definition or identifier is invalid."""


def _err(field_name: str, message: str) -> RegistryError:
    return RegistryError(f"integration.{field_name}: {message}")


# --------------------------------------------------------------------------- #
# Accepted value patterns (the security alphabet of a definition)
# --------------------------------------------------------------------------- #

#: Stable identifier, e.g. ``libreoffice-calc``.
INTEGRATION_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
#: An alias is a user-facing word ('calc', 'document viewer').
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9 .-]{0,63}$")
#: A capability / permission word ('open_file').
WORD_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
#: Executables are *bare PATH names*: no slash means no absolute or
#: relative path can ever be smuggled in.
EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")
#: Launch arguments: a conservative literal alphabet without any shell
#: metacharacter (no ;|&$<>()'"`\\ newline ...).
ARGUMENT_RE = re.compile(r"^[A-Za-z0-9 ._:=,%+-]{1,120}$")
#: File extensions handled by an integration.
FILE_TYPE_RE = re.compile(r"^\.[a-z0-9]{1,8}$")
#: Integration semantic version.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
#: Desktop-entry file id (basename without .desktop), e.g. org.gnome.Evince.
DESKTOP_ENTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

#: The only argument placeholder that exists.
PATH_TOKEN = "{path}"

#: Capabilities an integration may declare.  Deliberately tiny; a
#: capability that does not exist here cannot be validated at all.
#: ``open_file`` = may be launched with a file (v0.3).  ``automate`` =
#: may be driven by the desktop-automation layer (v0.4); only reviewed
#: applications that genuinely expose accessibility information declare
#: it, and ``ai_core.permissions`` denies automation without it.
#: ``launch`` (v0.13) = may be started *without* a file and its state
#: observed (a "launch-only" integration: the user opens it and drives
#: it themselves).  Launching never implies controllability — the
#: control tiers are derived from evidence, not from this word.
KNOWN_CAPABILITIES = frozenset({"open_file", "automate", "launch"})

#: Permissions an integration may require.
KNOWN_REQUIRED_PERMISSIONS = frozenset({"open_file", "automate", "launch"})

#: Launch strategies.  Only argv-list execution exists; there is no
#: shell strategy and there never should be.
KNOWN_LAUNCH_STRATEGIES = frozenset({"exec"})

#: Verification strategies.  ``process_started`` confirms only that
#: the process was handed to the OS — never what the app displayed.
KNOWN_VERIFICATION_STRATEGIES = frozenset({"process_started"})

#: Integration kinds.
KNOWN_KINDS = frozenset({"application", "desktop_default"})

DEFINITION_KEYS = frozenset(
    {
        "id",
        "name",
        "version",
        "aliases",
        "capabilities",
        "file_types",
        "detection",
        "launch",
        "verification",
        "required_permissions",
        "kind",
        "a11y_names",
    }
)
DETECTION_KEYS = frozenset({"executables", "desktop_entries"})
LAUNCH_KEYS = frozenset({"strategy", "executable", "arguments", "fallback_executable"})
VERIFICATION_KEYS = frozenset({"strategy"})

MAX_NAME_CHARS = 100


# --------------------------------------------------------------------------- #
# Definition types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DetectionStrategy:
    """How to detect the application (read-only signals only)."""

    executables: Tuple[str, ...] = ()
    desktop_entries: Tuple[str, ...] = ()


@dataclass(frozen=True)
class LaunchStrategy:
    """Fixed argv template: ``[executable] + arguments`` with one {path}."""

    strategy: str
    executable: str
    arguments: Tuple[str, ...]
    fallback_executable: str = ""


@dataclass(frozen=True)
class VerificationStrategy:
    """What a successful launch may honestly be claimed to prove."""

    strategy: str


@dataclass(frozen=True)
class IntegrationDefinition:
    """One validated application integration."""

    id: str
    name: str
    version: str
    capabilities: Tuple[str, ...]
    file_types: Tuple[str, ...]
    detection: DetectionStrategy
    launch: LaunchStrategy
    verification: VerificationStrategy
    required_permissions: Tuple[str, ...]
    aliases: Tuple[str, ...] = ()
    kind: str = "application"
    #: Reviewed accessibility-bus application names used to find this
    #: application's windows (v0.13).  Empty means "derive the
    #: candidates from detection executables + desktop-entry ids".
    a11y_names: Tuple[str, ...] = ()

    def supports_file_type(self, suffix: str) -> bool:
        return suffix.lower() in self.file_types

    @property
    def opens_files(self) -> bool:
        """May this integration be launched *with* a file?"""
        return "open_file" in self.capabilities

    @property
    def launches_bare(self) -> bool:
        """May this integration be started *without* a file?"""
        return self.kind == "application" and (
            "launch" in self.capabilities or "open_file" in self.capabilities
        )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _require_keys(payload: dict, *, allowed: frozenset, required: frozenset, what: str):
    extra = set(payload) - allowed
    if extra:
        raise _err(what, f"unexpected fields: {', '.join(sorted(extra))}.")
    missing = required - set(payload)
    if missing:
        raise _err(what, f"missing fields: {', '.join(sorted(missing))}.")


def _string_tuple(value: object, *, field_name: str, pattern: re.Pattern,
                  max_items: int = 32) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise _err(field_name, f"must be a list of strings, got {type(value).__name__}.")
    if len(value) > max_items:
        raise _err(field_name, f"too many entries (limit {max_items}).")
    out = []
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise _err(field_name, f"{item!r} is not an accepted value.")
        out.append(item)
    return tuple(out)


def validate_integration(payload: object) -> IntegrationDefinition:
    """Validate one integration definition against the closed schema.

    Raises :class:`RegistryError` for anything unexpected — unknown
    keys, unsafe executables or arguments, unknown strategies.
    """
    if not isinstance(payload, dict):
        raise RegistryError(
            f"An integration definition must be a JSON object, got "
            f"{type(payload).__name__}."
        )
    _require_keys(
        payload,
        allowed=DEFINITION_KEYS,
        required=frozenset(
            {
                "id",
                "name",
                "version",
                "capabilities",
                "file_types",
                "detection",
                "launch",
                "verification",
                "required_permissions",
            }
        ),
        what="definition",
    )

    integration_id = payload.get("id")
    if not isinstance(integration_id, str) or not INTEGRATION_ID_RE.fullmatch(
        integration_id
    ):
        raise _err("id", f"{integration_id!r} is not a valid integration id.")

    name = payload.get("name")
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_NAME_CHARS
        or "\n" in name
    ):
        raise _err("name", f"{name!r} is not a valid application name.")

    version = payload.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise _err("version", f"{version!r} is not a MAJOR.MINOR.PATCH version.")

    kind = payload.get("kind", "application")
    if kind not in KNOWN_KINDS:
        raise _err("kind", f"{kind!r} is not one of {sorted(KNOWN_KINDS)}.")

    aliases = _string_tuple(
        payload.get("aliases", ()), field_name="aliases", pattern=ALIAS_RE
    )
    capabilities = _string_tuple(
        payload.get("capabilities"), field_name="capabilities", pattern=WORD_RE
    )
    for capability in capabilities:
        if capability not in KNOWN_CAPABILITIES:
            raise _err(
                "capabilities",
                f"unknown capability {capability!r}; known: "
                f"{', '.join(sorted(KNOWN_CAPABILITIES))}.",
            )
    if not capabilities:
        raise _err("capabilities", "an integration must declare at least one.")

    file_types = _string_tuple(
        payload.get("file_types"), field_name="file_types", pattern=FILE_TYPE_RE
    )
    if not file_types and "open_file" in capabilities:
        raise _err(
            "file_types",
            "an integration that opens files must support at least one file "
            "type (a launch-only integration declares no 'open_file' "
            "capability and may list none).",
        )
    if len(set(file_types)) != len(file_types):
        raise _err("file_types", "duplicate file types are not allowed.")

    required_permissions = _string_tuple(
        payload.get("required_permissions"),
        field_name="required_permissions",
        pattern=WORD_RE,
    )
    for permission in required_permissions:
        if permission not in KNOWN_REQUIRED_PERMISSIONS:
            raise _err(
                "required_permissions",
                f"unknown permission {permission!r}; known: "
                f"{', '.join(sorted(KNOWN_REQUIRED_PERMISSIONS))}.",
            )

    # ---- detection ---------------------------------------------------- #
    detection_raw = payload.get("detection")
    if not isinstance(detection_raw, dict):
        raise _err("detection", "must be an object.")
    _require_keys(
        detection_raw,
        allowed=DETECTION_KEYS,
        required=frozenset(),
        what="detection",
    )
    executables = _string_tuple(
        detection_raw.get("executables", ()),
        field_name="detection.executables",
        pattern=EXECUTABLE_RE,
    )
    desktop_entries = _string_tuple(
        detection_raw.get("desktop_entries", ()),
        field_name="detection.desktop_entries",
        pattern=DESKTOP_ENTRY_RE,
    )
    if not executables and not desktop_entries:
        raise _err("detection", "needs at least one executable or desktop entry.")
    detection = DetectionStrategy(
        executables=executables, desktop_entries=desktop_entries
    )

    # ---- launch ------------------------------------------------------- #
    launch_raw = payload.get("launch")
    if not isinstance(launch_raw, dict):
        raise _err("launch", "must be an object.")
    _require_keys(
        launch_raw,
        allowed=LAUNCH_KEYS,
        required=frozenset({"strategy", "executable", "arguments"}),
        what="launch",
    )
    strategy = launch_raw.get("strategy")
    if strategy not in KNOWN_LAUNCH_STRATEGIES:
        raise _err(
            "launch.strategy",
            f"{strategy!r} is not allowed; only "
            f"{', '.join(sorted(KNOWN_LAUNCH_STRATEGIES))} exists.",
        )
    executable = launch_raw.get("executable")
    if not isinstance(executable, str) or not EXECUTABLE_RE.fullmatch(executable):
        raise _err(
            "launch.executable",
            f"{executable!r} must be a bare executable name (no path, no slash).",
        )
    fallback = launch_raw.get("fallback_executable", "")
    if fallback and (
        not isinstance(fallback, str) or not EXECUTABLE_RE.fullmatch(fallback)
    ):
        raise _err(
            "launch.fallback_executable",
            f"{fallback!r} must be a bare executable name (no path, no slash).",
        )
    arguments = _string_tuple(
        launch_raw.get("arguments"),
        field_name="launch.arguments",
        pattern=re.compile(r"^(?:[A-Za-z0-9 ._:=,%+-]{1,120}|\{path\})$"),
        max_items=8,
    )
    if "open_file" in capabilities:
        if not arguments:
            raise _err("launch.arguments", "must contain at least one entry.")
        if arguments.count(PATH_TOKEN) != 1:
            raise _err(
                "launch.arguments",
                "must contain exactly one '{path}' placeholder (the file to "
                "open).",
            )
    elif arguments.count(PATH_TOKEN) != 0:
        # v0.13 launch-only integration (no 'open_file'): it is started
        # without a file, so the template must not reference one.
        raise _err(
            "launch.arguments",
            "a launch-only integration (no 'open_file' capability) must not "
            "contain a '{path}' placeholder.",
        )
    launch = LaunchStrategy(
        strategy=strategy,
        executable=executable,
        arguments=arguments,
        fallback_executable=fallback,
    )

    # ---- verification ------------------------------------------------- #
    verification_raw = payload.get("verification")
    if not isinstance(verification_raw, dict):
        raise _err("verification", "must be an object.")
    _require_keys(
        verification_raw,
        allowed=VERIFICATION_KEYS,
        required=frozenset({"strategy"}),
        what="verification",
    )
    verification_strategy = verification_raw.get("strategy")
    if verification_strategy not in KNOWN_VERIFICATION_STRATEGIES:
        raise _err(
            "verification.strategy",
            f"{verification_strategy!r} is not allowed; only "
            f"{', '.join(sorted(KNOWN_VERIFICATION_STRATEGIES))} exists.",
        )
    verification = VerificationStrategy(strategy=verification_strategy)

    # ---- accessibility-bus names (v0.13, optional) --------------------- #
    a11y_names = _string_tuple(
        payload.get("a11y_names", ()), field_name="a11y_names",
        pattern=EXECUTABLE_RE,
    )

    return IntegrationDefinition(
        id=integration_id,
        name=name,
        version=version,
        capabilities=capabilities,
        file_types=file_types,
        detection=detection,
        launch=launch,
        verification=verification,
        required_permissions=required_permissions,
        aliases=aliases,
        kind=kind,
        a11y_names=a11y_names,
    )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class IntegrationRegistry:
    """A closed, validated set of integrations."""

    def __init__(self, definitions: Iterable[IntegrationDefinition]) -> None:
        by_id: Dict[str, IntegrationDefinition] = {}
        for definition in definitions:
            if definition.id in by_id:
                raise RegistryError(
                    f"Duplicate integration id '{definition.id}'."
                )
            by_id[definition.id] = definition
        if not by_id:
            raise RegistryError("An integration registry must not be empty.")
        self._by_id = by_id

    @property
    def ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._by_id))

    @property
    def definitions(self) -> Tuple[IntegrationDefinition, ...]:
        return tuple(self._by_id[k] for k in sorted(self._by_id))

    def get(self, integration_id: str) -> IntegrationDefinition:
        try:
            return self._by_id[integration_id]
        except KeyError:
            raise RegistryError(
                f"Unknown application '{integration_id}'. Known: "
                f"{', '.join(self.ids)}."
            ) from None

    def __contains__(self, integration_id: object) -> bool:
        return integration_id in self._by_id


def build_registry(definitions: Iterable[object]) -> IntegrationRegistry:
    """Validate every definition and build a registry from them."""
    return IntegrationRegistry(validate_integration(d) for d in definitions)


def _normalize_app_token(value: str) -> str:
    """Canonical form of an application token: case- and separator-blind."""
    normalized = value.strip().strip("\"'").lower()
    normalized = re.sub(r"[\s_]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized.strip("-.'")


def resolve_integration_id(
    token: object, registry: IntegrationRegistry = None
) -> str:
    """Resolve a user- or model-supplied application token to an id.

    Accepts ids, application names and aliases, case- and
    separator-insensitively (``"LibreOffice Calc"``, ``"calc"`` ->
    ``"libreoffice-calc"``).  ``""``/``"all"``/``None`` mean "no
    specific application" and return ``""``.  Raises
    :class:`RegistryError` for unknown tokens — an unknown token is
    never guessed at and never executed.
    """
    registry = registry or BUILTIN_REGISTRY
    if token is None:
        return ""
    if not isinstance(token, str):
        raise RegistryError(
            f"The application must be a name, got {type(token).__name__}."
        )
    normalized = _normalize_app_token(token)
    if not normalized or normalized in ("all", "any"):
        return ""
    for definition in registry.definitions:
        candidates = {
            _normalize_app_token(definition.id),
            _normalize_app_token(definition.name),
        }
        candidates.update(_normalize_app_token(alias) for alias in definition.aliases)
        if normalized in candidates:
            return definition.id
    raise RegistryError(
        f"Unknown application '{token.strip()}'. Available: "
        f"{', '.join(registry.ids)}."
    )


# --------------------------------------------------------------------------- #
# The builtin registry — the only source of definitions (v0.3.0, v0.4.0)
#
# Reviewed, fixed mappings.  LibreOffice Writer/Calc plus two common
# PDF viewers and the desktop's file-association default.  Detection is
# read-only (PATH executables + desktop-entry ids); launch is argv-only
# with shell disabled by the adapter; verification claims only that the
# process was started.
#
# v0.4: Writer and Calc additionally declare the 'automate'
# capability — the permission stage denies every automation action for
# any integration that does not declare it.
# --------------------------------------------------------------------------- #

BUILTIN_INTEGRATIONS = (
    {
        "id": "libreoffice-writer",
        "name": "LibreOffice Writer",
        # v0.4: minor bump for the added 'automate' capability.
        "version": "1.1.0",
        "aliases": ("writer",),
        "capabilities": ("open_file", "automate"),
        "file_types": (".txt", ".pdf"),
        "detection": {
            "executables": ("soffice", "libreoffice"),
            "desktop_entries": ("libreoffice-writer",),
        },
        "launch": {
            "strategy": "exec",
            "executable": "soffice",
            "fallback_executable": "libreoffice",
            "arguments": ("--writer", PATH_TOKEN),
        },
        "verification": {"strategy": "process_started"},
        "required_permissions": ("open_file", "automate"),
    },
    {
        "id": "libreoffice-calc",
        "name": "LibreOffice Calc",
        # v0.4: minor bump for the added 'automate' capability.
        "version": "1.1.0",
        "aliases": ("calc",),
        "capabilities": ("open_file", "automate"),
        "file_types": (".xlsx",),
        "detection": {
            "executables": ("soffice", "libreoffice"),
            "desktop_entries": ("libreoffice-calc",),
        },
        "launch": {
            "strategy": "exec",
            "executable": "soffice",
            "fallback_executable": "libreoffice",
            "arguments": ("--calc", PATH_TOKEN),
        },
        "verification": {"strategy": "process_started"},
        "required_permissions": ("open_file", "automate"),
    },
    {
        "id": "evince",
        "name": "Document Viewer (Evince)",
        "version": "1.0.0",
        "aliases": ("evince", "document viewer"),
        "capabilities": ("open_file",),
        "file_types": (".pdf",),
        "detection": {
            "executables": ("evince",),
            "desktop_entries": ("org.gnome.Evince", "evince"),
        },
        "launch": {
            "strategy": "exec",
            "executable": "evince",
            "arguments": (PATH_TOKEN,),
        },
        "verification": {"strategy": "process_started"},
        "required_permissions": ("open_file",),
    },
    {
        "id": "okular",
        "name": "Okular",
        "version": "1.0.0",
        "aliases": ("okular",),
        "capabilities": ("open_file",),
        "file_types": (".pdf",),
        "detection": {
            "executables": ("okular",),
            "desktop_entries": ("org.kde.okular", "okular"),
        },
        "launch": {
            "strategy": "exec",
            "executable": "okular",
            "arguments": (PATH_TOKEN,),
        },
        "verification": {"strategy": "process_started"},
        "required_permissions": ("open_file",),
    },
    {
        "id": "libreoffice",
        "name": "LibreOffice",
        # v0.13: the suite's Start Center — the honest target for
        # "open LibreOffice" (bare launch); with a file it hands the
        # document to the right component (soffice <file>).
        "version": "1.0.0",
        "aliases": ("libreoffice", "start center", "libreoffice start center"),
        "capabilities": ("launch", "open_file"),
        "file_types": (".txt", ".csv", ".odt"),
        "detection": {
            "executables": ("soffice", "libreoffice"),
            "desktop_entries": ("libreoffice-startcenter",),
        },
        "launch": {
            "strategy": "exec",
            "executable": "soffice",
            "fallback_executable": "libreoffice",
            "arguments": (PATH_TOKEN,),
        },
        "verification": {"strategy": "process_started"},
        "required_permissions": ("open_file", "launch"),
        "a11y_names": ("libreoffice", "soffice.bin"),
    },
    {
        "id": "nautilus",
        "name": "Files (Nautilus)",
        # v0.13: launch-only (tier: open and leave for the user) —
        # it manages files itself, so Alwin never drives it.
        "version": "1.0.0",
        "aliases": ("files", "file manager", "nautilus"),
        "capabilities": ("launch",),
        "file_types": (),
        "detection": {
            "executables": ("nautilus",),
            "desktop_entries": ("org.gnome.Nautilus",),
        },
        "launch": {"strategy": "exec", "executable": "nautilus", "arguments": ()},
        "verification": {"strategy": "process_started"},
        "required_permissions": ("launch",),
        "a11y_names": ("nautilus",),
    },
    {
        "id": "gnome-text-editor",
        "name": "Text Editor",
        "version": "1.0.0",
        "aliases": ("text editor", "gnome text editor"),
        "capabilities": ("launch", "open_file"),
        "file_types": (".txt", ".md", ".csv"),
        "detection": {
            "executables": ("gnome-text-editor",),
            "desktop_entries": ("org.gnome.TextEditor",),
        },
        "launch": {
            "strategy": "exec",
            "executable": "gnome-text-editor",
            "arguments": (PATH_TOKEN,),
        },
        "verification": {"strategy": "process_started"},
        "required_permissions": ("open_file", "launch"),
        "a11y_names": ("gnome-text-editor",),
    },
    {
        "id": "default",
        "name": "Desktop default application",
        "version": "1.0.0",
        "aliases": ("system", "system default", "xdg-open", "file association"),
        "capabilities": ("open_file",),
        "file_types": (".txt", ".pdf", ".xlsx"),
        "detection": {"executables": ("xdg-open",), "desktop_entries": ()},
        "launch": {
            "strategy": "exec",
            "executable": "xdg-open",
            "arguments": (PATH_TOKEN,),
        },
        "verification": {"strategy": "process_started"},
        "required_permissions": ("open_file",),
        "kind": "desktop_default",
    },
)

#: The trusted registry used by the core (validated at import time).
BUILTIN_REGISTRY = build_registry(BUILTIN_INTEGRATIONS)
