"""Persistent memory for the companion (v0.15.0) — local, typed, honest.

A small SQLite-backed store for the four things the user *explicitly*
asks Alwin to remember: user preferences, project context, concise
task summaries, and user-approved general context.  It is deliberately
separate from :class:`companion.conversation.SessionContext` (bounded,
in-memory, per-run): this store outlives the process, lives in the
owner-only state directory, and is inspectable, correctable, disable-
able and deletable from the UI.

Design rules (v0.15.0 brief):

* **stdlib only** (``sqlite3``/``os``/``re``/``time``/``uuid``) — no new
  dependencies, no database service, no cloud, no vector index,
* **explicit creation only** — a row exists because the user asked for
  it (``explicit_request``) or created/corrected it in the Memory
  manager (``ui_create``/``ui_correction``); conversation turns, task
  records, documents and model output are NEVER persisted silently,
* **honest persistence** — a save that did not commit raises
  :class:`MemoryStoreError` and never reports success; corruption,
  locking, schema upgrades, exhaustion and unavailability are distinct,
  bounded, content-free messages,
* **bounded & validated** — closed category/sensitivity/provenance
  sets, bounded content/project fields, normalized single-line text,
  secret-shaped content declined WITHOUT echoing the secret,
* **trust isolation** — this module imports NO execution engine
  (``ai_core.pipeline|executor|planner|permissions|verifier|audit`` and
  ``integrations`` are banned package-wide by the companion security
  suite); a memory row can never authorize, approve or extend anything.
  At retrieval it is untrusted DATA: quoted, labelled, scope-filtered
  and bounded before it can reach a prompt.

Schema migrations are explicit: ``PRAGMA user_version`` records the
version and every step in ``_MIGRATIONS`` runs in order inside one
transaction (v1 = base row, v2 = privacy/freshness columns + indexes).
A database from a *newer* schema is refused honestly, never downgraded.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

#: Database file inside the owner-only state directory (0600 file,
#: 0700 directory — the same layout as the audit log).
DB_FILENAME = "memory.db"

#: Current schema version (see ``_MIGRATIONS``).
SCHEMA_VERSION = 2

#: The closed sets — a value outside them is refused, never coerced.
CATEGORIES: Tuple[str, ...] = ("preference", "project", "task_summary",
                               "general")
SENSITIVITIES: Tuple[str, ...] = ("normal", "sensitive")
PROVENANCES: Tuple[str, ...] = ("explicit_request", "ui_create",
                                "ui_correction")

#: Bounds: content is one bounded line; the project scope is a short
#: explicit label ("" = global), never a guessed association.
MAX_CONTENT_CHARS = 600
MAX_PROJECT_CHARS = 60

#: Retrieval bounds — memory is never loaded wholesale into a prompt.
DEFAULT_RETRIEVE_LIMIT = 8
DEFAULT_RETRIEVE_CHARS = 1400
#: SQL candidate ceiling: ranking runs over at most this many rows.
MAX_CANDIDATES = 200
#: Inspection list bound (UI list, never the prompt).
MAX_LIST_LIMIT = 200

#: Ranking: direct user preferences and confirmed facts come first,
#: then project context, general context and task summaries.
CATEGORY_PRIORITY: Dict[str, int] = {"preference": 0, "project": 1,
                                     "general": 2, "task_summary": 3}

#: Two rows of the same category/scope whose content tokens overlap
#: this much (but differ) are flagged as a material conflict so the
#: reply can ask one clarifying question instead of picking silently.
CONFLICT_JACCARD = 0.6

#: Honest, content-free failure texts — one per kind.  None of these
#: ever embeds stored or attempted content.
ERROR_TEXT: Dict[str, str] = {
    "unavailable": "persistent memory storage is unavailable",
    "corrupt": "the memory database is unreadable (corrupt)",
    "locked": "the memory database is locked by another process",
    "full": "no space is left to save memories",
    "schema": "the memory database uses a newer schema than this "
              "version understands",
    "disabled": "persistent memory is turned off in Settings",
    "invalid": "the memory could not be validated",
    "not_found": "that memory no longer exists",
}

#: Decline reasons for prohibited memories (fixed strings — the
#: attempted content is NEVER repeated back or stored anywhere).
REASON_SECRET = ("I can't save that — it looks like it contains a "
                 "password, key, token, or other secret, and secrets "
                 "are never stored in memory.")
REASON_LONG = (f"I can't save that — memories are short facts "
               f"({MAX_CONTENT_CHARS} characters maximum).")
REASON_TRANSCRIPT = ("I can't save that — raw conversation transcripts "
                     "are never stored as memories.")
REASON_EMPTY = "There's nothing to remember in that."
REASON_REMINDER = ("Reminders and to-dos aren't memories — put them in "
                   "a task instead; I won't store this as a remembered "
                   "fact.")


class MemoryStoreError(Exception):
    """A real, classified persistence failure — never faked.

    ``kind`` is one of :data:`ERROR_TEXT` (``unavailable``, ``corrupt``,
    ``locked``, ``full``, ``schema``, ``disabled``, ``invalid``,
    ``not_found``); ``str(exc)`` is the corresponding bounded, honest,
    content-free sentence for the UI.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind if kind in ERROR_TEXT else "unavailable"
        message = ERROR_TEXT[self.kind]
        if detail:
            message = f"{message}: {detail[:120]}"
        super().__init__(message)
        self.detail = message


class MemoryRejected(MemoryStoreError):
    """The user asked to remember something prohibited — declined."""

    def __init__(self, reason: str) -> None:
        super().__init__("invalid")
        self.reason = reason
        # str(exc) must be the safe, fixed reason (never the content).
        Exception.__init__(self, reason)
        self.args = (reason,)


# --------------------------------------------------------------------------- #
# Explicit-request parsing (deterministic — the model never decides a write)
# --------------------------------------------------------------------------- #

_REMEMBER_PATTERN = r"^\s*(?:please\s+)?remember\b(?P<rest>.*)$"

#: Preference-shaped phrasing → category "preference"; anything else
#: the user explicitly remembers lands in "general" (user-approved
#: general context).  Project/task categories come from the UI form,
#: where the user picks them deliberately.
_PREFERENCE_PATTERN = (
    r"^(?:i\s+(?:prefer|like|want|love|hate|always|never)|"
    r"(?:always|never|please\s+always|please\s+never)\b|my\s+default\b)"
)


@dataclass(frozen=True)
class RememberRequest:
    """A parsed explicit remember-request (or why it is refused)."""

    #: What to store (already normalized) — "" for refusals.
    body: str
    #: "fact" (store it), "reminder" (decline), "empty" (ask again).
    kind: str


def parse_remember_request(text: Optional[str]) -> Optional[RememberRequest]:
    """Recognize an *explicit* remember-request — deterministically.

    Only imperative forms starting with ``remember`` (optionally
    ``please``) count; a sentence that merely mentions remembering is
    ordinary conversation.  ``remember to …`` is reminder-shaped and is
    refused (it is not a durable fact), and an empty request asks the
    user what they meant.  Returns ``None`` when this is not a
    remember-request at all.
    """
    match = re.match(_REMEMBER_PATTERN, str(text or ""), re.IGNORECASE)
    if match is None:
        return None
    rest = match.group("rest").strip()
    if not rest:
        return RememberRequest("", "empty")
    if re.match(r"^to\b", rest, re.IGNORECASE):
        return RememberRequest("", "reminder")
    if rest.endswith("?"):
        # A question ("remember me?") is conversation, not a request
        # to store something — never persist on a query.
        return None
    rest = re.sub(r"^(?:that|:|,)\s*", "", rest, count=1,
                  flags=re.IGNORECASE).strip()
    if not rest:
        return RememberRequest("", "empty")
    return RememberRequest(rest, "fact")


def classify_category(body: str) -> str:
    """Deterministic category for an explicit request (never the LLM)."""
    return ("preference" if re.match(_PREFERENCE_PATTERN, body,
                                     re.IGNORECASE) else "general")


# --------------------------------------------------------------------------- #
# Prohibited-content detection (declined WITHOUT repeating the secret)
# --------------------------------------------------------------------------- #

#: Credential-*shaped values*: strong signals that a secret is present.
#: A conversation *about* passwords ("my password policy needs 12
#: characters") is fine; an assigned/embedded value is refused.
#: Pattern strings + flags (the package pins ``re.search``-style use —
#: ``re.compile`` would collide with the banned ``compile`` call name).
_SECRET_VALUE_PATTERNS: Tuple[Tuple[str, int], ...] = (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    (r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b", 0),
    (r"\bsk-[A-Za-z0-9_\-]{16,}\b", 0),
    (r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b", 0),
    (r"\bAKIA[0-9A-Z]{16}\b", 0),
    (r"\bbearer\s+[A-Za-z0-9._~+/=\-]{12,}", re.IGNORECASE),
    (r"://[^\s/:@]+:[^\s/@]+@", 0),                  # URL userinfo
    (r"\b(?:password|passwd|pwd|api[_\- ]?key|secret|token|"
     r"access[_\- ]?key|credentials?)(?:\s*[:=]\s*|\s+is\s+)\S{4,}",
     re.IGNORECASE),
    (r"\b[A-Za-z0-9+/=_\-]{40,}\b", 0),               # long random run
)

#: Dialogue/transcript shape: several speaker-prefixed lines.
_TRANSCRIPT_LINE_PATTERN = (r"^\s*(?:user|assistant|human|alwin|system|"
                            r"speaker\s*\d+)\s*:\s*\S")


def secret_reason(content: str) -> Optional[str]:
    """``REASON_SECRET`` when *content* is credential-shaped, else None."""
    for pattern, flags in _SECRET_VALUE_PATTERNS:
        if re.search(pattern, content, flags):
            return REASON_SECRET
    return None


def transcript_reason(content: str) -> Optional[str]:
    """``REASON_TRANSCRIPT`` when *content* looks like a raw transcript."""
    lowered = content.lower()
    if "conversation transcript" in lowered or "chat log" in lowered:
        return REASON_TRANSCRIPT
    lines = str(content).splitlines()
    speakers = sum(
        1 for line in lines
        if re.match(_TRANSCRIPT_LINE_PATTERN, line, re.IGNORECASE)
    )
    if speakers >= 3:
        return REASON_TRANSCRIPT
    return None


def validate_memory(
    content: str,
    *,
    category: str = "general",
    project: str = "",
    confidence: float = 1.0,
    sensitivity: str = "normal",
) -> Tuple[str, str, str, float, str]:
    """Normalize and validate a memory before persistence.

    Returns ``(content, category, project, confidence, sensitivity)``
    on success; raises :class:`MemoryRejected` (with a fixed, safe
    reason — never the attempted content) when the request is
    prohibited or out of bounds.
    """
    if not isinstance(content, str):
        raise MemoryRejected(REASON_EMPTY)
    # Transcript shape is judged on the RAW text (its line structure is
    # the signal) before normalization flattens it.
    if transcript_reason(content) is not None:
        raise MemoryRejected(REASON_TRANSCRIPT)
    # Normalize: control characters dropped, whitespace collapsed to a
    # single line (prompt safety) — then bound the length.
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", content)
    clean = " ".join(clean.split())
    if not clean:
        raise MemoryRejected(REASON_EMPTY)
    if secret_reason(clean) is not None:
        raise MemoryRejected(REASON_SECRET)
    if len(clean) > MAX_CONTENT_CHARS:
        raise MemoryRejected(REASON_LONG)

    if category not in CATEGORIES:
        raise MemoryStoreError("invalid", "unknown category")
    if sensitivity not in SENSITIVITIES:
        raise MemoryStoreError("invalid", "unknown sensitivity")
    scope = " ".join(str(project or "").split())[:MAX_PROJECT_CHARS]
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        raise MemoryStoreError("invalid", "confidence must be a number")
    if not 0.0 <= conf <= 1.0:
        conf = max(0.0, min(1.0, conf))
    return clean, category, scope, conf, sensitivity


# --------------------------------------------------------------------------- #
# Typed records
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MemoryRecord:
    """One persisted memory — every field bounded and typed."""

    #: Stable identifier (``mem_`` + 16 hex) — survives restarts.
    id: str
    #: One of :data:`CATEGORIES`.
    category: str
    #: Bounded, normalized single-line content (≤ 600 chars).
    content: str
    #: How it got here: ``explicit_request`` / ``ui_create`` /
    #: ``ui_correction`` — never "the model decided".
    provenance: str
    #: Explicit project scope ("" = global; never guessed).
    project: str
    #: 0..1 — user-declared facts are 1.0; uncertain inferences are
    #: never created (the UI asks the user, it does not guess).
    confidence: float
    #: ``normal`` or ``sensitive`` (sensitive rows are flagged in UI).
    sensitivity: str
    #: Unix seconds.
    created_at: int
    updated_at: int
    #: Unix seconds of the last retrieval that used it (best effort).
    last_used_at: Optional[int]
    #: Unix seconds after which the row is stale (None = no expiry).
    expires_at: Optional[int]
    #: Unix seconds at which the row should be re-confirmed (None).
    review_at: Optional[int]
    #: Bumped on every in-place correction — corrections win.
    revision: int
    #: Retrieval-only annotation (not a stored column): this row
    #: conflicts with another returned row of the same category/scope.
    conflict: bool = False
    #: Retrieval-only annotation: ``review_at`` has passed — the fact
    #: is due for re-confirmation (flagged, never silently trusted).
    review_due: bool = False

    def prompt_line(self) -> str:
        """One bounded, labelled, untrusted-data line for the prompt."""
        flags = ""
        if self.conflict:
            flags += " [conflict]"
        if self.review_due:
            flags += " [needs re-confirmation]"
        return f"- {self.category}: {self.content}{flags}"


@dataclass(frozen=True)
class RetrievalResult:
    """Bounded retrieval outcome (never a full dump of the store)."""

    #: Ranked, scope-filtered, bounded records (conflicts annotated).
    records: Tuple[MemoryRecord, ...] = ()
    #: True when matching rows existed beyond the count/char budget.
    truncated: bool = False
    #: True when the store is disabled — nothing was read at all.
    disabled: bool = False

    @property
    def empty(self) -> bool:
        return not self.records


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #

#: Ordered schema steps; key = target version.  Applied in order on
#: open inside a single transaction (each step is idempotent by
#: version check, not by content sniffing).
_MIGRATIONS: Dict[int, str] = {
    1: """
    CREATE TABLE memories (
        id         TEXT PRIMARY KEY,
        category   TEXT NOT NULL,
        content    TEXT NOT NULL,
        provenance TEXT NOT NULL,
        project    TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    """,
    2: """
    ALTER TABLE memories ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0;
    ALTER TABLE memories ADD COLUMN sensitivity TEXT NOT NULL
                                                 DEFAULT 'normal';
    ALTER TABLE memories ADD COLUMN last_used_at INTEGER;
    ALTER TABLE memories ADD COLUMN expires_at  INTEGER;
    ALTER TABLE memories ADD COLUMN review_at   INTEGER;
    ALTER TABLE memories ADD COLUMN revision    INTEGER NOT NULL
                                                 DEFAULT 1;
    CREATE INDEX memories_project_idx  ON memories(project);
    CREATE INDEX memories_category_idx ON memories(category);
    """,
}

_COLUMNS = ("id", "category", "content", "provenance", "project",
            "confidence", "sensitivity", "created_at", "updated_at",
            "last_used_at", "expires_at", "review_at", "revision")


def _classify(exc: BaseException) -> str:
    """Map a driver exception to an honest, content-free kind."""
    message = str(exc).lower()
    if ("unable to open" in message or "disk i/o" in message
            or "readonly" in message or "permission denied" in message
            or "read-only" in message):
        return "unavailable"
    if "locked" in message:
        return "locked"
    if "disk is full" in message or "disk full" in message:
        return "full"
    if ("not a database" in message or "malformed" in message
            or "corrupt" in message):
        return "corrupt"
    if isinstance(exc, sqlite3.DatabaseError):
        return "corrupt"
    return "unavailable"


def _tokens(text: str) -> set:
    """Lowercased word tokens for deterministic overlap ranking."""
    return {t for t in re.findall(r"\w{2,}", str(text).lower())}


class MemoryStore:
    """SQLite-backed persistent memory — local, bounded, honest.

    Open is eager: constructing the store migrates the schema (or
    raises :class:`MemoryStoreError` with the real kind).  The file is
    chmod 0600 on success; the state directory is 0700 (created by the
    caller's ``ensure_state_dir``).
    """

    def __init__(self, path: Path, *, enabled: bool = True,
                 now=None) -> None:
        self._path = Path(path)
        self._now = now or time.time
        self._enabled = bool(enabled)
        self._last_error: Optional[str] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    # ------------------------------------------------------------------ #
    # Lifecycle & migrations
    # ------------------------------------------------------------------ #

    @classmethod
    def open_for(cls, state_dir: Path, *, enabled: bool = True,
                 now=None) -> "MemoryStore":
        """Open ``state_dir/memory.db`` (creating the dir 0700 if new)."""
        directory = Path(state_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise MemoryStoreError("unavailable",
                                   exc.__class__.__name__) from exc
        return cls(directory / DB_FILENAME, enabled=enabled, now=now)

    def _open(self) -> None:
        try:
            conn = sqlite3.connect(str(self._path), timeout=2.0,
                                   isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 2000")
            # Owner-only from the moment the file exists (the parent
            # directory is 0700 — opened for() ensures it).
            os.chmod(self._path, 0o600)
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
        except OSError as exc:
            self._raise("unavailable", exc)
        try:
            self._migrate(conn)
        except MemoryStoreError:
            conn.close()
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            conn.close()
            self._raise(_classify(exc), exc)
        self._conn = conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply every pending migration in ONE transaction.

        The whole upgrade (schema steps + ``user_version`` bump) is
        atomic: a failure rolls back to the previous consistent
        version instead of leaving a half-upgraded file.  A database
        NEWER than this build is refused honestly (``schema``), never
        downgraded or silently wiped.
        """
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            version = int(row[0])
        except (sqlite3.DatabaseError, IndexError, ValueError) as exc:
            self._last_error = (f"{ERROR_TEXT[_classify(exc)]} "
                                f"({exc.__class__.__name__})")
            raise MemoryStoreError(_classify(exc),
                                   exc.__class__.__name__) from exc
        if version > SCHEMA_VERSION:
            detail = f"found v{version}"
            self._last_error = f"{ERROR_TEXT['schema']}: {detail}"
            raise MemoryStoreError("schema", detail)
        pending = [target for target in sorted(_MIGRATIONS)
                   if target > version]
        if not pending:
            return
        # executescript() commits any open transaction before it runs,
        # so BEGIN/COMMIT live INSIDE the single script string.
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
            self._last_error = (f"{ERROR_TEXT[_classify(exc)]} "
                                f"({exc.__class__.__name__})")
            raise MemoryStoreError(_classify(exc),
                                   exc.__class__.__name__) from exc
        self._last_error = None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.DatabaseError:  # pragma: no cover - defensive
                pass
            self._conn = None

    def _raise(self, kind: str, exc: BaseException) -> None:
        """Record the (content-free) failure and raise honestly."""
        detail = exc.__class__.__name__
        self._last_error = f"{ERROR_TEXT[kind]} ({detail})"
        raise MemoryStoreError(kind, detail) from exc

    # ------------------------------------------------------------------ #
    # State (disable is a real, tested effect — both directions)
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    @property
    def last_error(self) -> Optional[str]:
        """Bounded, content-free diagnostic of the last real failure."""
        return self._last_error

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise MemoryStoreError("unavailable", "store is closed")
        return self._conn

    def _guard_write(self) -> sqlite3.Connection:
        if not self._enabled:
            # Deliberate: disabling never pretends a save happened and
            # never sets last_error (it is not a failure).
            raise MemoryStoreError("disabled")
        return self._require_conn()

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def create(
        self,
        content: str,
        *,
        category: str = "general",
        provenance: str = "explicit_request",
        project: str = "",
        confidence: float = 1.0,
        sensitivity: str = "normal",
        expires_at: Optional[int] = None,
        review_at: Optional[int] = None,
    ) -> MemoryRecord:
        """Validate and persist one memory; raise honestly on failure."""
        conn = self._guard_write()
        if provenance not in PROVENANCES:
            raise MemoryStoreError("invalid", "unknown provenance")
        clean, category, scope, conf, sensitivity = validate_memory(
            content, category=category, project=project,
            confidence=confidence, sensitivity=sensitivity,
        )
        now = int(self._now())
        record = MemoryRecord(
            id=f"mem_{uuid.uuid4().hex[:16]}",
            category=category,
            content=clean,
            provenance=provenance,
            project=scope,
            confidence=conf,
            sensitivity=sensitivity,
            created_at=now,
            updated_at=now,
            last_used_at=None,
            expires_at=expires_at,
            review_at=review_at,
            revision=1,
        )
        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                "INSERT INTO memories (id, category, content, provenance,"
                " project, confidence, sensitivity, created_at,"
                " updated_at, last_used_at, expires_at, review_at,"
                " revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.id, record.category, record.content,
                 record.provenance, record.project, record.confidence,
                 record.sensitivity, record.created_at, record.updated_at,
                 record.last_used_at, record.expires_at, record.review_at,
                 record.revision),
            )
            if cur.rowcount != 1:
                raise MemoryStoreError("unavailable", "insert not "
                                       "confirmed")
            conn.execute("COMMIT")
        except MemoryStoreError:
            _rollback(conn)
            raise
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            self._raise(_classify(exc), exc)
        self._last_error = None
        return record

    def get(self, mem_id: str) -> Optional[MemoryRecord]:
        """One row by id, or None (deleted means gone, not filtered)."""
        conn = self._require_conn()
        try:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (str(mem_id),)
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
        return _to_record(row) if row is not None else None

    def list(
        self,
        *,
        project: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = MAX_LIST_LIMIT,
    ) -> List[MemoryRecord]:
        """Inspection listing (bounded, newest first, never the prompt)."""
        conn = self._require_conn()
        clauses: List[str] = []
        params: List[object] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(str(project))
        if category is not None:
            clauses.append("category = ?")
            params.append(str(category))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        bound = max(1, min(int(limit), MAX_LIST_LIMIT))
        try:
            rows = conn.execute(
                f"SELECT * FROM memories{where} ORDER BY updated_at DESC,"
                f" id DESC LIMIT {bound}",     # integer-bound only
                params,
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
        return [_to_record(row) for row in rows]

    def count(self) -> int:
        conn = self._require_conn()
        try:
            row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
        return int(row[0])

    def update(
        self,
        mem_id: str,
        *,
        content: Optional[str] = None,
        category: Optional[str] = None,
        confidence: Optional[float] = None,
        sensitivity: Optional[str] = None,
        project: Optional[str] = None,
        expires_at: Optional[int] = None,
        review_at: Optional[int] = None,
        clear_expiry: bool = False,
    ) -> MemoryRecord:
        """Correct a memory IN PLACE (revision bump — contradictions are
        updated, never kept as equally valid copies)."""
        conn = self._guard_write()
        existing = self.get(mem_id)
        if existing is None:
            raise MemoryStoreError("not_found")
        new_content = existing.content if content is None else content
        new_category = existing.category if category is None else category
        new_conf = (existing.confidence if confidence is None
                    else confidence)
        new_sens = (sensitivity if sensitivity is not None
                    else existing.sensitivity)
        new_scope = existing.project if project is None else project
        clean, new_category, new_scope, new_conf, new_sens = (
            validate_memory(new_content, category=new_category,
                            project=new_scope, confidence=new_conf,
                            sensitivity=new_sens)
        )
        new_expires = (None if clear_expiry else
                       (existing.expires_at if expires_at is None
                        else int(expires_at)))
        new_review = (None if clear_expiry else
                      (existing.review_at if review_at is None
                       else int(review_at)))
        now = int(self._now())
        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                "UPDATE memories SET content = ?, category = ?,"
                " project = ?, confidence = ?, sensitivity = ?,"
                " updated_at = ?, expires_at = ?, review_at = ?,"
                " revision = revision + 1 WHERE id = ?",
                (clean, new_category, new_scope, new_conf, new_sens,
                 now, new_expires, new_review, existing.id),
            )
            if cur.rowcount != 1:
                _rollback(conn)
                raise MemoryStoreError("not_found")
            conn.execute("COMMIT")
        except MemoryStoreError:
            _rollback(conn)
            raise
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            self._raise(_classify(exc), exc)
        self._last_error = None
        updated = self.get(existing.id)
        if updated is None:  # pragma: no cover - defensive
            raise MemoryStoreError("not_found")
        return updated

    def correct(self, mem_id: str, content: str) -> MemoryRecord:
        """Correct the text of a memory (provenance becomes
        ``ui_correction`` — the record shows it was corrected)."""
        record = self.update(mem_id, content=content)
        conn = self._require_conn()
        try:
            conn.execute(
                "UPDATE memories SET provenance = 'ui_correction'"
                " WHERE id = ?",
                (record.id,),
            )
        except sqlite3.DatabaseError as exc:  # pragma: no cover
            self._raise(_classify(exc), exc)
        corrected = self.get(record.id)
        return corrected if corrected is not None else record

    def delete(self, mem_id: str) -> bool:
        """Remove the row itself (not a retrieval-side hide)."""
        conn = self._guard_write()
        try:
            cur = conn.execute("DELETE FROM memories WHERE id = ?",
                               (str(mem_id),))
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
        if cur.rowcount == 1:
            self._last_error = None
            return True
        return False

    def clear(self) -> int:
        """Delete EVERY memory row — and nothing else on disk."""
        conn = self._guard_write()
        try:
            cur = conn.execute("DELETE FROM memories")
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)
        self._last_error = None
        return int(cur.rowcount)

    # ------------------------------------------------------------------ #
    # Retrieval (deterministic SQLite filter + bounded ranking)
    # ------------------------------------------------------------------ #

    def retrieve(
        self,
        query: str = "",
        *,
        project: str = "",
        limit: int = DEFAULT_RETRIEVE_LIMIT,
        max_chars: int = DEFAULT_RETRIEVE_CHARS,
        touch: bool = True,
    ) -> RetrievalResult:
        """Bounded, scope-respecting, deterministic retrieval.

        Rows from OTHER projects are excluded in SQL (parameterized);
        global rows (``project = ''``) are always eligible.  Expired
        rows are skipped.  Ranking: query-token overlap, then category
        priority (preferences/confirmed facts first), then recency —
        all deterministic, no embeddings, no cloud.  Count and total
        character budgets are enforced; ``truncated`` says so honestly.
        """
        if not self._enabled:
            return RetrievalResult((), False, True)
        conn = self._require_conn()
        scope = " ".join(str(project or "").split())[:MAX_PROJECT_CHARS]
        now = int(self._now())
        try:
            rows = conn.execute(
                "SELECT * FROM memories WHERE project IN ('', ?)"
                " AND (expires_at IS NULL OR expires_at > ?)"
                " ORDER BY updated_at DESC LIMIT ?",
                (scope, now, MAX_CANDIDATES),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            self._raise(_classify(exc), exc)

        query_tokens = _tokens(query)
        scored: List[Tuple[tuple, MemoryRecord]] = []
        for row in rows:
            record = _to_record(row)
            overlap = len(query_tokens & _tokens(record.content))
            key = (
                -int(overlap > 0),          # overlapping rows first
                -overlap,
                CATEGORY_PRIORITY.get(record.category, 9),
                -record.updated_at,
                record.id,
            )
            scored.append((key, record))
        scored.sort(key=lambda item: item[0])

        # Exact duplicates (same scope/category/text — e.g. asked twice)
        # collapse to the newest copy before budgeting: duplicates are
        # never both carried into a prompt.
        deduped: List[Tuple[tuple, MemoryRecord]] = []
        seen: set = set()
        for key, record in scored:
            dup_key = (record.project, record.category, record.content)
            if dup_key in seen:
                continue
            seen.add(dup_key)
            deduped.append((key, record))
        scored = deduped

        # One ranked pass: overlapping rows lead (relevance), then
        # priority/recency rows fill the remaining budget (so a
        # preference personalizes a prompt even when the utterance
        # shares no words with it) — always count- and char-bounded.
        chosen: List[MemoryRecord] = []
        used = 0
        truncated = False
        for _key, record in scored:
            if len(chosen) >= limit:
                truncated = True
                break
            size = len(record.content)
            if used + size > max_chars:
                truncated = True
                break
            chosen.append(record)
            used += size

        annotated = _annotate_conflicts(chosen)
        # Freshness: a due-for-review row is flagged in the block (it is
        # still usable context, but explicitly marked for re-confirming).
        annotated = [
            replace(record,
                    review_due=(record.review_at is not None
                                and record.review_at <= now))
            for record in annotated
        ]
        if touch and annotated:
            now = int(self._now())
            self._touch([r.id for r in annotated])
            annotated = [replace(r, last_used_at=now) for r in annotated]
        return RetrievalResult(tuple(annotated), truncated, False)

    def _touch(self, ids: Sequence[str]) -> None:
        """Best-effort last-used tracking (never fails a retrieval)."""
        if self._conn is None:  # pragma: no cover - defensive
            return
        try:
            now = int(self._now())
            self._conn.executemany(
                "UPDATE memories SET last_used_at = ? WHERE id = ?",
                [(now, mem_id) for mem_id in ids],
            )
        except sqlite3.DatabaseError:  # pragma: no cover - cosmetic
            pass

    # ------------------------------------------------------------------ #
    # Diagnostics (content-free — visible in the UI, never the rows)
    # ------------------------------------------------------------------ #

    def status(self) -> Dict[str, object]:
        """Honest status for the settings UI / health checks."""
        label = "On" if self._enabled else "Off"
        try:
            total = self.count() if self._conn is not None else 0
            detail = (f"{total} memor{'y' if total == 1 else 'ies'}"
                      f" stored in {self._path.name} (this device only)")
            if self._last_error:
                detail = f"{detail} — last error: {self._last_error}"
            return {"label": label, "detail": detail,
                    "enabled": self._enabled, "count": total,
                    "error": self._last_error}
        except MemoryStoreError as exc:
            return {"label": label, "detail": str(exc), "enabled":
                    self._enabled, "count": 0, "error": str(exc)}


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.DatabaseError:  # pragma: no cover - defensive
        pass


def _to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=str(row["id"]),
        category=str(row["category"]),
        content=str(row["content"]),
        provenance=str(row["provenance"]),
        project=str(row["project"]),
        confidence=float(row["confidence"]),
        sensitivity=str(row["sensitivity"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        last_used_at=(None if row["last_used_at"] is None
                      else int(row["last_used_at"])),
        expires_at=(None if row["expires_at"] is None
                    else int(row["expires_at"])),
        review_at=(None if row["review_at"] is None
                   else int(row["review_at"])),
        revision=int(row["revision"]),
    )


def _annotate_conflicts(records: List[MemoryRecord]) -> List[MemoryRecord]:
    """Flag same-category/scope rows whose content largely overlaps.

    Deterministic and conservative: only *returned* rows are compared,
    the pair must actually differ, and both sides are marked so the
    reply prompt can ask one clarifying question instead of silently
    preferring one.
    """
    out: List[MemoryRecord] = []
    for index, record in enumerate(records):
        conflict = False
        if record.category != "preference":
            out.append(record)
            continue
        mine = _tokens(record.content)
        for other_index, other in enumerate(records):
            if other_index == index or other.category != record.category:
                continue
            theirs = _tokens(other.content)
            union = mine | theirs
            if not union or other.content == record.content:
                continue
            if len(mine & theirs) / len(union) >= CONFLICT_JACCARD:
                conflict = True
                break
        out.append(replace(record, conflict=True) if conflict else record)
    return out


__all__ = [
    "CATEGORIES",
    "DEFAULT_RETRIEVE_CHARS",
    "DEFAULT_RETRIEVE_LIMIT",
    "DB_FILENAME",
    "ERROR_TEXT",
    "MAX_CONTENT_CHARS",
    "MAX_PROJECT_CHARS",
    "PROVENANCES",
    "REASON_EMPTY",
    "REASON_LONG",
    "REASON_REMINDER",
    "REASON_SECRET",
    "REASON_TRANSCRIPT",
    "SCHEMA_VERSION",
    "SENSITIVITIES",
    "MemoryRecord",
    "MemoryRejected",
    "MemoryStore",
    "MemoryStoreError",
    "RememberRequest",
    "RetrievalResult",
    "classify_category",
    "parse_remember_request",
    "secret_reason",
    "transcript_reason",
    "validate_memory",
]
