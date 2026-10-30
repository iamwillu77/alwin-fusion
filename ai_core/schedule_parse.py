"""Deterministic English grammar for scheduling commands (v0.16.0).

A closed-set parser in the same tradition as
:mod:`ai_core.command_parser`: the language model may help the user
*phrase* a request, but the resulting job is always produced and
validated by this deterministic code — the parser itself writes
nothing; the caller decides what to persist after seeing the
interpretation.

Supported intents (nothing else is recognised):

* ``create_reminder`` / ``create_task`` — a time clause plus text,
* ``list`` / ``inspect`` / ``pause`` / ``resume`` / ``cancel`` /
  ``run_now`` — operate on existing jobs by title words or id,
* ``question`` — questions NEVER create anything; they are answered
  from current state by the caller,
* ``none`` — not a scheduling utterance at all (route it onward).

Ambiguity contract (tested): a materially ambiguous request (no time of
day, a bare hour like "at 9", a missing day for a dated reminder, a
recurring request that could plausibly be one-shot) comes back with
``needs_confirmation=True`` and a bounded ``interpretation`` string —
the caller must show it and get an explicit confirmation **before**
anything is stored.  Unambiguous one-time requests may be stored
immediately; their creation notice still shows the interpreted
schedule.

Frequency contract: recurring schedules are subject to
``MIN_RECURRING_SECONDS`` (validated later by
:mod:`ai_core.schedule`); phrases like "check this regularly",
"every now and then", "constantly" deliberately do not parse into any
schedule — they come back as ``none``/``question`` so they can never
become high-frequency background jobs.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ai_core.schedule import (
    Schedule,
    ScheduleError,
    format_schedule,
    local_tz,
    make_schedule,
    tz_label,
    validate_tzid,
)

INTENT_CREATE_REMINDER = "create_reminder"
INTENT_CREATE_TASK = "create_task"
INTENT_LIST = "list"
INTENT_INSPECT = "inspect"
INTENT_PAUSE = "pause"
INTENT_RESUME = "resume"
INTENT_CANCEL = "cancel"
INTENT_RUN_NOW = "run_now"
INTENT_EDIT = "edit"
INTENT_QUESTION = "question"
INTENT_NONE = "none"

#: Question openers — an utterance starting with one of these (or a
#: trailing "?") is a question and can never create a job.
_QUESTION_OPENERS: Tuple[str, ...] = (
    "what", "when", "where", "which", "who", "why", "how", "can i",
    "could i", "do i", "does", "did", "is there", "are there", "is my",
    "are my", "show me what", "will", "would", "should", "any ",
)

_WEEKDAY_INDEX = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3,
    "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

#: Vague-frequency phrases that must never become a schedule.
_VAGUE = (
    "regularly", "constantly", "periodically", "from time to time",
    "every now and then", "all the time", "every so often", "often",
    "continuously", "non stop", "nonstop", "around the clock",
)

_MAX_TEXT_CHARS = 500


@dataclass
class ScheduleIntent:
    """What the parser deterministically understood (or refused)."""

    intent: str
    query: str = ""                    # words identifying an existing job
    title: str = ""
    description: str = ""
    kind: str = ""                     # once | daily | weekly
    at_utc: Optional[int] = None
    hour: int = 0
    minute: int = 0
    weekdays: Tuple[int, ...] = ()
    tzid: str = ""
    day_hint: str = ""                 # today | tomorrow | weekday | iso
    needs_confirmation: bool = False
    interpretation: str = ""
    reason: str = ""                   # refusal / clarification text
    extras: Dict[str, Any] = field(default_factory=dict)

    @property
    def creates(self) -> bool:
        return self.intent in (INTENT_CREATE_REMINDER, INTENT_CREATE_TASK)

    @property
    def question(self) -> bool:
        return self.intent == INTENT_QUESTION

    def schedule(self, *, now: int) -> Schedule:
        """Build the validated :class:`Schedule` this intent implies."""
        if self.kind == "once":
            return make_schedule("once", at_utc=self.at_utc, now=now,
                                 tzid=self.tzid)
        return make_schedule(self.kind, hour=self.hour,
                             minute=self.minute, weekdays=self.weekdays,
                             tzid=self.tzid)


class _Cursor:
    """Tiny helper for ordered, optional phrase consumption."""

    def __init__(self, words: List[str]) -> None:
        self.words = words

    def drop(self, *options: str) -> bool:
        if self.words and self.words[0] in options:
            self.words.pop(0)
            return True
        return False

    def drop_many(self, *options: str) -> bool:
        found = False
        while self.drop(*options):
            found = True
        return found


def _clean(text: str) -> str:
    clean = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    return " ".join(clean.split()).strip(" ?!.,")


def _words(text: str) -> List[str]:
    return re.findall(r"[\w']+", text.lower())


# --------------------------------------------------------------------------- #
# Time clause: "at 9 am" / "at 09:30" / "at 14:00" / "at 9" (ambiguous)
# --------------------------------------------------------------------------- #


_TIME_RE = re.compile(
    r"\bat\s+(?:(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*"
    r"(?P<ampm1>a\.?m\.?|p\.?m\.?)?"
    r"|(?P<h2>\d{1,2}):(?P<m2>\d{2}))\b", re.IGNORECASE)


@dataclass
class _TimeMatch:
    hour: int
    minute: int
    ambiguous: bool


def _parse_time(fragment: str) -> Optional[_TimeMatch]:
    match = _TIME_RE.search(fragment)
    if not match:
        return None
    if match.group("h2") is not None:
        hour, minute = int(match.group("h2")), int(match.group("m2"))
        ampm = None
    else:
        hour, minute = int(match.group("h1")), int(match.group("m1") or 0)
        ampm = (match.group("ampm1") or "").replace(".", "").lower() or None
    if ampm:
        if hour < 1 or hour > 12:
            return None
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        ambiguous = False
    else:
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        # A bare hour 1..12 with no am/pm is materially ambiguous.
        ambiguous = 1 <= hour <= 12
    return _TimeMatch(hour, minute, ambiguous)


# --------------------------------------------------------------------------- #
# Day clause: today | tomorrow | [next] <weekday> | on YYYY-MM-DD
# --------------------------------------------------------------------------- #


@dataclass
class _DayMatch:
    kind: str            # today | tomorrow | weekday | iso | none
    weekday: int = -1
    iso: str = ""


_ISO_RE = re.compile(r"\bon\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)


def _parse_day(text: str) -> _DayMatch:
    lowered = text.lower()
    if re.search(r"\btoday\b", lowered):
        return _DayMatch("today")
    if re.search(r"\btomorrow\b", lowered):
        return _DayMatch("tomorrow")
    iso = _ISO_RE.search(text)
    if iso:
        try:
            _dt.date.fromisoformat(iso.group(1))
            return _DayMatch("iso", iso=iso.group(1))
        except ValueError:
            pass
    match = re.search(r"\b(?:on\s+|next\s+)?("
                      + "|".join(sorted(_WEEKDAY_INDEX, key=len,
                                        reverse=True)) + r")\b", lowered)
    if match:
        return _DayMatch("weekday", weekday=_WEEKDAY_INDEX[match.group(1)])
    return _DayMatch("none")


def _resolve_day(day: _DayMatch, *, now: int, tz) -> Tuple[Optional[_dt.date],
                                                           bool]:
    """``(date, ambiguous)`` for a one-time schedule in *tz*."""
    local = _dt.datetime.fromtimestamp(now, tz).date()
    if day.kind == "today":
        return local, False
    if day.kind == "tomorrow":
        return local + _dt.timedelta(days=1), False
    if day.kind == "iso":
        try:
            return _dt.date.fromisoformat(day.iso), False
        except ValueError:  # pragma: no cover - pre-validated
            return None, True
    if day.kind == "weekday":
        target = (local + _dt.timedelta(
            days=(day.weekday - local.weekday()) % 7 or 7))
        return target, False
    # No day at all: "remind me at 10" is materially ambiguous.
    return None, True


# --------------------------------------------------------------------------- #
# Creation intents
# --------------------------------------------------------------------------- #


def _split_time_text(text: str) -> Optional[str]:
    """Reminder/task text = everything after 'to' following the time."""
    match = re.search(r"\bto\s+(.+)$", text, re.IGNORECASE)
    if match:
        body = match.group(1).strip()
    else:
        # Fall back: everything after the time clause that is not the
        # day/time words themselves.
        body = _TIME_RE.sub(" ", text, count=1)
        body = re.sub(
            r"^(?:please\s+)?(?:remind\s+me|set\s+a\s+reminder|"
            r"create\s+a\s+reminder|schedule|plan)\s*(?:a\s+)?"
            r"(?:task|job|reminder)?\s*", " ", body,
            flags=re.IGNORECASE)
        body = re.sub(
            r"\b(today|tomorrow|yesterday|on|next|every|day|at|"
            r"a\.?m\.?|p\.?m\.?|\d{4}-\d{2}-\d{2}|"
            + "|".join(_WEEKDAY_INDEX) + r")\b",
            " ", body, flags=re.IGNORECASE)
        body = " ".join(body.split())
    if not body:
        return None
    if len(body) > _MAX_TEXT_CHARS:
        return body[:_MAX_TEXT_CHARS]
    return body[0].upper() + body[1:]


def _creation(text: str, phrase: str, *, job_type: str, now: int,
              tzid: str) -> ScheduleIntent:
    intent = INTENT_CREATE_REMINDER if job_type == "reminder" \
        else INTENT_CREATE_TASK
    lowered = text.lower()

    # Recurrence clauses ------------------------------------------------ #
    if re.search(r"\bevery\s+day\b|\bdaily\b", lowered):
        time_match = _parse_time(text)
        if time_match is None:
            return ScheduleIntent(
                intent=INTENT_NONE,
                reason="When during the day? Say e.g. 'every day at 9 AM'.")
        body = _split_time_text(text) or ""
        schedule = make_schedule("daily", hour=time_match.hour,
                                 minute=time_match.minute, tzid=tzid)
        title = body[:80] if body else "Daily reminder"
        return ScheduleIntent(
            intent=intent, title=title, description=body or title,
            kind="daily", hour=time_match.hour,
            minute=time_match.minute, tzid=tzid,
            needs_confirmation=True,
            interpretation=f"I understood: {intent.split('_')[1]} "
                           f"'{body}' {format_schedule(schedule)}. "
                           f"Confirm to save.")

    weekday_clause = re.search(
        r"\bevery\s+((?:" + "|".join(_WEEKDAY_INDEX) + r")"
        r"(?:\s+and\s+(?:" + "|".join(_WEEKDAY_INDEX) + r"))*)\b",
        lowered)
    if weekday_clause:
        time_match = _parse_time(text)
        if time_match is None:
            return ScheduleIntent(
                intent=INTENT_NONE,
                reason=f"When on {weekday_clause.group(1)}s? Say e.g. "
                       f"'every Monday at 9 AM'.")
        body = _split_time_text(text) or ""
        words = re.findall(r"[a-z]+", weekday_clause.group(1))
        days = tuple(sorted({_WEEKDAY_INDEX[w] for w in words
                             if w in _WEEKDAY_INDEX}))
        if not days:
            return ScheduleIntent(
                intent=INTENT_NONE,
                reason="Which weekday? Say e.g. 'every Monday at 9 AM'.")
        schedule = make_schedule("weekly", hour=time_match.hour,
                                 minute=time_match.minute, weekdays=days,
                                 tzid=tzid)
        title = body[:80] if body else "Weekly reminder"
        return ScheduleIntent(
            intent=intent, title=title, description=body or title,
            kind="weekly", hour=time_match.hour,
            minute=time_match.minute, weekdays=days, tzid=tzid,
            needs_confirmation=True,
            interpretation=f"I understood: {intent.split('_')[1]} "
                           f"'{body}' {format_schedule(schedule)}. "
                           f"Confirm to save.")

    # One-time clause --------------------------------------------------- #
    if re.search(r"\byesterday\b", lowered):
        return ScheduleIntent(
            intent=INTENT_NONE,
            reason="Yesterday is in the past — give a future day, e.g. "
                   "'tomorrow at 10 AM'.")
    time_match = _parse_time(text)
    day = _parse_day(text)
    body = _split_time_text(text) or ""
    if time_match is None and day.kind == "none":
        return ScheduleIntent(
            intent=INTENT_NONE,
            reason="When should it happen? Give a day and time, e.g. "
                   "'remind me tomorrow at 10 AM …'.")
    ambiguous = False
    notes: List[str] = []
    resolved_tz = tzid
    from ai_core.schedule import tz_for
    zone = tz_for(tzid)
    if time_match is None:
        return ScheduleIntent(
            intent=INTENT_NONE,
            reason="What time on that day? Say e.g. 'at 10 AM'.")
    if time_match.ambiguous:
        ambiguous = True
        shown = f"{time_match.hour:02d}:{time_match.minute:02d}"
        notes.append(f"'at {time_match.hour}' is ambiguous — "
                     f"interpreting as {shown} (24-hour)")
    target_date, day_ambiguous = _resolve_day(day, now=now, tz=zone)
    if day_ambiguous:
        ambiguous = True
        target_date = _dt.datetime.fromtimestamp(now, zone).date()
        notes.append("no day given — interpreting as today")
    assert target_date is not None
    naive = _dt.datetime(target_date.year, target_date.month,
                         target_date.day, time_match.hour,
                         time_match.minute)
    at_utc = int(naive.replace(tzinfo=zone).timestamp())
    if at_utc <= now:
        # "at 10" with no day, already past → tomorrow, flagged.
        if day.kind == "none":
            naive += _dt.timedelta(days=1)
            at_utc = int(naive.replace(tzinfo=zone).timestamp())
            notes.append("that time has passed today — interpreting as "
                         "tomorrow")
            ambiguous = True
        else:
            return ScheduleIntent(
                intent=INTENT_NONE,
                reason="That date and time is in the past.")
    schedule = make_schedule("once", at_utc=at_utc, now=now,
                             tzid=resolved_tz)
    title = body[:80] if body else f"{job_type} at {naive:%H:%M}"
    interpretation = f"I understood: one-time {job_type} '{body}' " \
                     f"{format_schedule(schedule)}."
    if notes:
        interpretation += " Note: " + "; ".join(notes) + "."
    if ambiguous:
        interpretation += " Confirm to save."
    return ScheduleIntent(
        intent=intent, title=title, description=body or title,
        kind="once", at_utc=at_utc, tzid=resolved_tz,
        needs_confirmation=ambiguous, interpretation=interpretation)


# --------------------------------------------------------------------------- #
# Management intents
# --------------------------------------------------------------------------- #


def _query_after(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return ""
    tail = text[match.end():]
    tail = re.sub(r"^(about|for|named|called|called)\s+", "", tail,
                  flags=re.IGNORECASE)
    return _clean(tail)


_LIST_RE = re.compile(
    r"^\s*(?:please\s+)?(?:show|list|view|what are)\b.*\b"
    r"(?:scheduled|jobs|reminders|tasks|upcoming|schedule)\b", re.I)
_LIST_RE2 = re.compile(
    r"^\s*(?:show|list)\s+my\b", re.I)


def parse(text: str, *, now: int, tzid: str = "") -> ScheduleIntent:
    """Parse one utterance into a deterministic :class:`ScheduleIntent`.

    *never* raises for user text: malformed or unsupported input comes
    back as ``none``/``question``/``reason`` so the caller can answer
    honestly instead of guessing a consequential schedule.
    """
    try:
        validated_tz = validate_tzid(tzid)
    except ScheduleError as exc:
        return ScheduleIntent(intent=INTENT_NONE, reason=exc.reason)
    original = _clean(text)
    if not original:
        return ScheduleIntent(intent=INTENT_NONE)
    lowered = original.lower()

    # Questions first: they must never create anything. --------------- #
    if original.endswith("?") or any(lowered.startswith(opener)
                                     for opener in _QUESTION_OPENERS):
        return ScheduleIntent(intent=INTENT_QUESTION, query=original)

    # Vague frequency can never become a schedule. -------------------- #
    if any(phrase in lowered for phrase in _VAGUE):
        return ScheduleIntent(
            intent=INTENT_QUESTION,
            reason="I can't turn vague phrases like 'check this "
                   "regularly' into a schedule — tell me an explicit "
                   "frequency (e.g. 'every day at 9 AM') and I'll show "
                   "you the interpretation first.")

    # Reminder creation. ---------------------------------------------- #
    if re.match(r"^(?:please\s+)?remind\s+me\b", lowered) or \
            re.match(r"^(?:please\s+)?set\s+a\s+reminder\b", lowered) \
            or re.match(r"^(?:please\s+)?create\s+a\s+reminder\b",
                        lowered):
        result = _creation(original, "remind me", job_type="reminder",
                           now=now, tzid=validated_tz)
        if result.intent != INTENT_NONE or result.reason:
            return result

    # Scheduled task creation. ---------------------------------------- #
    if re.match(r"^(?:please\s+)?(?:schedule|plan)\s+(?:a\s+)?"
                r"(?:task|job)\b", lowered) or \
            re.match(r"^(?:please\s+)?(?:schedule|plan)\s+.*\bto\s+",
                     lowered):
        result = _creation(original, "schedule a task",
                           job_type="task", now=now, tzid=validated_tz)
        if result.intent != INTENT_NONE or result.reason:
            return result

    # Management. ------------------------------------------------------ #
    if re.match(r"^(?:please\s+)?(?:pause|hold|stop)\b", lowered):
        query = _query_after(original, r"\b(?:pause|hold|stop)\b")
        if query:
            return ScheduleIntent(intent=INTENT_PAUSE, query=query)
    if re.match(r"^(?:please\s+)?(?:resume|restart|unpause)\b", lowered):
        query = _query_after(original, r"\b(?:resume|restart|unpause)\b")
        if query:
            return ScheduleIntent(intent=INTENT_RESUME, query=query)
    if re.match(r"^(?:please\s+)?(?:cancel|delete|remove|forget)\b",
                lowered):
        query = _query_after(original,
                             r"\b(?:cancel|delete|remove|forget)\b")
        if query:
            return ScheduleIntent(intent=INTENT_CANCEL, query=query)
    if re.match(r"^(?:please\s+)?run\b.*\bnow\b", lowered) or \
            re.match(r"^(?:please\s+)?run\s+(?:the\s+)?", lowered):
        query = _query_after(original, r"\b(?:run)\b")
        query = re.sub(r"\bnow\b", "", query, flags=re.IGNORECASE).strip()
        if query:
            return ScheduleIntent(intent=INTENT_RUN_NOW, query=query)
    # Editing: "reschedule the report reminder to tomorrow at 9 AM" —
    # the query identifies the job; an explicit 'to <time clause>'
    # carries the replacement schedule (still validated, still shown
    # for confirmation by the caller when ambiguous).
    if re.match(r"^(?:please\s+)?(?:edit|change|reschedule|move)\b",
                lowered):
        split = re.split(r"\s+to\s+", original, maxsplit=1,
                         flags=re.IGNORECASE)
        query = _query_after(split[0],
                             r"\b(?:edit|change|reschedule|move)\b")
        if len(split) == 2:
            replacement = _creation("remind me " + split[1],
                                   "edit", job_type="reminder",
                                   now=now, tzid=validated_tz)
            if replacement.creates:
                return ScheduleIntent(
                    intent=INTENT_EDIT, query=query,
                    kind=replacement.kind, at_utc=replacement.at_utc,
                    hour=replacement.hour, minute=replacement.minute,
                    weekdays=replacement.weekdays,
                    tzid=replacement.tzid,
                    needs_confirmation=replacement.needs_confirmation,
                    interpretation=replacement.interpretation)
            return ScheduleIntent(
                intent=INTENT_EDIT, query=query,
                reason=replacement.reason or
                "Give the new time, e.g. 'reschedule … to tomorrow "
                "at 9 AM'.")
        if query:
            return ScheduleIntent(
                intent=INTENT_EDIT, query=query,
                reason="What should change? Give a new time, e.g. "
                       "'reschedule … to tomorrow at 9 AM'.")
    if re.match(r"^(?:please\s+)?(?:inspect|details|status|open)\b",
                lowered):
        query = _query_after(original,
                             r"\b(?:inspect|details|status|open)\b")
        return ScheduleIntent(intent=INTENT_INSPECT, query=query)
    if _LIST_RE.match(lowered) or _LIST_RE2.match(lowered):
        return ScheduleIntent(intent=INTENT_LIST, query=original)

    # A reminder-shaped request that failed creation parsing still
    # means the user wants scheduling help — answer, don't guess.
    if "remind" in lowered or "schedule" in lowered or \
            "reminder" in lowered:
        return ScheduleIntent(
            intent=INTENT_QUESTION,
            reason="Tell me when: e.g. 'remind me tomorrow at 10 AM to "
                   "review the report', or 'remind me every Monday at "
                   "9 AM to …'.")
    return ScheduleIntent(intent=INTENT_NONE)


__all__ = [
    "INTENT_CANCEL",
    "INTENT_CREATE_REMINDER",
    "INTENT_CREATE_TASK",
    "INTENT_EDIT",
    "INTENT_INSPECT",
    "INTENT_LIST",
    "INTENT_NONE",
    "INTENT_PAUSE",
    "INTENT_QUESTION",
    "INTENT_RESUME",
    "INTENT_RUN_NOW",
    "ScheduleIntent",
    "parse",
]
