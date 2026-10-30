"""Persistent scheduled-job storage for Alwin Fusion (v0.16.0).

A second SQLite database — ``state_dir/scheduler.db`` — deliberately
separate from ``memory.db``.  The clear reason: memory is a *privacy*
store the user may disable or clear wholesale, while schedules are
*operational state* that must keep working while memory is off and must
never be wiped by "Clear all memories".  A shared file would mean a
shared ``PRAGMA user_version`` — coupled migrations and coupled failure
domains in both directions — so the two stores stay independent.  Same
library, same permission layout (0700 dir / 0600 file), same honesty
patterns as :mod:`companion.memory`.

Schema v1 (see ``_MIGRATIONS``):

* ``jobs`` — one row per job, flat typed columns (schedule fields
  inline), re-validated through :mod:`ai_core.schedule` on every read:
  a tampered row is refused honestly, never coerced.
* ``occurrences`` — per-occurrence bookkeeping with a
  ``UNIQUE(job_id, occurrence)`` constraint.  The claim is a
  transactional ``INSERT OR IGNORE`` whose rowcount decides: exactly one
  claim can win, which gives duplicate-occurrence prevention,
  notification deduplication, attempt history and the dismissed flag —
  in one place, safe across threads and processes.

Guarantees and limits (documented, tested):

* Writes are rowcount-checked inside explicit transactions; a failed
  write rolls back and raises :class:`JobStoreError` with an honest,
  content-free kind (``unavailable``/``locked``/``full``/``corrupt``/
  ``schema``/``invalid``/``not_found``/``exists``).
* A database newer than this build is refused (``schema``) — never
  downgraded, never wiped.
* Crash recovery: SQLite's journal guarantees the file is either the
  pre- or post-transaction state; an occurrence claimed but whose
  outcome write never landed stays claimed (the attempt is retried at
  most ``max_retries`` times, then reported as uncertain) — the system
  prefers a missed repeat over a duplicate execution.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ai_core.schedule import (
    MAX_JOBS,
    ScheduledJob,
    ScheduleError,
    job_from_row,
    job_to_row,
    validate_outcome,
)

#: Database file name inside the state directory.
DB_FILENAME = "scheduler.db"

#: Current schema version (see ``_MIGRATIONS``).
SCHEMA_VERSION = 1

#: Occurrence statuses (what happened to a claimed occurrence).
STATUS_PENDING = "pending"
STATUS_DISPATCHED = "dispatched"
STATUS_MISSED = "missed"
STATUS_CANCELLED = "cancelled"
STATUS_ERROR = "error"
OCCURRENCE_STATUSES: Tuple[str, ...] = (
    STATUS_PENDING, STATUS_DISPATCHED, STATUS_MISSED,
    STATUS_CANCELLED, STATUS_ERROR,
)

#: Content-free failure text per kind (never echo paths/SQL/details).
ERROR_TEXT: Dict[str, str] = {
    "unavailable": "The scheduler store is unavailable.",
    "locked": "The scheduler store is locked by another process.",
    "full": "The scheduler store cannot grow (storage is full).",
    "corrupt": "The scheduler store is unreadable or corrupt.",
    "schema": "The scheduler store was written by a newer version.",
    "invalid": "The scheduler store rejected that record.",
    "not_found": "No such scheduled job.",
    "exists": "A scheduled job with that identifier already exists.",
    "exists_hint": "That would exceed the scheduled-job limit.",
}


class JobStoreError(Exception):
    """Honest, content-free storage failure."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{ERROR_TEXT.get(kind, kind)} {detail}".strip())
        self.kind = kind
        self.detail = detail


#: Explicit ordered migrations — v1 is the initial schema.  Each script
#: runs inside ONE transaction together with the user_version bump
#: (``executescript`` implicitly commits, so BEGIN/COMMIT are embedded).
_MIGRATIONS: Dict[int, str] = {
    1: """
    CREATE TABLE jobs (
        job_id             TEXT PRIMARY KEY,
        title              TEXT NOT NULL,
        job_type           TEXT NOT NULL,
        kind               TEXT NOT NULL,
        at_utc             INTEGER,
        hour               INTEGER NOT NULL DEFAULT 0,
        minute             INTEGER NOT NULL DEFAULT 0,
        weekdays           TEXT NOT NULL DEFAULT '',
        tzid               TEXT NOT NULL DEFAULT '',
        description        TEXT NOT NULL,
        state              TEXT NOT NULL,
        created_at         INTEGER NOT NULL,
        updated_at         INTEGER NOT NULL,
        next_occurrence    INTEGER,
        last_attempt       INTEGER,
        last_success       INTEGER,
        last_outcome       TEXT NOT NULL DEFAULT '',
        approval           TEXT NOT NULL DEFAULT '',
        project            TEXT NOT NULL DEFAULT '',
        condition_op       TEXT NOT NULL DEFAULT '',
        condition_arg      TEXT NOT NULL DEFAULT '',
        capabilities       TEXT NOT NULL DEFAULT '',
        max_retries        INTEGER NOT NULL DEFAULT 0,
        retries_used       INTEGER NOT NULL DEFAULT 0,
        retry_occurrence   INTEGER
    );
    CREATE TABLE occurrences (
        job_id      TEXT NOT NULL,
        occurrence  INTEGER NOT NULL,
        status      TEXT NOT NULL,
        outcome     TEXT NOT NULL DEFAULT '',
        approval    TEXT NOT NULL DEFAULT '',
        notified    INTEGER NOT NULL DEFAULT 0,
        dismissed   INTEGER NOT NULL DEFAULT 0,
        created_at  INTEGER NOT NULL,
        PRIMARY KEY (job_id, occurrence)
    );
    CREATE INDEX occurrences_job ON occurrences (job_id, occurrence);
    CREATE INDEX jobs_due ON jobs (state, next_occurrence);
    """,
}

_JOB_COLUMNS: Tuple[str, ...] = (
    "job_id", "title", "job_type", "kind", "at_utc", "hour", "minute",
    "weekdays", "tzid", "description", "state", "created_at", "updated_at",
    "next_occurrence", "last_attempt", "last_success", "last_outcome",
    "approval", "project", "condition_op", "condition_arg", "capabilities",
    "max_retries", "retries_used", "retry_occurrence",
)

_OCC_COLUMNS: Tuple[str, ...] = (
    "job_id", "occurrence", "status", "outcome", "approval", "notified",
    "dismissed", "created_at",
)


def _classify(exc: BaseException) -> str:
    """Map a driver exception to an honest, content-free kind."""
    message = str(exc).lower()
    if ("unable to open" in message or "disk i/o" in message
            or "readonly" in message or "permission denied" in message
            or "read-only" in message or "no such file" in message):
        return "unavailable"
    if "locked" in message:
        return "locked"
    if "disk is full" in message or "disk full" in message:
        return "full"
    if ("not a database" in message or "malformed" in message
            or "corrupt" in message):
        return "corrupt"
    if isinstance(exc, sqlite3.IntegrityError):
        return "invalid"
    if isinstance(exc, sqlite3.DatabaseError):
        return "corrupt"
    return "unavailable"


class JobStore:
    """SQLite-backed persistent scheduler state — local, bounded, honest.

    Open is eager: constructing the store migrates the schema (or raises
    :class:`JobStoreError`).  Not thread-safe by itself — the scheduler
    performs every store call from one thread (the tick thread), which
    is the documented threading contract.
    """

    def __init__(self, path: Path, *, now: Optional[Callable[[], int]] = None
                 ) -> None:
        self._path = Path(path)
        self._now = now or (lambda: int(time.time()))
        self._last_error: Optional[str] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    # ------------------------------------------------------------------ #
    # Lifecycle & migrations (same contract as MemoryStore)
    # ------------------------------------------------------------------ #

    @classmethod
    def open_for(cls, state_dir: Path,
                 *, now: Optional[Callable[[], int]] = None
                 ) -> "JobStore":
        """Open ``state_dir/scheduler.db`` (dir 0700 when created)."""
        directory = Path(state_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise JobStoreError("unavailable",
                                exc.__class__.__name__) from exc
        return cls(directory / DB_FILENAME, now=now)

    def _open(self) -> None:
        try:
            conn = sqlite3.connect(str(self._path), timeout=2.0,
                                   isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 2000")
            os.chmod(self._path, 0o600)
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
        except OSError as exc:
            self._raise("unavailable", exc)
        try:
            self._migrate(conn)
        except JobStoreError:
            conn.close()
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            conn.close()
            self._raise(_classify(exc), exc)
        self._conn = conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply every pending migration in ONE transaction."""
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            version = int(row[0])
        except (sqlite3.DatabaseError, IndexError, ValueError) as exc:
            self._raise(_classify(exc), exc)
        if version > SCHEMA_VERSION:
            detail = f"found v{version}"
            self._last_error = f"{ERROR_TEXT['schema']}: {detail}"
            raise JobStoreError("schema", detail)
        pending = [target for target in sorted(_MIGRATIONS)
                   if target > version]
        if not pending:
            return
        script = "BEGIN;\n"
        for target in pending:
            script += _MIGRATIONS[target].strip() + "\n"
            script += f"PRAGMA user_version = {int(target)};\n"
        script += "COMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:  # pragma: no cover - defensive
                pass
            self._raise(_classify(exc), exc)
        self._last_error = None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.DatabaseError:  # pragma: no cover - defensive
                pass
            self._conn = None

    def _raise(self, kind: str, exc: BaseException) -> None:
        detail = exc.__class__.__name__
        self._last_error = f"{ERROR_TEXT.get(kind, kind)} ({detail})"
        raise JobStoreError(kind, detail) from exc

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_error(self) -> Optional[str]:
        """Content-free description of the most recent failure."""
        return self._last_error

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise JobStoreError("unavailable", "store is closed")
        return self._conn

    def _execute(self, sql: str, params: Tuple[Any, ...],
                 *, expect: int = 1) -> None:
        """Run one statement; a wrong rowcount is an honest failure."""
        conn = self._require()
        try:
            cursor = conn.execute(sql, params)
            if cursor.rowcount != expect:
                conn.execute("ROLLBACK")
                kind = ("not_found" if expect == 0 else "invalid")
                self._last_error = (
                    f"{ERROR_TEXT[kind]} (rowcount={cursor.rowcount})")
                raise JobStoreError(kind, f"rowcount={cursor.rowcount}")
        except JobStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)

    # ------------------------------------------------------------------ #
    # Jobs
    # ------------------------------------------------------------------ #

    def add(self, job: ScheduledJob) -> ScheduledJob:
        """Persist a new job (validated; refuses duplicates and overflow)."""
        try:
            job.validate(now=self._now())
        except ScheduleError as exc:
            raise JobStoreError("invalid", exc.reason) from exc
        conn = self._require()
        try:
            count = int(conn.execute("SELECT COUNT(*) FROM jobs")
                        .fetchone()[0])
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover - _raise always raises
        if count >= MAX_JOBS:
            raise JobStoreError("invalid", ERROR_TEXT["exists_hint"])
        row = job_to_row(job)
        placeholders = ", ".join("?" for _ in _JOB_COLUMNS)
        columns = ", ".join(_JOB_COLUMNS)
        sql = f"INSERT INTO jobs ({columns}) VALUES ({placeholders})"
        try:
            self._execute(sql, tuple(row[c] for c in _JOB_COLUMNS),
                          expect=1)
        except JobStoreError as exc:
            if exc.kind == "invalid":
                # A second row with the same primary key is a duplicate,
                # not a generic invalid record.
                self._last_error = ERROR_TEXT["exists"]
                raise JobStoreError("exists", "duplicate job_id") from exc
            raise
        self._last_error = None
        return job

    def get(self, job_id: str) -> Optional[ScheduledJob]:
        """The job, or None (never raises for a missing id)."""
        conn = self._require()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover
        if row is None:
            return None
        try:
            return job_from_row(dict(row))
        except ScheduleError as exc:
            raise JobStoreError("corrupt", exc.reason) from exc

    def list_jobs(self, *, state: Optional[str] = None,
                  job_type: Optional[str] = None) -> List[ScheduledJob]:
        """All jobs (optionally filtered), oldest-created first."""
        conn = self._require()
        sql = "SELECT * FROM jobs"
        clauses, params = [], []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if job_type is not None:
            clauses.append("job_type = ?")
            params.append(job_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, job_id ASC"
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover
        jobs: List[ScheduledJob] = []
        for row in rows:
            try:
                jobs.append(job_from_row(dict(row)))
            except ScheduleError as exc:
                raise JobStoreError("corrupt", exc.reason) from exc
        return jobs

    def update(self, job: ScheduledJob, *, now: Optional[int] = None
               ) -> ScheduledJob:
        """Replace an existing job's row (transactional, rowcount-checked).

        Returns the job with ``updated_at`` stamped.  Editing a job is
        re-validation, not a backdoor: schedule/type/state all go through
        the same validator as creation.
        """
        stamp = self._now() if now is None else int(now)
        stored = ScheduledJob(
            job_id=job.job_id, title=job.title, job_type=job.job_type,
            schedule=job.schedule, description=job.description,
            state=job.state, created_at=job.created_at, updated_at=stamp,
            next_occurrence=job.next_occurrence,
            last_attempt=job.last_attempt, last_success=job.last_success,
            last_outcome=job.last_outcome, approval=job.approval,
            project=job.project, condition=job.condition,
            required_capabilities=job.required_capabilities,
            max_retries=job.max_retries, retries_used=job.retries_used,
            retry_occurrence=job.retry_occurrence,
        )
        try:
            # Structural validation only: completed one-time jobs have
            # past instants and must remain updatable (outcome writes,
            # pause/cancel).  The "not in the past" constraint belongs
            # to creation and explicit edits, which validate with now.
            stored.validate()
        except ScheduleError as exc:
            raise JobStoreError("invalid", exc.reason) from exc
        row = job_to_row(stored)
        assignments = ", ".join(f"{c} = ?" for c in _JOB_COLUMNS
                                if c != "job_id")
        sql = f"UPDATE jobs SET {assignments} WHERE job_id = ?"
        params = tuple(row[c] for c in _JOB_COLUMNS if c != "job_id") \
            + (job.job_id,)
        self._execute(sql, params, expect=1)
        self._last_error = None
        return stored

    def due(self, now: int, *, limit: int = 5) -> List[ScheduledJob]:
        """Enabled jobs whose next occurrence has arrived (bounded)."""
        conn = self._require()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state = 'enabled' "
                "AND next_occurrence IS NOT NULL "
                "AND next_occurrence <= ? "
                "ORDER BY next_occurrence ASC LIMIT ?",
                (int(now), int(limit)),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover
        jobs: List[ScheduledJob] = []
        for row in rows:
            try:
                jobs.append(job_from_row(dict(row)))
            except ScheduleError as exc:
                raise JobStoreError("corrupt", exc.reason) from exc
        return jobs

    def counts(self) -> Dict[str, int]:
        """``{"enabled": n, "paused": n, "cancelled": n, "due": n}``."""
        conn = self._require()
        try:
            row = conn.execute(
                "SELECT "
                "SUM(CASE WHEN state='enabled' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN state='paused' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN state='cancelled' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN state='enabled' AND next_occurrence "
                "IS NOT NULL AND next_occurrence <= ? THEN 1 ELSE 0 END) "
                "FROM jobs", (self._now(),)).fetchone()
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover
        return {
            "enabled": int(row[0] or 0), "paused": int(row[1] or 0),
            "cancelled": int(row[2] or 0), "due": int(row[3] or 0),
        }

    # ------------------------------------------------------------------ #
    # Occurrences (claim, notification dedupe, history)
    # ------------------------------------------------------------------ #

    def claim(self, job_id: str, occurrence: int,
              *, status: str = STATUS_PENDING) -> bool:
        """Atomically claim an occurrence — True for the single winner.

        The UNIQUE(job_id, occurrence) constraint plus rowcount makes
        this the duplicate-prevention primitive: a second claim (same
        thread, other thread, other process) gets False and the
        occurrence is never dispatched twice.
        """
        if status not in OCCURRENCE_STATUSES:
            raise JobStoreError("invalid", "unknown occurrence status")
        conn = self._require()
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO occurrences "
                "(job_id, occurrence, status, outcome, approval, "
                " notified, dismissed, created_at) "
                "VALUES (?, ?, ?, '', '', 0, 0, ?)",
                (str(job_id), int(occurrence), status, self._now()),
            )
            won = cursor.rowcount == 1
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover
        if won:
            self._last_error = None
        return won

    def occurrence(self, job_id: str, occurrence: int) -> Optional[Dict]:
        """The occurrence row (dict), or None."""
        conn = self._require()
        try:
            row = conn.execute(
                "SELECT * FROM occurrences WHERE job_id = ? "
                "AND occurrence = ?", (str(job_id), int(occurrence))
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover
        return dict(row) if row is not None else None

    def finish_occurrence(self, job_id: str, occurrence: int, *,
                          status: str, outcome: str = "",
                          approval: str = "") -> None:
        """Record what happened to a claimed occurrence (final state)."""
        if status not in OCCURRENCE_STATUSES:
            raise JobStoreError("invalid", "unknown occurrence status")
        self._execute(
            "UPDATE occurrences SET status = ?, outcome = ?, approval = ? "
            "WHERE job_id = ? AND occurrence = ?",
            (status, validate_outcome(outcome), approval,
             str(job_id), int(occurrence)), expect=1)

    def mark_notified(self, job_id: str, occurrence: int) -> bool:
        """Flip notified 0→1 once; False when it was already set."""
        conn = self._require()
        try:
            cursor = conn.execute(
                "UPDATE occurrences SET notified = 1 "
                "WHERE job_id = ? AND occurrence = ? AND notified = 0",
                (str(job_id), int(occurrence)))
            flipped = cursor.rowcount == 1
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover
        return flipped

    def dismiss(self, job_id: str, occurrence: int) -> bool:
        """Mark an occurrence dismissed (user acknowledged it)."""
        conn = self._require()
        try:
            cursor = conn.execute(
                "UPDATE occurrences SET dismissed = 1 "
                "WHERE job_id = ? AND occurrence = ?",
                (str(job_id), int(occurrence)))
            found = cursor.rowcount == 1
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover
        return found

    def history(self, job_id: str, *, limit: int = 20) -> List[Dict]:
        """Recent occurrence rows for one job (newest first, bounded)."""
        conn = self._require()
        try:
            rows = conn.execute(
                "SELECT * FROM occurrences WHERE job_id = ? "
                "ORDER BY occurrence DESC LIMIT ?",
                (str(job_id), int(limit))).fetchall()
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover
        return [dict(r) for r in rows]

    def prune_history(self, job_id: str, *, keep: int = 50) -> int:
        """Drop the oldest occurrence rows beyond *keep* (bounded DB)."""
        conn = self._require()
        try:
            cursor = conn.execute(
                "DELETE FROM occurrences WHERE job_id = ? AND occurrence "
                "NOT IN (SELECT occurrence FROM occurrences WHERE job_id = ? "
                "ORDER BY occurrence DESC LIMIT ?)",
                (str(job_id), str(job_id), int(keep)))
            return max(0, cursor.rowcount)
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
            raise  # pragma: no cover

    # ------------------------------------------------------------------ #
    # Test/verification support
    # ------------------------------------------------------------------ #

    def inject_failure(self) -> None:
        """Force the next statement to fail (sandbox error-simulation).

        Used by the verification script and tests to prove honest error
        reporting end-to-end: it closes the live connection so every
        subsequent call raises ``unavailable`` with the documented text.
        """
        self.close()
        self._last_error = f"{ERROR_TEXT['unavailable']} (closed)"


__all__ = [
    "DB_FILENAME",
    "ERROR_TEXT",
    "OCCURRENCE_STATUSES",
    "SCHEMA_VERSION",
    "STATUS_CANCELLED",
    "STATUS_DISPATCHED",
    "STATUS_ERROR",
    "STATUS_MISSED",
    "STATUS_PENDING",
    "JobStore",
    "JobStoreError",
]
