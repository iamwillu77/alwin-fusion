"""Validation of structured document and spreadsheet content.

This module is part of the *trusted* core and depends only on the
standard library.  Everything it validates arrives untrusted — from
user text or a local model proposal — so it is deliberately strict:

* closed key sets per structure (no unknown fields),
* bounded sizes (no memory-exhaustion inputs),
* scalar cell values only, never row strings starting with ``=``
  (openpyxl would silently turn those into formulas, bypassing
  formula validation),
* formulas checked against a conservative grammar: allow-listed
  function names, no external-workbook links, no cross-sheet
  references, no undefined names, and every referenced cell inside
  the declared grid.

Errors are raised as :class:`SpecError`; the planner converts them to
``PlanValidationError`` so callers see a single exception type.
"""

from __future__ import annotations

import math
import re
from typing import List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class SpecError(Exception):
    """A structured content specification is invalid."""


# --------------------------------------------------------------------------- #
# Generic limits and helpers
# --------------------------------------------------------------------------- #

MAX_PATH_LEN = 1024
MAX_FILENAME_LEN = 120

#: Characters rejected anywhere in an output file name (control chars
#: plus characters that are unsafe on common filesystems).
UNSAFE_FILENAME_CHARS = set('<>:"|?*')
_TRAILING_NAME_CHARS = " ."

MAX_REPORT_TITLE = 200
MAX_BLOCKS = 200
MAX_BLOCK_TEXT = 5000
MAX_LIST_ITEMS = 500

MAX_SHEETS = 16
MAX_HEADERS = 100
MAX_ROWS = 5000
MAX_FORMULAS = 1000
MAX_FORMATS = 64
#: Global bound on headers + row cells across *all* sheets, so a
#: spec cannot exhaust memory (openpyxl holds every cell in RAM).
MAX_TOTAL_CELLS = 200_000
MAX_CELL_TEXT = 32767  # Excel's own per-cell limit
MAX_SHEET_NAME = 31
INVALID_SHEET_NAME_CHARS = set("[]:*?/\\")

MAX_FORMULA_LEN = 512

CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

NUMBER_FORMATS = frozenset(
    {
        "General",
        "0",
        "0.00",
        "0.000",
        "#,##0",
        "#,##0.00",
        "0%",
        "0.00%",
        "$#,##0.00",
        "yyyy-mm-dd",
        "mm/dd/yyyy",
        "dd/mm/yyyy",
        "hh:mm",
        "hh:mm:ss",
        "@",
    }
)

#: Conservative spreadsheet function allowlist.  Notably excluded:
#: WEBSERVICE / IMAGE / FILTERXML (network), HYPERLINK (navigation),
#: CALL / REGISTER / RUN (native code / macros).
ALLOWED_FORMULA_FUNCTIONS = frozenset(
    {
        "ABS", "AND", "AVERAGE", "AVERAGEIF", "CEILING", "CONCAT", "CONCATENATE",
        "COUNT", "COUNTA", "COUNTIF", "DATE", "DATEVALUE", "DAY", "EXACT",
        "FLOOR", "IF", "IFERROR", "IFS", "INT", "ISBLANK", "ISERROR",
        "ISNUMBER", "ISTEXT", "LEFT", "LEN", "LOWER", "MATCH", "MAX", "MID",
        "MIN", "MOD", "MONTH", "NOT", "NOW", "OR", "POWER", "PROPER", "ROUND",
        "ROUNDDOWN", "ROUNDUP", "RIGHT", "SQRT", "SUBSTITUTE", "SUM",
        "SUMIF", "SUMIFS", "TEXT", "TRIM", "TODAY", "UPPER", "VALUE",
        "VLOOKUP", "HLOOKUP", "YEAR",
    }
)

A1_CELL_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})$")
A1_RANGE_RE = re.compile(
    r"^\$?[A-Za-z]{1,3}\$?\d{1,7}:\$?[A-Za-z]{1,3}\$?\d{1,7}$"
)
COL_LETTERS_RE = re.compile(r"^[A-Z]{1,3}$")

MAX_EXCEL_COL = 16384  # XFD
MAX_EXCEL_ROW = 1048576


def _err(field: str, message: str) -> SpecError:
    return SpecError(f"{field}: {message}")


def _check_str(
    value: object, field: str, *, max_len: int, allow_empty: bool = False
) -> str:
    if not isinstance(value, str):
        raise _err(field, f"must be text, got {type(value).__name__}.")
    if not allow_empty and not value.strip():
        raise _err(field, "must not be empty.")
    if len(value) > max_len:
        raise _err(field, f"is too long (limit {max_len} characters).")
    if CONTROL_CHAR_RE.search(value):
        raise _err(field, "must not contain control characters.")
    return value


def _reject_unknown_keys(data: dict, allowed: frozenset, field: str) -> None:
    extra = set(data) - allowed
    if extra:
        raise _err(field, f"unknown field(s): {', '.join(sorted(extra))}.")


def col_letters_to_index(letters: str) -> int:
    """Turn 'A' -> 1, 'Z' -> 26, 'AA' -> 27 (1-based)."""
    num = 0
    for ch in letters.upper():
        num = num * 26 + (ord(ch) - ord("A") + 1)
    return num


def parse_a1(token: str) -> Tuple[int, int]:
    """Parse an A1-style reference into (row, col), both 1-based."""
    m = A1_CELL_RE.match(token.strip())
    if not m:
        raise _err("cell reference", f"'{token}' is not a valid A1-style cell.")
    col = col_letters_to_index(m.group(1))
    row = int(m.group(2))
    if row < 1 or row > MAX_EXCEL_ROW:
        raise _err("cell reference", f"row {row} is outside Excel's limits.")
    if col < 1 or col > MAX_EXCEL_COL:
        raise _err("cell reference", f"column {token[:2].upper()} is outside Excel's limits.")
    return row, col


def _check_within_grid(token: str, grid_rows: int, grid_cols: int, field: str) -> None:
    row, col = parse_a1(token)
    if row > grid_rows or col > grid_cols:
        raise _err(
            field,
            f"'{token}' is outside the declared data grid "
            f"(rows 1..{grid_rows}, columns 1..{grid_cols}). "
            f"Declare the row/column first, e.g. add empty cells to 'rows'.",
        )


# --------------------------------------------------------------------------- #
# File-name / extension rules
# --------------------------------------------------------------------------- #


def ensure_extension(path: str, extension: str) -> str:
    """Append *extension* when the path has none; reject any other one."""
    name = path.rsplit("/", 1)[-1]
    if "." in name and not name.endswith("."):
        suffix = "." + name.rsplit(".", 1)[-1]
        if suffix.lower() != extension:
            raise _err(
                "path",
                f"file name '{name}' has extension '{suffix}' but this "
                f"operation produces '{extension}' files.",
            )
        return path
    return path + extension


def validate_output_filename(path: str) -> str:
    """Enforce safe output file names for generated documents."""
    name = path.rsplit("/", 1)[-1]
    if not name:
        raise _err("path", "file name is empty.")
    if len(name) > MAX_FILENAME_LEN:
        raise _err("path", f"file name is too long (limit {MAX_FILENAME_LEN} characters).")
    if name.startswith("."):
        raise _err("path", "hidden file names (starting with '.') are not allowed.")
    if CONTROL_CHAR_RE.search(name) or any(c in UNSAFE_FILENAME_CHARS for c in name):
        raise _err("path", "file name contains unsafe characters.")
    if name[-1] in _TRAILING_NAME_CHARS:
        raise _err("path", "file name must not end with a space or dot.")
    return path


# --------------------------------------------------------------------------- #
# Text reports
# --------------------------------------------------------------------------- #

REPORT_BLOCK_TYPES = ("heading", "paragraph", "list")
REPORT_KEYS = {
    "heading": frozenset({"type", "level", "text"}),
    "paragraph": frozenset({"type", "text"}),
    "list": frozenset({"type", "style", "items"}),
}
LIST_STYLES = ("bullet", "numbered")


def validate_report_content(content: object) -> dict:
    """Validate and normalize report/PDF content.

    Expected shape::

        {"title": "Optional",
         "blocks": [
            {"type": "heading", "level": 1, "text": "..."},
            {"type": "paragraph", "text": "..."},
            {"type": "list", "style": "bullet|numbered", "items": ["...", ...]}
         ]}
    """
    if not isinstance(content, dict):
        raise SpecError(
            f"content must be a JSON object with 'title' and/or 'blocks', "
            f"got {type(content).__name__}."
        )
    _reject_unknown_keys(content, frozenset({"title", "blocks"}), "content")

    title_raw = content.get("title")
    title: Optional[str] = None
    if title_raw is not None:
        title = _check_str(title_raw, "content.title", max_len=MAX_REPORT_TITLE)

    blocks_raw = content.get("blocks", [])
    if not isinstance(blocks_raw, list):
        raise _err("content.blocks", "must be a list of blocks.")
    if len(blocks_raw) > MAX_BLOCKS:
        raise _err("content.blocks", f"too many blocks (limit {MAX_BLOCKS}).")
    if not blocks_raw and not title:
        raise SpecError(
            "content must include a 'title' or at least one block "
            "(heading/paragraph/list)."
        )

    blocks: List[dict] = []
    for index, block in enumerate(blocks_raw):
        field = f"content.blocks[{index}]"
        if not isinstance(block, dict):
            raise _err(field, f"must be an object, got {type(block).__name__}.")
        btype = block.get("type")
        if btype not in REPORT_KEYS:
            raise _err(
                field,
                f"unknown block type {btype!r}; allowed: {', '.join(REPORT_BLOCK_TYPES)}.",
            )
        _reject_unknown_keys(block, REPORT_KEYS[btype], field)

        if btype == "heading":
            level = block.get("level", 1)
            if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 3:
                raise _err(f"{field}.level", "must be an integer from 1 to 3.")
            text = _check_str(block.get("text"), f"{field}.text", max_len=MAX_BLOCK_TEXT)
            blocks.append({"type": "heading", "level": level, "text": text})

        elif btype == "paragraph":
            text = _check_str(block.get("text"), f"{field}.text", max_len=MAX_BLOCK_TEXT)
            blocks.append({"type": "paragraph", "text": text})

        else:  # list
            style = block.get("style", "bullet")
            if style not in LIST_STYLES:
                raise _err(
                    f"{field}.style", f"must be one of: {', '.join(LIST_STYLES)}."
                )
            items = block.get("items")
            if not isinstance(items, list) or not items:
                raise _err(f"{field}.items", "must be a non-empty list.")
            if len(items) > MAX_LIST_ITEMS:
                raise _err(f"{field}.items", f"too many items (limit {MAX_LIST_ITEMS}).")
            checked = [
                _check_str(item, f"{field}.items[{i}]", max_len=MAX_BLOCK_TEXT)
                for i, item in enumerate(items)
            ]
            blocks.append({"type": "list", "style": style, "items": checked})

    return {"title": title, "blocks": blocks}


# --------------------------------------------------------------------------- #
# Formulas
# --------------------------------------------------------------------------- #

_FORMULA_TOKEN_RE = re.compile(
    r"""
      "(?:[^"]|"")*"                                             # string literal
    | \$?[A-Za-z]{1,3}\$?\d{1,7}(?::\$?[A-Za-z]{1,3}\$?\d{1,7})? # cell or range
    | [A-Za-z][A-Za-z0-9_.]*\s*(?=\()                            # function name
    | \d+(?:\.\d+)?(?:[eE][+-]?\d+)?                             # number
    | [+\-*/^%&<>=,()]                                           # operators
    """,
    re.VERBOSE,
)


def _validate_formula_refs(
    token: str, grid_rows: int, grid_cols: int, field: str
) -> None:
    """Validate a cell or range token against the declared grid."""
    if ":" in token:
        left, right = token.split(":", 1)
        _check_within_grid(left, grid_rows, grid_cols, field)
        _check_within_grid(right, grid_rows, grid_cols, field)
        l_row, l_col = parse_a1(left)
        r_row, r_col = parse_a1(right)
        if l_row > r_row or l_col > r_col:
            raise _err(field, f"range '{token}' is reversed.")
    else:
        _check_within_grid(token, grid_rows, grid_cols, field)


def validate_formula(formula: object, grid_rows: int, grid_cols: int) -> str:
    """Validate one formula against the sheet's declared data grid.

    The formula is *written as text only*; nothing is calculated here
    (and openpyxl never calculates either).  Validation enforces:

    * starts with ``=``, bounded length,
    * no external workbook links (``[``/``]``),
    * no cross-sheet references (``!``),
    * only allow-listed function names,
    * balanced parentheses,
    * every cell/range reference inside the declared grid,
    * no unknown names or unexpected characters.
    """
    if not isinstance(formula, str):
        raise _err("formula", f"must be text, got {type(formula).__name__}.")
    if len(formula) > MAX_FORMULA_LEN:
        raise _err("formula", f"is too long (limit {MAX_FORMULA_LEN} characters).")
    if not formula.startswith("="):
        raise _err("formula", "must start with '='.")
    body = formula[1:]
    if not body.strip():
        raise _err("formula", "is empty after '='.")
    if "[" in body or "]" in body:
        raise _err("formula", "external workbook links ('['/']') are not allowed.")
    if "!" in body:
        raise _err(
            "formula", "cross-sheet references ('!') are not supported in this version."
        )
    if "{" in body or "}" in body:
        raise _err("formula", "unexpected '{' or '}'.")

    pos = 0
    depth = 0
    saw_content = False
    while pos < len(body):
        m = _FORMULA_TOKEN_RE.match(body, pos)
        if not m:
            snippet = body[pos : pos + 24]
            raise _err(
                "formula",
                f"unsupported expression near '{snippet}' (only simple "
                f"cell references, numbers and allow-listed functions are allowed).",
            )
        token = m.group(0)
        stripped = token.strip()
        pos = m.end()

        if token.startswith('"'):
            saw_content = True
            continue

        # Cell / range token?
        cellish = token.replace("$", "")
        if re.fullmatch(r"[A-Za-z]{1,3}\d{1,7}(:[A-Za-z]{1,3}\d{1,7})?", cellish):
            rest = body[pos:].lstrip()
            if rest.startswith("("):
                raise _err(
                    "formula",
                    f"'{stripped}' is not a known function and cannot be called.",
                )
            _validate_formula_refs(
                token, grid_rows, grid_cols, f"formula reference '{stripped}'"
            )
            saw_content = True
            continue

        # Function name? The regex matches it with a '(' lookahead, so the
        # parenthesis itself is still unconsumed in the body.
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", stripped) and body[pos:].lstrip().startswith("("):
            name = stripped.upper()
            if name not in ALLOWED_FORMULA_FUNCTIONS:
                raise _err(
                    "formula",
                    f"function '{name}' is not allowed. Allowed functions: "
                    f"{', '.join(sorted(ALLOWED_FORMULA_FUNCTIONS))}.",
                )
            saw_content = True
            continue

        if re.fullmatch(r"\d+(\.\d+)?([eE][+-]?\d+)?", stripped):
            saw_content = True
            continue

        if stripped == "(":
            depth += 1
        elif stripped == ")":
            depth -= 1
            if depth < 0:
                raise _err("formula", "unbalanced parentheses.")
        elif stripped in "+-*/^%&<>=":
            pass
        elif stripped == ",":
            pass
        else:  # pragma: no cover - regex cannot reach here
            raise _err("formula", f"unexpected token '{stripped}'.")
        saw_content = True

    if depth != 0:
        raise _err("formula", "unbalanced parentheses.")
    if not saw_content:
        raise _err("formula", "is empty.")
    return formula


# --------------------------------------------------------------------------- #
# Spreadsheet specification
# --------------------------------------------------------------------------- #

SHEET_KEYS = frozenset(
    {"name", "headers", "rows", "formulas", "number_formats", "column_widths", "styles"}
)
STYLES_KEYS = frozenset({"header_bold", "freeze_header", "header_fill"})
SPEC_KEYS = frozenset({"title", "sheets"})


def _validate_sheet_name(name: object, seen: set) -> str:
    if not isinstance(name, str) or not name.strip():
        raise _err("sheet.name", "must be non-empty text.")
    if len(name) > MAX_SHEET_NAME:
        raise _err("sheet.name", f"too long (limit {MAX_SHEET_NAME} characters).")
    if CONTROL_CHAR_RE.search(name):
        raise _err("sheet.name", "must not contain control characters.")
    bad = set(name) & INVALID_SHEET_NAME_CHARS
    if bad:
        raise _err(
            "sheet.name", f"contains invalid character(s): {''.join(sorted(bad))}."
        )
    if name.startswith("'") or name.endswith("'"):
        raise _err("sheet.name", "must not start or end with an apostrophe.")
    key = name.casefold()
    if key in seen:
        raise _err("sheet.name", f"duplicate sheet name '{name}'.")
    seen.add(key)
    return name


def _validate_cell_value(value: object, field: str) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _err(field, "must be a finite number.")
        return value
    if isinstance(value, str):
        if value.startswith("="):
            raise _err(
                field,
                "cell strings must not start with '=' (they would become "
                "formulas); put computed cells in the 'formulas' list.",
            )
        if len(value) > MAX_CELL_TEXT:
            raise _err(field, f"too long (limit {MAX_CELL_TEXT} characters).")
        if CONTROL_CHAR_RE.search(value):
            raise _err(field, "must not contain control characters.")
        return value
    raise _err(
        field,
        f"must be a number, text, true/false or null, got {type(value).__name__}.",
    )


def _grid_extent(headers: Sequence[object], rows: Sequence[Sequence[object]]) -> Tuple[int, int]:
    grid_rows = (1 if headers else 0) + len(rows)
    grid_cols = len(headers)
    for row in rows:
        grid_cols = max(grid_cols, len(row))
    return grid_rows, grid_cols


def validate_spreadsheet_spec(spec: object) -> dict:
    """Validate and normalize a workbook specification.

    Expected shape::

        {"title": "Optional",
         "sheets": [{
            "name": "Sales",
            "headers": ["Region", "Amount"],
            "rows": [["North", 100], ["South", 250.5]],
            "formulas": [{"cell": "B4", "formula": "=SUM(B2:B3)"}],
            "number_formats": {"B2:B3": "#,##0.00"},
            "column_widths": {"A": 16},
            "styles": {"header_bold": true, "freeze_header": true,
                       "header_fill": "#DDEBF7"}
         }]}
    """
    if not isinstance(spec, dict):
        raise SpecError(
            f"spec must be a JSON object with a 'sheets' list, got "
            f"{type(spec).__name__}."
        )
    _reject_unknown_keys(spec, SPEC_KEYS, "spec")

    title = None
    if spec.get("title") is not None:
        title = _check_str(spec["title"], "spec.title", max_len=MAX_REPORT_TITLE)

    sheets_raw = spec.get("sheets")
    if not isinstance(sheets_raw, list) or not sheets_raw:
        raise _err("spec.sheets", "must be a non-empty list.")
    if len(sheets_raw) > MAX_SHEETS:
        raise _err("spec.sheets", f"too many sheets (limit {MAX_SHEETS}).")

    seen_names: set = set()
    sheets: List[dict] = []
    total_cells = 0
    for index, sheet in enumerate(sheets_raw):
        field = f"spec.sheets[{index}]"
        if not isinstance(sheet, dict):
            raise _err(field, f"must be an object, got {type(sheet).__name__}.")
        _reject_unknown_keys(sheet, SHEET_KEYS, field)

        name = _validate_sheet_name(sheet.get("name"), seen_names)

        headers_raw = sheet.get("headers", [])
        if not isinstance(headers_raw, list):
            raise _err(f"{field}.headers", "must be a list.")
        if len(headers_raw) > MAX_HEADERS:
            raise _err(f"{field}.headers", f"too many columns (limit {MAX_HEADERS}).")
        headers = [
            _check_str(h, f"{field}.headers[{i}]", max_len=MAX_CELL_TEXT)
            for i, h in enumerate(headers_raw)
        ]

        rows_raw = sheet.get("rows", [])
        if not isinstance(rows_raw, list):
            raise _err(f"{field}.rows", "must be a list of rows.")
        if len(rows_raw) > MAX_ROWS:
            raise _err(f"{field}.rows", f"too many rows (limit {MAX_ROWS}).")
        rows: List[List[object]] = []
        for r_index, row in enumerate(rows_raw):
            if not isinstance(row, list):
                raise _err(
                    f"{field}.rows[{r_index}]",
                    f"must be a list, got {type(row).__name__}.",
                )
            if headers and len(row) > len(headers):
                raise _err(
                    f"{field}.rows[{r_index}]",
                    f"has {len(row)} values but there are only "
                    f"{len(headers)} headers.",
                )
            rows.append(
                [
                    _validate_cell_value(v, f"{field}.rows[{r_index}][{c_index}]")
                    for c_index, v in enumerate(row)
                ]
            )

        grid_rows, grid_cols = _grid_extent(headers, rows)
        if grid_rows < 1 or grid_cols < 1:
            raise _err(field, "sheet must declare headers or rows (it would be empty).")

        formulas_raw = sheet.get("formulas", [])
        if not isinstance(formulas_raw, list):
            raise _err(f"{field}.formulas", "must be a list.")
        if len(formulas_raw) > MAX_FORMULAS:
            raise _err(
                f"{field}.formulas", f"too many formulas (limit {MAX_FORMULAS})."
            )
        formulas: List[dict] = []
        seen_cells: set = set()
        for f_index, entry in enumerate(formulas_raw):
            f_field = f"{field}.formulas[{f_index}]"
            if not isinstance(entry, dict):
                raise _err(f_field, f"must be an object, got {type(entry).__name__}.")
            _reject_unknown_keys(entry, frozenset({"cell", "formula"}), f_field)
            cell = entry.get("cell")
            if not isinstance(cell, str):
                raise _err(f"{f_field}.cell", "must be text like 'B4'.")
            _check_within_grid(cell, grid_rows, grid_cols, f"{f_field}.cell")
            cell_key = cell.replace("$", "").upper()
            if cell_key in seen_cells:
                raise _err(f_field, f"cell '{cell}' has more than one formula.")
            seen_cells.add(cell_key)
            formula = validate_formula(entry.get("formula"), grid_rows, grid_cols)
            formulas.append({"cell": cell, "formula": formula})

        formats_raw = sheet.get("number_formats", {})
        if not isinstance(formats_raw, dict):
            raise _err(f"{field}.number_formats", "must be an object mapping cells to formats.")
        if len(formats_raw) > MAX_FORMATS:
            raise _err(
                f"{field}.number_formats", f"too many entries (limit {MAX_FORMATS})."
            )
        number_formats = {}
        for key, fmt in formats_raw.items():
            n_field = f"{field}.number_formats['{key}']"
            if not isinstance(key, str) or not A1_RANGE_RE.match(key) and not A1_CELL_RE.match(key):
                raise _err(n_field, "must be a cell like 'B2' or a range like 'B2:B3'.")
            if not isinstance(fmt, str) or fmt not in NUMBER_FORMATS:
                raise _err(
                    n_field,
                    f"format must be one of: {', '.join(sorted(NUMBER_FORMATS))}.",
                )
            _validate_formula_refs(key, grid_rows, grid_cols, n_field)
            number_formats[key] = fmt

        widths_raw = sheet.get("column_widths", {})
        if not isinstance(widths_raw, dict):
            raise _err(f"{field}.column_widths", "must be an object.")
        if len(widths_raw) > MAX_FORMATS:
            raise _err(f"{field}.column_widths", f"too many entries (limit {MAX_FORMATS}).")
        column_widths = {}
        for key, width in widths_raw.items():
            w_field = f"{field}.column_widths['{key}']"
            if not isinstance(key, str) or not COL_LETTERS_RE.match(key):
                raise _err(w_field, "must be column letters like 'A' or 'BC'.")
            if col_letters_to_index(key) > MAX_EXCEL_COL:
                raise _err(w_field, "column is outside Excel's limits.")
            if isinstance(width, bool) or not isinstance(width, (int, float)):
                raise _err(w_field, "must be a number.")
            if not 0 <= float(width) <= 255:
                raise _err(w_field, "must be between 0 and 255.")
            column_widths[key.upper()] = float(width)

        styles_raw = sheet.get("styles", {})
        if not isinstance(styles_raw, dict):
            raise _err(f"{field}.styles", "must be an object.")
        _reject_unknown_keys(styles_raw, STYLES_KEYS, f"{field}.styles")
        if not headers:
            # Only the options that actually need a header row are an
            # error; the validator's own normalized output (defaults for
            # a headerless sheet) must re-validate cleanly.
            needs_headers = styles_raw.get("freeze_header") is True or (
                styles_raw.get("header_fill") is not None
            )
            if needs_headers:
                raise _err(f"{field}.styles", "formatting options require 'headers'.")
        styles = {"header_bold": True, "freeze_header": False, "header_fill": None}
        if "header_bold" in styles_raw:
            if not isinstance(styles_raw["header_bold"], bool):
                raise _err(f"{field}.styles.header_bold", "must be true or false.")
            styles["header_bold"] = styles_raw["header_bold"]
        if "freeze_header" in styles_raw:
            if not isinstance(styles_raw["freeze_header"], bool):
                raise _err(f"{field}.styles.freeze_header", "must be true or false.")
            styles["freeze_header"] = styles_raw["freeze_header"]
        if "header_fill" in styles_raw:
            fill = styles_raw["header_fill"]
            if fill is not None and (
                not isinstance(fill, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", fill)
            ):
                raise _err(
                    f"{field}.styles.header_fill", "must be null or a '#RRGGBB' color."
                )
            styles["header_fill"] = fill.upper() if isinstance(fill, str) else None

        total_cells += len(headers) + sum(len(r) for r in rows)
        if total_cells > MAX_TOTAL_CELLS:
            raise _err(
                field,
                f"the specification declares more than {MAX_TOTAL_CELLS} cells "
                f"in total; split the work into several workbooks.",
            )

        sheets.append(
            {
                "name": name,
                "headers": headers,
                "rows": rows,
                "formulas": formulas,
                "number_formats": number_formats,
                "column_widths": column_widths,
                "styles": styles,
            }
        )

    return {"title": title, "sheets": sheets}
