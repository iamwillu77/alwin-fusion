"""Local task history (v0.5): persistent, validated, display-only.

One JSON line per finished task in ``<state_dir>/task_history.jsonl``
(same owner-only directory as the audit log: dir 0700, file 0600).

Design rules:

* **Display-only by construction.** This module never imports the
  planner, pipeline, executor or dispatcher.  A history record is
  *data to look at*: there is no function here that can execute,
  re-run or interpret a record as an instruction, and the task
  manager exposes no "run this history entry" path.  A malformed or
  hostile record can at worst be *displayed* (after sanitizing) or
  skipped.
* **Validated on read.** Every record is checked against a closed
  schema (bounded strings, known states, bounded lists) before it is
  returned; anything that does not conform is skipped and counted,
  never repaired or guessed at.
* **Redacted on write.** Requests are masked exactly like the audit
  log (file *content* never recorded — only length markers), long
  strings are truncated, and no execution payload is stored.
* **Not tamper-proof.** It is a normal user-owned file with restricted
  modes; we say so plainly instead of implying integrity guarantees.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ai_core.audit import redact
from taskui.lifecycle import ALL_STATES, STATE_LABELS, utc_now

#: History record format version (bump on incompatible change).
HISTORY_VERSION = 1

#: Bounds applied to every record read back from disk.
MAX_RECORDS = 500
MAX_COMMAND_CHARS = 400
MAX_MESSAGE_CHARS = 600
MAX_STEPS = 64
MAX_NOTES = 16
MAX_CHECKS = 64

VALID_VERIFICATION_STATUSES = frozenset(
    {"passed", "failed", "skipped", None}
)
VALID_APPROVALS = frozenset(
    {
        "not_required",
        "pending",
        "approved",
        "rejected",
        "invalidated",
        "dry_run",
    }
)
VALID_REPORT_STATUSES = frozenset(
    {
        "success",
        "dry_run",
        "rejected",
        "denied",
        "unsupported",
        "failed",
        "exit",
        None,
    }
)


class HistoryError(Exception):
    """Raised when the history file cannot be written."""


def _bounded_str(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value if len(value) <= limit else value[:limit] + "…<truncated>"


def summarize_verification(verification: Optional[dict]) -> Optional[dict]:
    """Curate a verification dict for display/history (no raw payloads)."""
    if not isinstance(verification, dict):
        return None
    checks = []
    raw_checks = verification.get("checks")
    if isinstance(raw_checks, list):
        for check in raw_checks[:MAX_CHECKS]:
            if not isinstance(check, dict):
                continue
            checks.append(
                {
                    "name": _bounded_str(check.get("name"), 80),
                    "passed": bool(check.get("passed")),
                    "detail": _bounded_str(check.get("detail"), 200),
                }
            )
    status = verification.get("status")
    return {
        "status": status if status in VALID_VERIFICATION_STATUSES else None,
        "message": _bounded_str(verification.get("message"), MAX_MESSAGE_CHARS),
        "checks": checks,
    }


def summarize_steps(steps) -> List[dict]:
    """Curate per-step records for display/history (no step payloads)."""
    curated: List[dict] = []
    if not isinstance(steps, list):
        return curated
    for step in steps[:MAX_STEPS]:
        if not isinstance(step, dict):
            continue
        curated.append(
            {
                "index": step.get("index"),
                "action": _bounded_str(step.get("action"), 60),
                "status": _bounded_str(step.get("status"), 20),
                "ok": step.get("ok"),
                "error": _bounded_str(step.get("error"), MAX_MESSAGE_CHARS)
                or None,
            }
        )
    return curated


class TaskHistory:
    """Append-only, display-only task history with restricted modes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def _ensure(self) -> None:
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
            if self.path.exists():
                os.chmod(self.path, 0o600)
        except OSError as exc:
            raise HistoryError(
                f"Cannot prepare task-history directory {parent}: {exc}"
            ) from exc

    def record(self, record) -> Dict:
        """Append one finished :class:`~taskui.lifecycle.TaskRecord`.

        Only curated summary fields are stored — never raw file
        content, never execution payloads.  The command string is
        masked with the same rule as the audit log (user-supplied
        file content becomes a length marker).
        """
        from ai_core.pipeline import TaskPipeline  # local: masking helper

        masked_command = TaskPipeline._mask_command(
            record.command, (record._preview.action if record._preview else None)
        )
        entry = {
            "history_version": HISTORY_VERSION,
            "task_id": record.task_id,
            "created_at": record.created_at,
            "finished_at": record.updated_at,
            "request": masked_command,
            "dry_run": bool(record.dry_run),
            "state": record.state,
            "state_label": STATE_LABELS.get(record.state, record.state),
            "plan": record.plan,  # already curated (action/target/summary)
            "proposal": _bounded_str(record.proposal, MAX_MESSAGE_CHARS),
            "approval": record.approval
            if record.approval in VALID_APPROVALS
            else "not_required",
            "steps": summarize_steps(record.steps),
            "result_message": _bounded_str(
                record.result_message, MAX_MESSAGE_CHARS
            ),
            "verification": summarize_verification(record.verification),
            "report_status": record.report_status
            if record.report_status in VALID_REPORT_STATUSES
            else None,
            "error": _bounded_str(record.error, MAX_MESSAGE_CHARS),
            "notes": [
                _bounded_str(n, MAX_MESSAGE_CHARS) for n in record.notes[:MAX_NOTES]
            ],
        }
        # Defence in depth: the audit redactor also strips any sensitive
        # key that ever sneaks into a summary structure.
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
            raise HistoryError(
                f"Cannot write task history {self.path}: {exc}"
            ) from exc
        return entry

    # ------------------------------------------------------------------ #
    # Read (validated; malformed data is skipped, never executed)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _valid_plan(value) -> Optional[dict]:
        if not isinstance(value, dict):
            return None
        plan = {
            "action": _bounded_str(value.get("action"), 60),
            "summary": _bounded_str(value.get("summary"), MAX_MESSAGE_CHARS),
            "target": _bounded_str(value.get("target"), MAX_MESSAGE_CHARS),
            "mutating": bool(value.get("mutating")),
            "requires_approval": bool(value.get("requires_approval")),
        }
        if not plan["action"]:
            return None
        return plan

    @classmethod
    def _validate_record(cls, raw) -> Optional[Dict]:
        """Return a sanitized record, or ``None`` when unusable."""
        if not isinstance(raw, dict):
            return None
        version = raw.get("history_version")
        if version != HISTORY_VERSION:
            return None
        task_id = raw.get("task_id")
        if not isinstance(task_id, str) or not (0 < len(task_id) <= 64):
            return None
        state = raw.get("state")
        if state not in ALL_STATES:
            return None

        verification = raw.get("verification")
        if verification is not None and not isinstance(verification, dict):
            verification = None
        if isinstance(verification, dict):
            status = verification.get("status")
            if status not in VALID_VERIFICATION_STATUSES:
                verification = dict(verification, status=None)

        steps = raw.get("steps")
        curated_steps = summarize_steps(steps) if isinstance(steps, list) else []

        approval = raw.get("approval")
        if approval not in VALID_APPROVALS:
            approval = "not_required"
        report_status = raw.get("report_status")
        if report_status not in VALID_REPORT_STATUSES:
            report_status = None
        notes = raw.get("notes")
        if not isinstance(notes, list):
            notes = []

        return {
            "history_version": HISTORY_VERSION,
            "task_id": task_id,
            "created_at": _bounded_str(raw.get("created_at"), 40),
            "finished_at": _bounded_str(raw.get("finished_at"), 40),
            "request": _bounded_str(raw.get("request"), MAX_COMMAND_CHARS + 40),
            "dry_run": bool(raw.get("dry_run")),
            "state": state,
            "state_label": STATE_LABELS.get(state, state),
            "plan": cls._valid_plan(raw.get("plan")),
            "proposal": _bounded_str(raw.get("proposal"), MAX_MESSAGE_CHARS),
            "approval": approval,
            "steps": curated_steps,
            "result_message": _bounded_str(
                raw.get("result_message"), MAX_MESSAGE_CHARS
            ),
            "verification": verification,
            "report_status": report_status,
            "error": _bounded_str(raw.get("error"), MAX_MESSAGE_CHARS),
            "notes": [
                _bounded_str(n, MAX_MESSAGE_CHARS) for n in notes[:MAX_NOTES]
            ],
        }

    def load(self, limit: int = 50) -> Tuple[List[Dict], int]:
        """Return ``(records, skipped)`` — newest first.

        Missing file -> ``([], 0)``.  Corrupt JSON lines, records that
        fail schema validation and unreadable files are *skipped and
        counted*, never repaired and never executed.  A hostile file
        cannot become an instruction: this method only produces
        sanitized display data.
        """
        if not self.path.exists():
            return [], 0
        records: List[Dict] = []
        skipped = 0
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except (OSError, UnicodeDecodeError):
            return [], 1
        for line in lines[-MAX_RECORDS * 2:]:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            validated = self._validate_record(raw)
            if validated is None:
                skipped += 1
                continue
            records.append(validated)
        records.reverse()  # newest first
        return records[: max(0, int(limit))], skipped

    def summary_rows(self, limit: int = 50) -> List[dict]:
        """Compact rows for a history list (display only)."""
        records, _skipped = self.load(limit=limit)
        rows = []
        for record in records:
            plan = record.get("plan") or {}
            rows.append(
                {
                    "task_id": record["task_id"],
                    "finished_at": record["finished_at"] or record["created_at"],
                    "request": record["request"],
                    "state": record["state"],
                    "state_label": record["state_label"],
                    "action": plan.get("action", ""),
                    "error": record["error"],
                }
            )
        return rows

    def detail(self, task_id: str) -> Optional[Dict]:
        """One full (sanitized) record by task id, for inspection."""
        records, _skipped = self.load(limit=MAX_RECORDS)
        for record in records:
            if record["task_id"] == task_id:
                return record
        return None


__all__ = [
    "HistoryError",
    "TaskHistory",
    "summarize_steps",
    "summarize_verification",
    "utc_now",
]
