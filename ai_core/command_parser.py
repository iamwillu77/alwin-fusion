"""Deterministic natural-language command parser.

The parser converts user text into a structured :class:`ParsedIntent`.
It never executes anything and never depends on a language model, so
the application stays useful while Ollama is unavailable.

Only a deliberately small set of commands is understood.  Anything
else becomes an ``unsupported`` intent with a helpful reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional

# --------------------------------------------------------------------------- #
# Intents
# --------------------------------------------------------------------------- #

LIST_DIRECTORY = "list_directory"
CREATE_DIRECTORY = "create_directory"
CREATE_TEXT_FILE = "create_text_file"
READ_FILE = "read_file"
EXPLAIN = "explain"
EXIT = "exit"
UNSUPPORTED = "unsupported"

INTENTS = (
    LIST_DIRECTORY,
    CREATE_DIRECTORY,
    CREATE_TEXT_FILE,
    READ_FILE,
    EXPLAIN,
    EXIT,
    UNSUPPORTED,
)

SUPPORTED_COMMAND_EXAMPLES = (
    "Show files and folders in my Documents.",
    "Create a folder named Business Reports in my Documents.",
    "Create a file named todo.txt in Documents containing buy milk.",
    "Read notes.txt in Documents.",
    "Explain create a folder named Archive in Documents.",
)


class ParseError(Exception):
    """Raised for malformed input that cannot be parsed at all."""


@dataclass(frozen=True)
class ParsedIntent:
    """Structured result of parsing a single command line."""

    intent: str
    slots: Dict[str, str] = field(default_factory=dict)
    raw: str = ""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

_ARTICLE_RE = re.compile(r"^(?:a|an|my|our|the)\s+", re.IGNORECASE)
_LOCATION_SUFFIX_RE = re.compile(
    r"\s+(?:directory|directories|dir|folder|folders)$", re.IGNORECASE
)
_LEADING_HOME_RE = re.compile(r"^(?:~|home)\s*/\s*", re.IGNORECASE)
_TRAILING_PUNCT_RE = re.compile(r"[.,!?;:]+$")


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _trim(text: str) -> str:
    return _TRAILING_PUNCT_RE.sub("", _clean(text)).strip()


def normalize_location(raw: Optional[str]) -> str:
    """Normalize a location phrase such as ``my Documents directory``.

    Returns a path fragment relative to the user's home directory (or an
    absolute path if the user typed one).  Returns ``""`` when no
    location was given.
    """
    loc = _trim(raw or "")
    if not loc:
        return ""
    loc = loc.strip("\"'")
    loc = _LEADING_HOME_RE.sub("", loc)
    loc = _ARTICLE_RE.sub("", loc)
    loc = _LOCATION_SUFFIX_RE.sub("", loc)
    return loc.strip("/ \t")


def normalize_name(raw: Optional[str]) -> str:
    """Normalize a file or folder name supplied by the user."""
    name = _trim(raw or "")
    name = name.strip("\"'")
    return name


def _strip_presentation_verb(text: str) -> Optional[str]:
    """Return the inner command of an 'explain ...' style phrase."""
    m = re.match(
        r"^(?:explain|describe)\b\s*[:\-]?\s*(?P<rest>.+)$", text, re.IGNORECASE
    )
    if m:
        return m.group("rest")
    m = re.match(
        r"^(?:what would happen if|what would you do if|how would you)\s*(?P<rest>.+)$",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group("rest")
    return None


_PAST_TO_PRESENT = {
    "created": "create",
    "made": "make",
    "listed": "list",
    "wrote": "write",
    "opened": "open",
    "showed": "show",
    "show": "show",
    "read": "read",
}


def _normalize_inner_command(text: str) -> str:
    """Best-effort normalization of the command inside an explain phrase."""
    inner = _clean(text)
    inner = re.sub(r"^(?:if|when)\s+", "", inner, flags=re.IGNORECASE)
    inner = re.sub(r"^i\s+(?:were\s+to\s+|will\s+|want\s+to\s+)?", "", inner, flags=re.IGNORECASE)
    first, _, rest = inner.partition(" ")
    mapped = _PAST_TO_PRESENT.get(first.lower())
    if mapped:
        inner = f"{mapped} {rest}" if rest else mapped
    return inner


# --------------------------------------------------------------------------- #
# Pattern-based parsing
# --------------------------------------------------------------------------- #

_CONTENT_SPLIT_RE = re.compile(
    r"\s+(?:with(?:\s+the)?|containing|that\s+says|whose\s+content\s+is)"
    r"(?:\s+(?:text|content))?\s+(?P<content>.+)$",
    re.IGNORECASE,
)

_FILE_RE = re.compile(
    r"^(?:create|make|new|write)\s+(?:a|an)?\s*(?:new\s+)?(?:text\s+)?file\s*"
    r"(?:(?:named|called|titled)\s+)?(?P<name>.+?)"
    r"(?:\s+(?:in|inside|under)\s+(?P<location>.+))?$",
    re.IGNORECASE,
)

_DIR_RE = re.compile(
    r"^(?:create|make|new)\s+(?:a|an)?\s*(?:new\s+)?"
    r"(?:folder|directory|dir|subdirectory)\s*"
    r"(?:(?:named|called|titled|for)\s+)?(?P<name>.+?)"
    r"(?:\s+(?:in|inside|under|at)\s+(?P<location>.+))?$",
    re.IGNORECASE,
)

_LIST_RE = re.compile(
    r"^(?:show|list|display|enumerate)\s+(?:me\s+)?(?:the\s+)?(?:my\s+)?"
    r"(?:files\s+and\s+folders|folders\s+and\s+files|contents|files|folders|dirs|directory)"
    r"(?:\s+(?:in|inside|of|under|at)\s+(?P<location>.+))?$",
    re.IGNORECASE,
)

_LIST_WHATS_RE = re.compile(
    r"^(?:what(?:'s| is)|show\s+me\s+what(?:'s| is))\s+in\s+"
    r"(?:(?:my|the|our)\s+)?(?P<location>.+)$",
    re.IGNORECASE,
)

_READ_RE = re.compile(
    r"^(?:read|open|cat)\s+(?:me\s+)?(?:the\s+)?(?:file\s+)?(?P<rest>.+)$",
    re.IGNORECASE,
)

_READ_LOCATION_RE = re.compile(
    r"^(?P<name>.+?)(?:\s+(?:in|from|under)\s+(?P<location>.+))?$", re.IGNORECASE
)

_READ_QUOTED_RE = re.compile(
    r"^[\"'](?P<name>[^\"']+)[\"'](?:\s+(?:in|from|under)\s+(?P<location>.+))?$",
    re.IGNORECASE,
)

_EXIT_RE = re.compile(
    r"^(?:quit|exit|bye|goodbye|q|end)(?:\s+(?:the\s+)?(?:program|session|app(?:lication)?|cli))?\s*[.!]?$",
    re.IGNORECASE,
)


def _unsupported(reason: str, raw: str) -> ParsedIntent:
    return ParsedIntent(UNSUPPORTED, {"reason": reason}, raw)


def _parse_file(text: str, raw: str) -> Optional[ParsedIntent]:
    if not re.match(r"^(?:create|make|new|write)\b", text, re.IGNORECASE):
        return None
    if not re.search(r"\b(?:text\s+)?file\b", text, re.IGNORECASE):
        return None

    content = ""
    body = text
    content_match = _CONTENT_SPLIT_RE.search(text)
    if content_match:
        content = content_match.group("content").strip().strip("\"'")
        body = text[: content_match.start()].strip()

    if not content:
        return _unsupported(
            "Creating a text file needs content, e.g. "
            "'Create a file named todo.txt in Documents containing buy milk'.",
            raw,
        )

    m = _FILE_RE.match(body)
    if not m:
        return _unsupported(
            "I could not find a file name in that command. Try: "
            "'Create a file named notes.txt in Documents containing hello'.",
            raw,
        )
    name = normalize_name(m.group("name"))
    if not name:
        return _unsupported("The file name is empty.", raw)
    if not m.group("location") and re.match(r"^(?:in|inside|under|at)\b", name, re.IGNORECASE):
        return _unsupported(
            "I could not find a file name in that command. Try: "
            "'Create a file named notes.txt in Documents containing hello'.",
            raw,
        )
    return ParsedIntent(
        CREATE_TEXT_FILE,
        {"name": name, "location": normalize_location(m.group("location")), "content": content},
        raw,
    )


def _parse_read(text: str, raw: str) -> Optional[ParsedIntent]:
    m = _READ_RE.match(text)
    if not m:
        return None
    rest = _trim(m.group("rest"))
    rest = re.sub(r"^(?:my|the)\s+", "", rest, flags=re.IGNORECASE)
    if not rest:
        return _unsupported("Tell me which file to read, e.g. 'Read notes.txt in Documents'.", raw)

    quoted = _READ_QUOTED_RE.match(rest)
    if quoted:
        name = quoted.group("name").strip()
        location = quoted.group("location") or ""
    else:
        plain = _READ_LOCATION_RE.match(rest)
        name = plain.group("name").strip()
        location = plain.group("location") or ""

    name = name.strip("\"'")
    if not name:
        return _unsupported("Tell me which file to read, e.g. 'Read notes.txt in Documents'.", raw)
    return ParsedIntent(
        READ_FILE, {"name": name, "location": normalize_location(location)}, raw
    )


def parse_command(command: str) -> ParsedIntent:
    """Parse *command* into a :class:`ParsedIntent`.

    Never raises for ordinary user input; unknown input becomes an
    ``unsupported`` intent with an explanatory reason.
    """
    raw = command if command is not None else ""
    text = _clean(raw)
    if not text:
        return _unsupported("The command is empty.", raw)

    if _EXIT_RE.match(text):
        return ParsedIntent(EXIT, {}, raw)

    explained = _strip_presentation_verb(text)
    if explained is not None:
        inner = _normalize_inner_command(explained)
        if not inner:
            return _unsupported(
                "Tell me which operation to explain, e.g. "
                "'Explain create a folder named Archive in Documents'.",
                raw,
            )
        return ParsedIntent(EXPLAIN, {"target": inner}, raw)

    m = _DIR_RE.match(text)
    if m:
        name = normalize_name(m.group("name"))
        if not name:
            return _unsupported("The folder name is empty.", raw)
        if not m.group("location") and re.match(r"^(?:in|inside|under|at)\b", name, re.IGNORECASE):
            return _unsupported(
                "Please include the folder name, e.g. "
                "'Create a folder named Business Reports in Documents'.",
                raw,
            )
        return ParsedIntent(
            CREATE_DIRECTORY,
            {"name": name, "location": normalize_location(m.group("location"))},
            raw,
        )

    file_intent = _parse_file(text, raw)
    if file_intent is not None:
        return file_intent

    m = _LIST_RE.match(text)
    if m:
        return ParsedIntent(
            LIST_DIRECTORY, {"location": normalize_location(m.group("location"))}, raw
        )

    m = _LIST_WHATS_RE.match(text)
    if m:
        return ParsedIntent(
            LIST_DIRECTORY, {"location": normalize_location(m.group("location"))}, raw
        )

    read_intent = _parse_read(text, raw)
    if read_intent is not None:
        return read_intent

    return _unsupported(
        "Supported commands are: list files, create a folder, create a text "
        "file with content, read a text file, explain an operation. Type "
        "'help' for examples.",
        raw,
    )
