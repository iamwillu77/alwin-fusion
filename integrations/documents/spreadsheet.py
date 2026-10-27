"""Workbook building and inspection with openpyxl (optional).

``build_workbook`` turns a *validated* spec (see
``ai_core.specs.validate_spreadsheet_spec``) into .xlsx bytes in
memory; ``inspect_workbook`` reopens such bytes and reports what they
really contain, so the verifier can prove the file on disk holds the
approved headers, rows, formulas, formats and widths.

Formula cells are written exactly as given and are **never
calculated** — openpyxl stores the formula text and a spreadsheet
application computes the result when the file is opened.
"""

from __future__ import annotations

from io import BytesIO
from typing import Dict, List

from integrations.documents import deps


def build_workbook(spec: dict) -> bytes:
    """Build .xlsx bytes from a validated workbook *spec*.

    Raises :class:`~integrations.documents.deps.MissingDependencyError`
    when openpyxl is not installed.
    """
    deps.require("openpyxl")
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    default_sheet = workbook.active

    if spec.get("title"):
        workbook.properties.title = spec["title"]

    for index, sheet in enumerate(spec["sheets"]):
        worksheet = default_sheet if index == 0 else workbook.create_sheet()
        worksheet.title = sheet["name"]
        headers = sheet["headers"]
        rows = sheet["rows"]
        styles = sheet["styles"]

        # Headers (row 1) with optional styling.
        if headers:
            for column, header in enumerate(headers, start=1):
                cell = worksheet.cell(row=1, column=column, value=header)
                if styles.get("header_bold", True):
                    cell.font = Font(bold=True)
                fill = styles.get("header_fill")
                if fill:
                    cell.fill = PatternFill("solid", fgColor=fill.lstrip("#"))
            if styles.get("freeze_header"):
                worksheet.freeze_panes = "A2"

        # Data rows.
        for row_offset, row in enumerate(rows, start=2 if headers else 1):
            for column, value in enumerate(row, start=1):
                if value is not None:
                    worksheet.cell(row=row_offset, column=column, value=value)

        # Formulas (written as text; never calculated here).
        for entry in sheet["formulas"]:
            worksheet[entry["cell"]] = entry["formula"]

        # Number formats (cells or ranges).
        for key, number_format in sheet["number_formats"].items():
            for cell in _cells_in(worksheet, key):
                cell.number_format = number_format

        # Column widths.
        for column, width in sheet["column_widths"].items():
            worksheet.column_dimensions[get_column_letter(
                # column_widths keys are validated letters like 'A'/'BC'
                _column_index(column)
            )].width = width

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _column_index(letters: str) -> int:
    number = 0
    for char in letters.upper():
        number = number * 26 + (ord(char) - ord("A") + 1)
    return number


def _cells_in(worksheet, key: str):
    """Yield every cell selected by an A1 key or range."""
    target = worksheet[key]
    if hasattr(target, "value"):  # a single Cell
        yield target
        return
    for row in target:  # tuple of rows (a range like 'B2:B3')
        if hasattr(row, "value"):
            yield row
        else:
            yield from row


def inspect_workbook(data: bytes) -> dict:
    """Reopen .xlsx *data* and report its real content.

    Returns ``{"sheetnames": [...], "sheets": [{"name", "grid",
    "formulas"}]}`` where ``grid`` is a list of rows of raw cell values
    and ``formulas`` maps cell coordinates to their formula strings.
    """
    deps.require("openpyxl")
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(data), data_only=False)
    sheets: List[Dict] = []
    try:
        for worksheet in workbook.worksheets:
            grid = []
            formulas: Dict[str, str] = {}
            for row in worksheet.iter_rows():
                values = []
                for cell in row:
                    value = cell.value
                    values.append(value)
                    if isinstance(value, str) and value.startswith("="):
                        formulas[cell.coordinate] = value
                grid.append(values)
            sheets.append({"name": worksheet.title, "grid": grid, "formulas": formulas})
    finally:
        workbook.close()
    return {"sheetnames": [s["name"] for s in sheets], "sheets": sheets}
