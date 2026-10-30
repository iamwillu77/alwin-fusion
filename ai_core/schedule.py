"""Typed, validated schedules and scheduled jobs for Alwin Fusion (v0.16.0).

A *scheduled job* is structured data — never an executable string, never
a cron snippet, never model-authored code.  This module is the trusted
model and calendar: closed enums, bounded fields, validated timezone
identifiers, and a **deterministic** next-occurrence computation with
explicit daylight-saving rules.  No I/O, no threads, no network — safe
to unit-test headless (the persistence lives in :mod:`ai_core.jobstore`,
the runtime in :mod:`ai_core.scheduler`).

Design rules (enforced by tests):

* three schedule kinds only — ``once`` (a UTC instant), ``daily``
  (wall time), ``weekly`` (wall time + a weekday set) — and three job
  types: ``reminder`` (notify only), ``task`` (execute through the
  existing TaskManager/permissioned pipeline), ``check`` (bounded
  conditional evaluation against a closed operation set),
* jobs carry stable ids, bounded user-visible text, explicit state
  (``enabled``/``paused``/``cancelled``), occurrence bookkeeping, a
  bounded retry policy, capability hints and the approval status of the
  latest attempt,
* credential-shaped text is refused with a fixed, content-free reason
  (the attempted text is never echoed or stored),
* recurrence is bounded: recurring schedules are at least
  ``MIN_RECURRING_SECONDS`` apart (a vague "check this regularly" can
  never become a high-frequency background job),
* timezone interpretation is explicit: a valid IANA identifier or ``""``
  meaning the machine's local zone (resolved by :func:`local_tz`), and
  DST transitions follow the documented fold rules below.

Daylight-saving rules (deterministic, tested with Europe/Berlin):

* **Ambiguous wall times** (the hour that repeats when clocks go back)
  fire at the **first** occurrence (``fold=0``, pre-transition offset).
* **Nonexistent wall times** (the hour skipped when clocks go forward)
  are interpreted with the pre-transition offset and therefore fire at
  the resulting shifted local time — a 02:30 job on a 02:00→03:00
  spring-forward day fires at 03:30 local, exactly once.
* One-time jobs are stored as UTC instants, so they are immune to DST
  reinterpretation; the display timezone is presentation only.
"""

from __future__ import annotations

import datetime as _dt
import re
import uuid
import zoneinfo
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# --------------------------------------------------------------------------- #
# Closed sets and bounds
# --------------------------------------------------------------------------- #

#: Job types — reminders notify, tasks execute through the normal
#: permissioned pipeline, checks evaluate a bounded condition first.
JOB_TYPES: Tuple[str, ...] = ("reminder", "task", "check")

#: Job lifecycle states.  Only ``enabled`` jobs are ever considered by
#: the scheduler tick; ``cancelled`` is terminal.
JOB_STATES: Tuple[str, ...] = ("enabled", "paused", "cancelled")

#: Schedule kinds (no free-form strings, no cron).
SCHEDULE_KINDS: Tuple[str, ...] = ("once", "daily", "weekly")

#: The closed conditional-check operation set (Phase 6): each op is a
#: single bounded query — no shell, no scans, no monitoring.
CONDITION_OPS: Tuple[str, ...] = ("file_exists", "file_updated",
                                  "app_installed")

#: Approval/attention values shown for the latest attempt.
APPROVAL_VALUES: Tuple[str, ...] = ("", "not_required", "pending",
                                    "approved", "refused",
                                    "needs_attention")

#: Bounds (title/task/condition/capability are user-visible or routed
#: into the pipeline, so they are strictly bounded).
MAX_TITLE_CHARS = 80
MAX_TASK_CHARS = 500
MAX_CONDITION_CHARS = 300
MAX_PROJECT_CHARS = 60
MAX_CAPABILITIES = 8
MAX_CAPABILITY_CHARS = 60
MAX_OUTCOME_CHARS = 120
MAX_TZ_CHARS = 64
MAX_JOBS = 200
MAX_RETRIES = 3
#: One-time jobs must land within this horizon (a stale year is a bug,
#: not a reminder).
MAX_ONCE_SECONDS = 5 * 365 * 24 * 3600
#: The smallest allowed distance between two occurrences of the same
#: recurring job: no high-frequency background loops, ever.
MIN_RECURRING_SECONDS = 300
#: Scan bound for finding the next occurrence (days).
NEXT_SCAN_DAYS = 400

WEEKDAY_NAMES: Tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri",
                                  "Sat", "Sun")

#: Fixed, content-free refusal reasons (never echo the input).
REASON_SECRET = ("I can't schedule that — the text looks like it contains "
                 "a password, key, token, or other secret, and secrets are "
                 "never stored in scheduled jobs.")
REASON_LONG = "That text is too long for a scheduled job."
REASON_EMPTY = "There is nothing to schedule in that."


class ScheduleError(Exception):
    """Validation failure with a fixed, bounded, user-presentable reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# Credential-shaped input refusal (mirrors companion.memory's contract)
# --------------------------------------------------------------------------- #

#: Pattern strings + flags — same value-shaped signals the memory store
#: refuses (house style: ``re.search`` with patterns, no ``re.compile``
#: call is required here but kept consistent).
_SECRET_PATTERNS: Tuple[Tuple[str, int], ...] = (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    (r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b", 0),
    (r"\bsk-[A-Za-z0-9_\-]{16,}\b", 0),
    (r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b", 0),
    (r"\bAKIA[0-9A-Z]{16}\b", 0),
    (r"\bbearer\s+[A-Za-z0-9._~+/=\-]{12,}", re.IGNORECASE),
    (r"://[^\s/:@]+:[^\s/@]+@", 0),
    (r"\b(?:password|passwd|pwd|api[_\- ]?key|secret|token|"
     r"access[_\- ]?key|credentials?)(?:\s*[:=]\s*|\s+is\s+)\S{4,}",
     re.IGNORECASE),
    (r"\b[A-Za-z0-9+/=_\-]{40,}\b", 0),
)


def secret_reason(text: str) -> Optional[str]:
    """``REASON_SECRET`` when *text* is credential-shaped, else None."""
    for pattern, flags in _SECRET_PATTERNS:
        if re.search(pattern, str(text or ""), flags):
            return REASON_SECRET
    return None


def _normalize(text: Any, limit: int, *, empty_reason: str) -> str:
    """Control chars dropped, whitespace collapsed, bounded — or refused."""
    if not isinstance(text, str):
        raise ScheduleError(empty_reason)
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    clean = " ".join(clean.split())
    if not clean:
        raise ScheduleError(empty_reason)
    if len(clean) > limit:
        raise ScheduleError(REASON_LONG)
    if secret_reason(clean) is not None:
        raise ScheduleError(REASON_SECRET)
    return clean


def validate_title(text: Any) -> str:
    """Bounded, single-line, credential-free job title."""
    return _normalize(text, MAX_TITLE_CHARS, empty_reason=REASON_EMPTY)


def validate_description(text: Any) -> str:
    """Bounded reminder text / task description (untrusted display data).

    It flows into the pipeline exactly like typed user input (same
    validation, permissions and approval), and into bubbles/dialogs as
    bounded display text — never as instructions.
    """
    return _normalize(text, MAX_TASK_CHARS, empty_reason=REASON_EMPTY)


# --------------------------------------------------------------------------- #
# Timezones
# --------------------------------------------------------------------------- #


def validate_tzid(tzid: Any) -> str:
    """Return a validated timezone identifier ("" = machine local).

    Raises :class:`ScheduleError` for anything that is not a known IANA
    identifier available on this machine — unknown zones are rejected,
    never silently coerced to UTC.
    """
    value = str(tzid or "").strip()
    if not value:
        return ""
    if len(value) > MAX_TZ_CHARS:
        raise ScheduleError("That timezone identifier is too long.")
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, KeyError):
        raise ScheduleError(
            f"'{value[:40]}' is not a timezone this system knows "
            f"(use an IANA identifier like 'Europe/Berlin', or leave it "
            f"empty for this machine's local time)."
        )
    return value


def local_tz() -> Tuple[_dt.tzinfo, str, bool]:
    """Resolve the machine's local zone.

    Returns ``(tzinfo, label, resolved)`` — ``resolved`` is False when
    only a fixed offset could be determined (in which case DST rules do
    not apply and the label says so honestly).
    """
    import os

    env_tz = os.environ.get("TZ") or ""
    if env_tz:
        try:
            tz = zoneinfo.ZoneInfo(env_tz)
            return tz, env_tz, True
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    try:
        target = Path("/etc/localtime")
        link = os.path.realpath(target)
        marker = "/usr/share/zoneinfo/"
        if marker in link:
            key = link.split(marker, 1)[1]
            tz = zoneinfo.ZoneInfo(key)
            return tz, key, True
    except (OSError, ValueError, zoneinfo.ZoneInfoNotFoundError):
        pass
    fixed = _dt.datetime.now().astimezone().tzinfo
    offset = _dt.datetime.now().astimezone().strftime("%z")
    label = f"UTC{offset[:3]}:{offset[3:]} (fixed offset, no DST rules)"
    return fixed or _dt.timezone.utc, label, False


def tz_for(tzid: str) -> _dt.tzinfo:
    """ZoneInfo for an explicit id, or the machine's local zone for ``""``."""
    if tzid:
        try:
            return zoneinfo.ZoneInfo(tzid)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, KeyError):
            raise ScheduleError(
                f"'{str(tzid)[:40]}' is not a timezone this system knows.")
    return local_tz()[0]


def tz_label(tzid: str) -> str:
    """Human label for notices/UI (resolved local zone when ``""``)."""
    if tzid:
        return tzid
    return local_tz()[1]


# --------------------------------------------------------------------------- #
# Schedule model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Schedule:
    """A validated schedule — structured fields only, no strings to run.

    * ``once``: ``at_utc`` is a UTC instant (unix seconds); the other
      fields are unused.
    * ``daily``: ``hour``/``minute`` local wall time every day.
    * ``weekly``: ``hour``/``minute`` local wall time on ``weekdays``
      (Python convention: Monday=0 … Sunday=6, sorted, unique).
    * ``tzid``: IANA identifier or ``""`` = machine-local zone.
    """

    kind: str
    at_utc: Optional[int] = None
    hour: int = 0
    minute: int = 0
    weekdays: Tuple[int, ...] = ()
    tzid: str = ""

    @property
    def is_recurring(self) -> bool:
        return self.kind in ("daily", "weekly")

    @property
    def period_seconds(self) -> int:
        """Nominal distance between occurrences (0 for one-time)."""
        if self.kind == "daily":
            return 24 * 3600
        if self.kind == "weekly":
            return 7 * 24 * 3600
        return 0

    def validate(self, *, now: Optional[int] = None) -> "Schedule":
        """Reject malformed schedules; return self for chaining."""
        if self.kind not in SCHEDULE_KINDS:
            raise ScheduleError(
                f"Unknown schedule kind (supported: "
                f"{', '.join(SCHEDULE_KINDS)}).")
        validate_tzid(self.tzid)
        if self.kind == "once":
            if self.at_utc is None or not isinstance(self.at_utc, int):
                raise ScheduleError(
                    "A one-time schedule needs a specific date and time.")
            if now is not None:
                if self.at_utc < now:
                    raise ScheduleError(
                        "That date and time is in the past.")
                if self.at_utc > now + MAX_ONCE_SECONDS:
                    raise ScheduleError(
                        "One-time reminders may be scheduled at most "
                        "5 years ahead.")
            return self
        # daily / weekly: wall time + weekday validation
        if not isinstance(self.hour, int) or not 0 <= self.hour <= 23:
            raise ScheduleError("Hour must be between 0 and 23.")
        if not isinstance(self.minute, int) or not 0 <= self.minute <= 59:
            raise ScheduleError("Minute must be between 0 and 59.")
        if self.at_utc is not None:
            raise ScheduleError("Only one-time schedules carry a date.")
        if self.kind == "weekly":
            days = tuple(self.weekdays)
            if not days or len(days) > 7:
                raise ScheduleError(
                    "A weekly schedule needs 1–7 weekdays.")
            if any(not isinstance(d, int) or not 0 <= d <= 6
                   for d in days):
                raise ScheduleError(
                    "Weekdays must be numbers 0–6 (Monday–Sunday).")
            if tuple(sorted(set(days))) != days:
                raise ScheduleError(
                    "Weekdays must be sorted and unique.")
        elif self.weekdays:
            raise ScheduleError(
                "Daily schedules do not carry weekdays.")
        if MIN_RECURRING_SECONDS > 24 * 3600 and self.kind == "daily":
            raise ScheduleError("Internal frequency bound violated.")
        return self


def make_schedule(kind: str, *, now: Optional[int] = None,
                  at_utc: Optional[int] = None, hour: int = 0,
                  minute: int = 0, weekdays: Tuple[int, ...] = (),
                  tzid: str = "") -> Schedule:
    """Build and validate a :class:`Schedule` (the only supported way)."""
    schedule = Schedule(
        kind=str(kind or ""),
        at_utc=at_utc,
        hour=int(hour),
        minute=int(minute),
        weekdays=tuple(sorted(set(int(d) for d in weekdays))),
        tzid=str(tzid or "").strip(),
    )
    return schedule.validate(now=now)


# --------------------------------------------------------------------------- #
# Conditional checks (Phase 6 — closed set)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConditionSpec:
    """One bounded conditional check: a closed op + one argument.

    * ``file_exists`` / ``file_updated``: argument is a path, validated
      against the permitted roots by the scheduler's real
      ``PermissionChecker`` before any ``stat`` (never a scan).
    * ``app_installed``: argument is a registered application id
      checked against the real capability registry.

    ``file_updated`` compares the file's mtime against the job's own
    ``last_success`` (or creation time) — no history of file versions
    is kept, nothing is copied.
    """

    op: str
    argument: str

    def validate(self) -> "ConditionSpec":
        if self.op not in CONDITION_OPS:
            raise ScheduleError(
                f"Unknown check (supported: {', '.join(CONDITION_OPS)}).")
        # Paths get their own normalization: they legitimately contain
        # long slash-joined tokens, so the credential scanner (which
        # flags 40+ char secret-shaped blobs) must not apply here —
        # the real defense for a path is the permitted-roots check the
        # scheduler performs before any stat.
        if not isinstance(self.argument, str):
            raise ScheduleError("The check needs a target.")
        clean = re.sub(r"[\x00-\x1f\x7f]", " ", self.argument)
        clean = " ".join(clean.split())
        if not clean:
            raise ScheduleError("The check needs a target.")
        if len(clean) > MAX_CONDITION_CHARS:
            raise ScheduleError(REASON_LONG)
        return ConditionSpec(self.op, clean)


def make_condition(op: str, argument: str) -> ConditionSpec:
    return ConditionSpec(str(op or ""), str(argument or "")).validate()


# --------------------------------------------------------------------------- #
# Job model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScheduledJob:
    """A typed, validated scheduled job (every field bounded)."""

    job_id: str
    title: str
    job_type: str                      # reminder | task | check
    schedule: Schedule
    description: str                   # reminder text / task description
    state: str                         # enabled | paused | cancelled
    created_at: int
    updated_at: int
    next_occurrence: Optional[int] = None
    last_attempt: Optional[int] = None
    last_success: Optional[int] = None
    last_outcome: str = ""
    approval: str = ""                 # APPROVAL_VALUES (latest attempt)
    project: str = ""                  # explicit scope label ("" = global)
    condition: Optional[ConditionSpec] = None
    required_capabilities: Tuple[str, ...] = ()
    max_retries: int = 0               # bounded retry policy (0..3)
    retries_used: int = 0
    retry_occurrence: Optional[int] = None

    @property
    def is_due(self) -> bool:  # pragma: no cover - trivial helper
        return (self.state == "enabled" and self.next_occurrence is not None)

    def validate(self, *, now: Optional[int] = None) -> "ScheduledJob":
        """Full job validation — reject anything malformed."""
        if not self.job_id.startswith("job_") or len(self.job_id) != 20:
            raise ScheduleError("Malformed job identifier.")
        validate_title(self.title)
        # A check's follow-up task is OPTIONAL (empty = notify only);
        # reminders and tasks always carry their text.
        if self.job_type == "check" and not str(self.description).strip():
            pass
        else:
            validate_description(self.description)
        if self.job_type not in JOB_TYPES:
            raise ScheduleError(
                f"Unknown job type (supported: {', '.join(JOB_TYPES)}).")
        if self.state not in JOB_STATES:
            raise ScheduleError("Unknown job state.")
        self.schedule.validate(now=now)
        if self.job_type == "check" and self.condition is None:
            raise ScheduleError("A check job needs a condition.")
        if self.job_type != "check" and self.condition is not None:
            raise ScheduleError("Only check jobs carry a condition.")
        if self.job_type == "task" and self.schedule.is_recurring is False \
                and self.condition is not None:
            raise ScheduleError("Only check jobs carry a condition.")
        if len(str(self.project)) > MAX_PROJECT_CHARS:
            raise ScheduleError("Project scope is too long.")
        if not isinstance(self.max_retries, int) \
                or not 0 <= self.max_retries <= MAX_RETRIES:
            raise ScheduleError(
                f"Retries must be between 0 and {MAX_RETRIES}.")
        if not isinstance(self.retries_used, int) \
                or self.retries_used < 0:
            raise ScheduleError("Malformed retry counter.")
        if len(self.required_capabilities) > MAX_CAPABILITIES:
            raise ScheduleError("Too many required capabilities.")
        for cap in self.required_capabilities:
            if not isinstance(cap, str) or not cap.strip() \
                    or len(cap) > MAX_CAPABILITY_CHARS:
                raise ScheduleError("Malformed capability entry.")
        if self.approval not in APPROVAL_VALUES:
            raise ScheduleError("Unknown approval status.")
        if len(self.last_outcome) > MAX_OUTCOME_CHARS:
            raise ScheduleError("Outcome text is too long.")
        if self.condition is not None:
            self.condition.validate()
        return self


def new_job_id() -> str:
    """Stable job identifier (``job_`` + 16 hex), like memory's ids."""
    return f"job_{uuid.uuid4().hex[:16]}"


def make_job(
    title: str,
    description: str,
    *,
    job_type: str,
    schedule: Schedule,
    now: int,
    state: str = "enabled",
    project: str = "",
    condition: Optional[ConditionSpec] = None,
    required_capabilities: Tuple[str, ...] = (),
    max_retries: int = 0,
    job_id: Optional[str] = None,
) -> ScheduledJob:
    """Validate and assemble a new job (timestamps = *now*)."""
    description_text = str(description or "")
    if job_type == "check" and not description_text.strip():
        normalized_description = ""      # notify-only check: allowed
    else:
        normalized_description = validate_description(description)
    job = ScheduledJob(
        job_id=job_id or new_job_id(),
        title=validate_title(title),
        job_type=str(job_type or ""),
        schedule=schedule,
        description=normalized_description,
        state=str(state or "enabled"),
        created_at=int(now),
        updated_at=int(now),
        project=str(project or "")[:MAX_PROJECT_CHARS],
        condition=condition,
        required_capabilities=tuple(str(c) for c in required_capabilities),
        max_retries=int(max_retries),
    )
    job.validate(now=now)
    return job


def validate_outcome(text: Any) -> str:
    """Bound the trusted outcome string (single line, ≤ MAX_OUTCOME_CHARS)."""
    clean = " ".join(str(text or "").split())
    if len(clean) > MAX_OUTCOME_CHARS:
        return clean[:MAX_OUTCOME_CHARS - 1] + "…"
    return clean


# --------------------------------------------------------------------------- #
# Deterministic next-occurrence computation
# --------------------------------------------------------------------------- #


def _wall_epoch(hour: int, minute: int, day: _dt.date, tz: _dt.tzinfo
                ) -> int:
    """Epoch seconds of *day* at *hour*:*minute* in *tz* (fold=0 rules).

    fold=0 gives the documented DST behaviour: ambiguous wall times use
    the pre-transition offset (first occurrence), nonexistent wall times
    resolve to the shifted local time that actually exists.
    """
    naive = _dt.datetime(day.year, day.month, day.day, hour, minute)
    return int(naive.replace(tzinfo=tz).timestamp())


def next_occurrence(schedule: Schedule, *, after: int,
                    tz: Optional[_dt.tzinfo] = None) -> Optional[int]:
    """First occurrence strictly after *after* (unix seconds), or None.

    Deterministic: same inputs, same output — no wall-clock reads inside
    the scan (callers pass their injected now).  One-time schedules
    return their instant when it is still in the future; recurring
    schedules scan local dates (bounded by ``NEXT_SCAN_DAYS``) so DST
    transitions cannot skip or double a day.
    """
    if schedule.kind == "once":
        if schedule.at_utc is None:
            return None
        return schedule.at_utc if schedule.at_utc > after else None

    zone = tz if tz is not None else tz_for(schedule.tzid)
    start = _dt.datetime.fromtimestamp(after, zone).date()
    for offset in range(NEXT_SCAN_DAYS):
        day = start + _dt.timedelta(days=offset)
        if schedule.kind == "weekly" and day.weekday() not in schedule.weekdays:
            continue
        epoch = _wall_epoch(schedule.hour, schedule.minute, day, zone)
        if epoch > after:
            return epoch
    return None


def missed_count(schedule: Schedule, *, occurrence: int, now: int,
                 tz: Optional[_dt.tzinfo] = None) -> int:
    """How many occurrences passed between *occurrence* and *now*.

    Bounded scan (``NEXT_SCAN_DAYS``) — used to report coalesced missed
    runs honestly instead of replaying them.
    """
    if not schedule.is_recurring or occurrence >= now:
        return 0
    count = 0
    cursor = occurrence
    while count < NEXT_SCAN_DAYS:
        nxt = next_occurrence(schedule, after=cursor, tz=tz)
        if nxt is None or nxt > now:
            break
        cursor = nxt
        count += 1
    return count


# --------------------------------------------------------------------------- #
# Presentation (bounded summaries for notices, dialogs and status lines)
# --------------------------------------------------------------------------- #


def format_schedule(schedule: Schedule) -> str:
    """One-line human summary of the interpreted schedule (no content)."""
    if schedule.kind == "once":
        when = _dt.datetime.fromtimestamp(
            int(schedule.at_utc or 0),
            tz_for(schedule.tzid)).strftime("%Y-%m-%d %H:%M")
        return f"once at {when} ({tz_label(schedule.tzid)})"
    clock = f"{schedule.hour:02d}:{schedule.minute:02d}"
    zone = tz_label(schedule.tzid)
    if schedule.kind == "daily":
        return f"every day at {clock} ({zone})"
    days = ",".join(WEEKDAY_NAMES[d] for d in schedule.weekdays)
    return f"every {days} at {clock} ({zone})"


def validate_capabilities(raw: Any) -> Tuple[str, ...]:
    """Bounded, deduplicated capability hint list (informational only —
    the pipeline re-checks capabilities itself before every run)."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        items = [raw]
    else:
        items = list(raw)
    out = []
    for item in items:
        cap = str(item).strip()
        if not cap or len(cap) > MAX_CAPABILITY_CHARS:
            raise ScheduleError("Malformed capability entry.")
        if cap not in out:
            out.append(cap)
    if len(out) > MAX_CAPABILITIES:
        raise ScheduleError("Too many required capabilities.")
    return tuple(out)


def job_to_row(job: ScheduledJob) -> Dict[str, Any]:
    """Flat, store-friendly dict (schedule fields inline, no nesting)."""
    return {
        "job_id": job.job_id,
        "title": job.title,
        "job_type": job.job_type,
        "kind": job.schedule.kind,
        "at_utc": job.schedule.at_utc,
        "hour": job.schedule.hour,
        "minute": job.schedule.minute,
        "weekdays": ",".join(str(d) for d in job.schedule.weekdays),
        "tzid": job.schedule.tzid,
        "description": job.description,
        "state": job.state,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "next_occurrence": job.next_occurrence,
        "last_attempt": job.last_attempt,
        "last_success": job.last_success,
        "last_outcome": job.last_outcome,
        "approval": job.approval,
        "project": job.project,
        "condition_op": job.condition.op if job.condition else "",
        "condition_arg": job.condition.argument if job.condition else "",
        "capabilities": ",".join(job.required_capabilities),
        "max_retries": job.max_retries,
        "retries_used": job.retries_used,
        "retry_occurrence": job.retry_occurrence,
    }


def job_from_row(row: Dict[str, Any]) -> ScheduledJob:
    """Rebuild a validated job from flat store columns."""
    weekdays = tuple(
        int(d) for d in str(row.get("weekdays") or "").split(",")
        if d != ""
    )
    schedule = Schedule(
        kind=str(row.get("kind") or ""),
        at_utc=(None if row.get("at_utc") in (None, "")
                else int(row["at_utc"])),
        hour=int(row.get("hour") or 0),
        minute=int(row.get("minute") or 0),
        weekdays=weekdays,
        tzid=str(row.get("tzid") or ""),
    )
    condition = None
    if str(row.get("condition_op") or ""):
        condition = ConditionSpec(str(row["condition_op"]),
                                  str(row.get("condition_arg") or ""))
    capabilities = tuple(
        c for c in str(row.get("capabilities") or "").split(",") if c
    )
    job = ScheduledJob(
        job_id=str(row.get("job_id") or ""),
        title=str(row.get("title") or ""),
        job_type=str(row.get("job_type") or ""),
        schedule=schedule,
        description=str(row.get("description") or ""),
        state=str(row.get("state") or ""),
        created_at=int(row.get("created_at") or 0),
        updated_at=int(row.get("updated_at") or 0),
        next_occurrence=(None if row.get("next_occurrence") in (None, "")
                         else int(row["next_occurrence"])),
        last_attempt=(None if row.get("last_attempt") in (None, "")
                      else int(row["last_attempt"])),
        last_success=(None if row.get("last_success") in (None, "")
                      else int(row["last_success"])),
        last_outcome=str(row.get("last_outcome") or ""),
        approval=str(row.get("approval") or ""),
        project=str(row.get("project") or ""),
        condition=condition,
        required_capabilities=capabilities,
        max_retries=int(row.get("max_retries") or 0),
        retries_used=int(row.get("retries_used") or 0),
        retry_occurrence=(None if row.get("retry_occurrence") in (None, "")
                          else int(row["retry_occurrence"])),
    )
    # Never trust the file: a tampered row is refused honestly by the
    # same validator new jobs go through (no schema downgrade, no
    # best-effort coercion of unknown values).
    job.validate()
    return job


__all__ = [
    "APPROVAL_VALUES",
    "CONDITION_OPS",
    "JOB_STATES",
    "JOB_TYPES",
    "MAX_JOBS",
    "MAX_ONCE_SECONDS",
    "MAX_RETRIES",
    "MAX_TASK_CHARS",
    "MAX_TITLE_CHARS",
    "MIN_RECURRING_SECONDS",
    "REASON_EMPTY",
    "REASON_LONG",
    "REASON_SECRET",
    "SCHEDULE_KINDS",
    "WEEKDAY_NAMES",
    "ConditionSpec",
    "Schedule",
    "ScheduleError",
    "ScheduledJob",
    "format_schedule",
    "job_from_row",
    "job_to_row",
    "local_tz",
    "make_condition",
    "make_job",
    "make_schedule",
    "missed_count",
    "new_job_id",
    "next_occurrence",
    "secret_reason",
    "tz_for",
    "tz_label",
    "validate_capabilities",
    "validate_description",
    "validate_outcome",
    "validate_title",
    "validate_tzid",
]
