"""PDF rendering with reportlab (optional) and structural validation.

Rendering happens in memory (``BytesIO``) and returns bytes; the tools
module decides where they are written (always with ``O_EXCL``).

Text is drawn with the built-in Helvetica fonts and encoded as
latin-1: characters outside that set are replaced, never crashed on.
PDF *generation* needs reportlab, but :func:`validate_pdf_bytes` is
pure standard library so verification never depends on it.
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Tuple

from integrations.documents import deps

PAGE_MARGIN = 54  # points (~0.75 inch)
TITLE_WRAP = 40
PARAGRAPH_WRAP = 88


def _latin1(text: str) -> str:
    """Reportlab draws WinAnsi; replace anything outside latin-1."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


def render_pdf(content: dict) -> Tuple[bytes, int]:
    """Render validated report *content* into PDF bytes.

    Returns ``(pdf_bytes, page_count)``.  Raises
    :class:`~integrations.documents.deps.MissingDependencyError` when
    reportlab is not installed.
    """
    deps.require("reportlab")
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as rl_canvas

    buffer = BytesIO()
    page_width, page_height = letter
    canvas = rl_canvas.Canvas(buffer, pagesize=letter, pageCompression=0)
    left = PAGE_MARGIN
    bottom = PAGE_MARGIN
    top = page_height - PAGE_MARGIN
    state = {"y": top, "pages": 1}

    def new_page() -> None:
        canvas.showPage()
        state["pages"] += 1
        state["y"] = top

    def ensure(space: float) -> None:
        if state["y"] - space < bottom:
            new_page()

    def draw_wrapped(font: str, size: float, text: str, width: int, indent: int = 0) -> None:
        import textwrap

        canvas.setFont(font, size)
        leading = size * 1.35
        for line in textwrap.wrap(text, width) or [""]:
            ensure(leading)
            canvas.drawString(left + indent, state["y"], _latin1(line))
            state["y"] -= leading

    title = content.get("title")
    if title:
        draw_wrapped("Helvetica-Bold", 16, title, TITLE_WRAP)
        state["y"] -= 4
        ensure(12)
        canvas.setLineWidth(1)
        canvas.line(left, state["y"], page_width - PAGE_MARGIN, state["y"])
        state["y"] -= 16

    for block in content.get("blocks", []):
        btype = block.get("type")

        if btype == "heading":
            level = block.get("level", 1)
            draw_wrapped("Helvetica-Bold", {1: 13, 2: 12, 3: 11}.get(level, 11),
                         block["text"], 70)
            state["y"] -= 4

        elif btype == "paragraph":
            draw_wrapped("Helvetica", 10, block["text"], PARAGRAPH_WRAP)
            state["y"] -= 6

        else:  # list
            items = block.get("items", [])
            numbered = block.get("style") == "numbered"
            for index, item in enumerate(items, 1):
                marker = f"{index}." if numbered else "-"
                draw_wrapped("Helvetica", 10, f"{marker} {item}",
                             PARAGRAPH_WRAP - 4, indent=0)
            state["y"] -= 6

    canvas.save()
    return buffer.getvalue(), state["pages"]


# --------------------------------------------------------------------------- #
# Structural validation (pure standard library)
# --------------------------------------------------------------------------- #

_OBJECT_RE = re.compile(rb"\d+\s+\d+\s+obj")
_STARTXREF_RE = re.compile(rb"startxref\s+(\d+)")


def validate_pdf_bytes(data: bytes) -> Tuple[bool, str]:
    """Structurally validate PDF *data*.

    Returns ``(ok, detail)``.  Checks the ``%PDF-`` signature, a
    non-trivial object count, the document catalog, a ``%%EOF`` marker
    near the end and a ``startxref`` offset that points inside the
    file (at a classic ``xref`` table when possible).
    """
    if not isinstance(data, (bytes, bytearray)):
        return False, f"expected bytes, got {type(data).__name__}"
    data = bytes(data)
    if len(data) < 64:
        return False, f"file is too small to be a PDF ({len(data)} bytes)"
    if not data.startswith(b"%PDF-"):
        return False, "missing '%PDF-' file signature"

    tail = data[-1024:]
    if b"%%EOF" not in tail:
        return False, "missing '%%EOF' marker near the end of the file"

    match = _STARTXREF_RE.search(tail)
    if not match:
        return False, "missing 'startxref' near the end of the file"
    offset = int(match.group(1))
    if offset <= 0 or offset >= len(data):
        return False, f"'startxref' offset {offset} points outside the file"

    if b"/Catalog" not in data:
        return False, "missing the document catalog (/Catalog)"

    objects = _OBJECT_RE.findall(data)
    if len(objects) < 3:
        return False, f"only {len(objects)} indirect objects found"

    if data[offset : offset + 4] == b"xref":
        detail = f"{len(objects)} objects, classic xref table at {offset}"
    else:
        detail = f"{len(objects)} objects, xref reference at {offset}"
    return True, detail
