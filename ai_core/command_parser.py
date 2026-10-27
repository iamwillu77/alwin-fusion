"""Deterministic natural-language command parser.

The parser converts user text into a structured :class:`ParsedIntent`.
It never executes anything and never depends on a language model, so
the application stays useful while Ollama is unavailable.

Only a deliberately small set of commands is understood.  Anything
else becomes an ``unsupported`` intent with a helpful reason.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

# --------------------------------------------------------------------------- #
# Intents
# --------------------------------------------------------------------------- #

LIST_DIRECTORY = "list_directory"
CREATE_DIRECTORY = "create_directory"
CREATE_TEXT_FILE = "create_text_file"
CREATE_REPORT = "create_report"
CREATE_PDF = "create_pdf"
CREATE_SPREADSHEET = "create_spreadsheet"
READ_FILE = "read_file"
EXPLAIN = "explain"
EXIT = "exit"
UNSUPPORTED = "unsupported"

INTENTS = (
    LIST_DIRECTORY,
    CREATE_DIRECTORY,
    CREATE_TEXT_FILE,
    CREATE_REPORT,
    CREATE_PDF,
    CREATE_SPREADSHEET,
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
    'Create a report named summary.txt in Documents containing "Sales rose 12 percent."',
    "Create a report named summary.txt in Documents using report.json",
    "Create a pdf named summary.pdf in Documents using report.json",
    'Create a pdf named readme.pdf in Documents titled "Readme" containing "Welcome to Alwin Fusion."',
    "Create a spreadsheet named sales.xlsx in Documents using sales.json",
    "Explain create a folder named Archive in Documents.",
)


class ParseError(Exception):
    """Raised for malformed input that cannot be parsed at all."""


@dataclass(frozen=True)
class ParsedIntent:
    """Structured result of parsing a single command line.

    ``slots`` values are normally strings; the document intents may
    also carry a parsed inline JSON object under ``"inline"``.
    """

    intent: str
    slots: Dict[str, object] = field(default_factory=dict)
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

# ---- v0.2 document commands -------------------------------------------- #

_SOURCE_USING_RE = re.compile(
    r"\s+using\s+(?P<source>\"[^\"]+\"|'[^']+'|\S+?)"
    r"(?:\s+(?:in|inside|under)\s+(?P<location>.+?))?\s*[.]?$",
    re.IGNORECASE,
)
_SOURCE_FROM_RE = re.compile(r"\s+from\s+(?P<inline>\{.*\})\s*[.]?$", re.IGNORECASE)
_TITLE_RE = re.compile(
    r"\s+titled\s+(?P<title>\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE
)
_CONTAINING_RE = re.compile(r"\s+containing\s+(?P<text>.+)$", re.IGNORECASE)
_DOC_NOUN_RE = re.compile(
    r"^(?:create|make|new|write|generate)\s+(?:a|an|my|the)?\s*(?:new\s+)?"
    r"(?P<kind>report|pdf|spreadsheet|workbook|excel\s+sheet)\b\s*"
    r"(?:(?:named|called)\s+(?P<name>.+?))?"
    r"(?:\s+(?:in|inside|under)\s+(?P<location>.+))?$",
    re.IGNORECASE,
)

#: Verb + document-kind noun must be adjacent ("create a report ..."),
#: so v0.1 commands that merely mention a document word inside a file
#: name ("create a file named report.txt ...") keep their old meaning.
_DOC_HEAD_RE = re.compile(
    r"^(?:create|make|new|write|generate)\s+(?:a|an|my|the)?\s*(?:new\s+)?"
    r"(?:report|pdf|spreadsheet|workbook|excel\s+sheet)\b",
    re.IGNORECASE,
)

_SPREADSHEET_SPEC_HELP = (
    "A spreadsheet needs a JSON specification. Either save it as a file and "
    "say 'using sales.json', or pass it inline, e.g. "
    'from {"sheets": [{"name": "Sheet1", "headers": ["A"], "rows": [[1]]}]}'
)
_REPORT_CONTENT_HELP = (
    "A report needs content: add 'using report.json' (a JSON file), inline "
    'JSON \'from {"title": "...", "blocks": [...]}\', or simple text like '
    'titled "Title" containing "Paragraph text".'
)


def _strip_quotes(value: str) -> str:
    return value.strip().strip("\"'")


def _parse_document(text: str, raw: str) -> Optional[ParsedIntent]:
    """Parse report / PDF / spreadsheet creation commands (v0.2)."""
    if not _DOC_HEAD_RE.match(text):
        return None

    body = text
    source = ""
    source_location = ""
    inline = None

    # 1. Structured source first: 'using <file>' XOR inline 'from {...}'.
    using_any_m = re.search(r"\s+using\s+", body, re.IGNORECASE)
    using_m = _SOURCE_USING_RE.search(body)
    from_m = _SOURCE_FROM_RE.search(body)
    if (using_m or using_any_m) and from_m:
        return _unsupported(
            "Use either a JSON file ('using sales.json') or inline JSON "
            "('from {\"sheets\": [...]}'), not both.",
            raw,
        )
    if using_m:
        source = normalize_name(using_m.group("source"))
        if not source:
            return _unsupported("The file name after 'using' is empty.", raw)
        source_location = using_m.group("location") or ""
        body = (body[: using_m.start()] + body[using_m.end() :]).strip()
    elif using_any_m:
        return _unsupported(
            "The 'using <file.json>' clause must be the last part of the "
            "command; it cannot be combined with 'titled'/'containing' text "
            "or inline JSON.",
            raw,
        )
    elif from_m:
        try:
            inline = json.loads(from_m.group("inline"))
        except json.JSONDecodeError as exc:
            return _unsupported(
                f"The inline JSON is not valid JSON ({exc.msg}).", raw
            )
        body = (body[: from_m.start()] + body[from_m.end() :]).strip()

    # 2. Then 'titled "..."' (removed before 'containing' so the two
    #    clauses cannot swallow each other).
    title = ""
    title_m = _TITLE_RE.search(body)
    if title_m:
        title = _strip_quotes(title_m.group("title"))
        body = (body[: title_m.start()] + body[title_m.end() :]).strip()
        if not title:
            return _unsupported("The title after 'titled' is empty.", raw)

    # 3. Then 'containing ...' greedily to the end of the command.
    text_content = ""
    cont_m = _CONTAINING_RE.search(body)
    if cont_m:
        text_content = _strip_quotes(cont_m.group("text"))
        body = (body[: cont_m.start()] + body[cont_m.end() :]).strip()
        if not text_content:
            return _unsupported("The text after 'containing' is empty.", raw)

    # 4. Finally the document noun, optional file name and location.
    m = _DOC_NOUN_RE.match(body)
    if not m:
        return _unsupported(
            "I could not find a document type or file name in that command. "
            "Try: 'Create a report named summary.txt in Documents containing "
            "Hello' or 'Create a spreadsheet named sales.xlsx in Documents "
            "using sales.json'.",
            raw,
        )

    kind = re.sub(r"\s+", " ", m.group("kind")).lower()
    if kind == "report":
        intent = CREATE_REPORT
    elif kind == "pdf":
        intent = CREATE_PDF
    else:
        intent = CREATE_SPREADSHEET

    name = normalize_name(m.group("name"))
    # The output folder may come from the noun clause or from a trailing
    # 'using <file> in <folder>' clause.
    location = normalize_location(m.group("location")) or normalize_location(
        source_location
    )

    if intent == CREATE_SPREADSHEET:
        if source == "" and inline is None:
            return _unsupported(_SPREADSHEET_SPEC_HELP, raw)
        if title or text_content:
            return _unsupported(
                "'titled'/'containing' describe reports and PDFs; a "
                "spreadsheet takes its content from the JSON specification.",
                raw,
            )
        slots: Dict[str, object] = {"name": name, "location": location, "source": source}
        if inline is not None:
            slots["inline"] = inline
        return ParsedIntent(CREATE_SPREADSHEET, slots, raw)

    # Report / PDF: a structured source XOR simple titled/containing text.
    has_simple = bool(title or text_content)
    has_structured = bool(source) or inline is not None
    if has_structured and has_simple:
        return _unsupported(
            "Use either a JSON content file/inline JSON or simple "
            "'titled ...' / 'containing ...' text, not both.",
            raw,
        )
    if not has_structured and not has_simple:
        return _unsupported(_REPORT_CONTENT_HELP, raw)

    slots = {
        "name": name,
        "location": location,
        "source": source,
        "title": title,
        "text": text_content,
    }
    if inline is not None:
        slots["inline"] = inline
    return ParsedIntent(intent, slots, raw)


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

    doc_intent = _parse_document(text, raw)
    if doc_intent is not None:
        return doc_intent

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
        "file with content, read a text file, create a report, PDF or "
        "spreadsheet from structured content, explain an operation. Type "
        "'help' for examples.",
        raw,
    )
