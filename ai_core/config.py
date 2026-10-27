"""Configuration for the Alwin Fusion local AI core.

All configuration comes from explicit arguments or environment
variables.  No cloud service, paid API or auto-download is involved.

Environment variables
---------------------
ALWIN_HOME               Override the home directory (mainly for tests).
ALWIN_PERMITTED_ROOTS    os.pathsep-separated list of permitted roots.
                         Relative entries are resolved against the home
                         directory.  Defaults to ``Documents``.
ALWIN_STATE_DIR          Directory for audit logs (defaults to
                         ``$XDG_STATE_HOME/alwin-fusion`` or
                         ``~/.local/state/alwin-fusion``).
OLLAMA_BASE_URL          Defaults to http://localhost:11434
OLLAMA_MODEL             Model name; empty by default.  A model is never
                         downloaded automatically.
OLLAMA_TIMEOUT           HTTP timeout in seconds (default 30).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Tuple, Union

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = ""
DEFAULT_OLLAMA_TIMEOUT = 30.0

HOME_ENV = "ALWIN_HOME"
ROOTS_ENV = "ALWIN_PERMITTED_ROOTS"
STATE_DIR_ENV = "ALWIN_STATE_DIR"


class ConfigError(Exception):
    """Raised when configuration values are invalid or unsafe."""


PathLike = Union[str, Path]


@dataclass(frozen=True)
class Config:
    """Immutable, validated runtime configuration."""

    home: Path
    permitted_roots: Tuple[Path, ...]
    state_dir: Path
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    ollama_timeout: float = DEFAULT_OLLAMA_TIMEOUT

    @property
    def audit_path(self) -> Path:
        return self.state_dir / "audit.jsonl"

    def ensure_state_dir(self) -> Path:
        """Create the state directory with owner-only access."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        return self.state_dir

    def describe(self) -> str:
        """Human-readable summary used by ``--show-config``."""
        roots = "\n".join(f"  - {r}" for r in self.permitted_roots)
        return "\n".join(
            [
                "Alwin Fusion configuration",
                f"  home directory : {self.home}",
                "  permitted roots:",
                roots,
                f"  state dir      : {self.state_dir}",
                f"  ollama url     : {self.ollama_base_url}",
                f"  ollama model   : {self.ollama_model or '(not set)'}",
                f"  ollama timeout : {self.ollama_timeout:g}s",
            ]
        )


def _resolve_root(entry: PathLike, home: Path) -> Path:
    root = Path(entry).expanduser()
    if not root.is_absolute():
        root = home / root
    # resolve() follows symlinks so a root cannot be a symlink pointing
    # outside the home directory.
    root = root.resolve()
    if root == home or not root.is_relative_to(home):
        raise ConfigError(
            f"Permitted root '{entry}' must be inside the home directory "
            f"({home}); refusing to configure it."
        )
    return root


def _parse_roots(
    roots: Optional[Sequence[PathLike]], env: Mapping[str, str], home: Path
) -> Tuple[Path, ...]:
    if roots is None:
        raw = env.get(ROOTS_ENV, "")
        entries: Iterable[str] = [e for e in raw.split(os.pathsep) if e.strip()] if raw.strip() else ["Documents"]
    else:
        entries = [str(e) for e in roots]
    resolved: list[Path] = []
    for entry in entries:
        root = _resolve_root(entry, home)
        if root not in resolved:
            resolved.append(root)
    if not resolved:
        raise ConfigError("At least one permitted root must be configured.")
    return tuple(resolved)


def load_config(
    env: Optional[Mapping[str, str]] = None,
    *,
    home: Optional[PathLike] = None,
    roots: Optional[Sequence[PathLike]] = None,
    state_dir: Optional[PathLike] = None,
    ollama_base_url: Optional[str] = None,
    ollama_model: Optional[str] = None,
    ollama_timeout: Optional[float] = None,
) -> Config:
    """Build a validated :class:`Config`.

    Explicit keyword arguments take precedence over environment
    variables, which take precedence over built-in defaults.
    """
    env = os.environ if env is None else env

    if home is None:
        home = env.get(HOME_ENV) or str(Path.home())
    home_path = Path(home).expanduser().resolve()

    permitted_roots = _parse_roots(roots, env, home_path)

    if state_dir is None:
        raw_state = env.get(STATE_DIR_ENV)
        if raw_state:
            state_path = Path(raw_state).expanduser()
        else:
            xdg = env.get("XDG_STATE_HOME")
            base = Path(xdg).expanduser() if xdg else home_path / ".local" / "state"
            state_path = base / "alwin-fusion"
    else:
        state_path = Path(state_dir).expanduser()

    base_url = (
        ollama_base_url
        or env.get("OLLAMA_BASE_URL")
        or DEFAULT_OLLAMA_BASE_URL
    ).rstrip("/")
    if ollama_model is None:
        model = env.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL).strip()
    else:
        model = ollama_model

    raw_timeout = ollama_timeout if ollama_timeout is not None else env.get("OLLAMA_TIMEOUT")
    try:
        timeout = float(raw_timeout) if raw_timeout is not None else DEFAULT_OLLAMA_TIMEOUT
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"OLLAMA_TIMEOUT must be a number, got {raw_timeout!r}.") from exc
    if timeout <= 0:
        raise ConfigError("OLLAMA_TIMEOUT must be greater than zero.")

    return Config(
        home=home_path,
        permitted_roots=permitted_roots,
        state_dir=state_path,
        ollama_base_url=base_url,
        ollama_model=model,
        ollama_timeout=timeout,
    )
