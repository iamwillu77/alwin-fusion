"""Audit logging.

Every task attempt is recorded as one JSON line in
``<state_dir>/audit.jsonl`` with owner-only permissions (directory
0700, file 0600).

What is recorded: task id, timestamp, parser used, proposed action,
permission decision, approval decision, execution result, verification
result and any error.

What is never recorded: passwords, credentials, tokens or the content
of user files (content values are redacted to a length marker).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

SENSITIVE_KEYS = frozenset(
    {
        "content",
        "spec",
        "password",
        "passphrase",
        "token",
        "secret",
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "authorization",
    }
)

MAX_FIELD_CHARS = 400


class AuditError(Exception):
    """Raised when an audit record cannot be written."""


def redact(value, key: Optional[str] = None):
    """Recursively strip sensitive values and cap long strings."""
    if key is not None and key.lower() in SENSITIVE_KEYS:
        if isinstance(value, str):
            return f"<redacted:{len(value)} chars>"
        return "<redacted>"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + "...<truncated>"
    return value


class AuditLogger:
    """Append-only JSON-lines audit log with restricted access."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _ensure(self) -> None:
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
            if self.path.exists():
                os.chmod(self.path, 0o600)
        except OSError as exc:
            raise AuditError(
                f"Cannot prepare audit log directory {parent}: {exc}"
            ) from exc

    def record(
        self,
        *,
        task_id: str,
        command: str,
        parser: str = "deterministic",
        dry_run: bool = False,
        action: Optional[dict] = None,
        permission: Optional[dict] = None,
        approval: str = "not_required",
        execution: Optional[dict] = None,
        verification: Optional[dict] = None,
        error: Optional[str] = None,
        extra: Optional[Dict] = None,
    ) -> Dict:
        """Append one task record and return the stored (redacted) entry."""
        entry = {
            "task_id": task_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "parser": parser,
            "dry_run": dry_run,
            "action": action,
            "permission": permission,
            "approval": approval,
            "execution": execution,
            "verification": verification,
            "error": error,
        }
        if extra:
            entry.update(extra)
        entry = redact(entry)

        line = json.dumps(entry, ensure_ascii=False) + "\n"
        self._ensure()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise AuditError(f"Cannot write audit log {self.path}: {exc}") from exc
        return entry

    def read_all(self) -> Iterable[Dict]:
        """Read back all records (used by tests and future tooling)."""
        if not self.path.exists():
            return []
        records = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records
