"""Deterministic plain-text report rendering (standard library only).

The input is *already validated* by ``ai_core.specs``; rendering is a
pure function of the content, which lets the verifier re-render and
byte-compare the file on disk to prove it holds what was approved.
"""

from __future__ import annotations

import textwrap

#: Total line width of a rendered report, in characters.
WIDTH = 78

_HEADING_MARKS = {1: "#", 2: "##", 3: "###"}


def render_text_report(content: dict) -> str:
    """Render validated report *content* to plain UTF-8 text.

    Layout: a centered title under a ``=`` rule, ``#``-prefixed
    headings, wrapped paragraphs, ``-`` bullets and ``1.`` numbered
    lists.  Always ends with exactly one newline.
    """
    lines = []

    title = content.get("title")
    if title:
        bar = "=" * WIDTH
        lines.append(bar)
        lines.append(title.center(WIDTH))
        lines.append(bar)
        lines.append("")

    for block in content.get("blocks", []):
        btype = block.get("type")

        if btype == "heading":
            level = block.get("level", 1)
            lines.append(f"{_HEADING_MARKS.get(level, '#')} {block['text']}")
            lines.append("")

        elif btype == "paragraph":
            wrapped = textwrap.wrap(block["text"], WIDTH) or [""]
            lines.extend(wrapped)
            lines.append("")

        else:  # list
            items = block.get("items", [])
            if block.get("style") == "numbered":
                marker_width = len(str(len(items))) + 1  # "12."
                for number, item in enumerate(items, 1):
                    marker = f"{number}.".rjust(marker_width)
                    indent = " " * (marker_width + 1)
                    wrapped = textwrap.wrap(item, WIDTH - marker_width - 1) or [""]
                    lines.append(f"{marker} {wrapped[0]}")
                    lines.extend(indent + extra for extra in wrapped[1:])
            else:
                for item in items:
                    wrapped = textwrap.wrap(item, WIDTH - 3) or [""]
                    lines.append(f"- {wrapped[0]}")
                    lines.extend("   " + extra for extra in wrapped[1:])
            lines.append("")

    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"
