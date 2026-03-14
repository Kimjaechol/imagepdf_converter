"""Render ordered document blocks to Markdown."""

from __future__ import annotations

import os
import re
from pathlib import Path

from backend.models.schema import (
    Alignment,
    BlockType,
    HeadingLevel,
    LayoutBlock,
    PageResult,
    TableCell,
    TableStructure,
)


class MarkdownRenderer:
    """Convert structured document blocks into Markdown."""

    def __init__(
        self,
        table_format: str = "pipe",
        footnote_style: str = "reference",
        image_dir: str = "images",
    ):
        self.table_format = table_format  # pipe | html_fallback
        self.footnote_style = footnote_style  # inline | reference
        self.image_dir = image_dir

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def render(self, pages: list[PageResult]) -> str:
        """Render all pages into a single Markdown document."""
        parts: list[str] = []
        footnotes: list[tuple[str, str]] = []

        for page in pages:
            page_md = self._render_page(page, footnotes)
            parts.append(page_md)

        result = "\n\n".join(parts)

        if footnotes:
            result += "\n\n---\n\n"
            for i, (fid, text) in enumerate(footnotes, 1):
                result += f"[^{i}]: {text}\n"

        return result

    # ------------------------------------------------------------------
    # Page rendering
    # ------------------------------------------------------------------

    def _render_page(
        self,
        page: PageResult,
        footnotes: list[tuple[str, str]],
    ) -> str:
        parts: list[str] = []

        for block in page.blocks:
            md = self._render_block(block, footnotes)
            if md:
                parts.append(md)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Block rendering
    # ------------------------------------------------------------------

    def _render_block(
        self,
        block: LayoutBlock,
        footnotes: list[tuple[str, str]],
    ) -> str:
        btype = block.block_type

        if btype == BlockType.HEADING:
            return self._render_heading(block)
        elif btype == BlockType.SUBTITLE:
            return f"**{block.text}**\n\n"
        elif btype == BlockType.PARAGRAPH:
            return self._render_paragraph(block)
        elif btype == BlockType.TABLE:
            return self._render_table(block)
        elif btype in (BlockType.FIGURE, BlockType.EQUATION):
            return self._render_figure(block)
        elif btype == BlockType.LIST:
            return self._render_list(block)
        elif btype == BlockType.CAPTION:
            return self._render_caption(block)
        elif btype in (BlockType.FOOTNOTE, BlockType.ENDNOTE):
            footnotes.append((block.id, block.text))
            return ""
        elif btype == BlockType.BOX:
            return self._render_box(block)
        elif btype == BlockType.BALLOON:
            return self._render_balloon(block)
        elif btype in (BlockType.HEADER, BlockType.FOOTER, BlockType.PAGE_NUMBER):
            return ""
        else:
            if block.text:
                return self._render_paragraph(block)
            return ""

    def _render_heading(self, block: LayoutBlock) -> str:
        level_map = {
            HeadingLevel.H1: "#",
            HeadingLevel.H2: "##",
            HeadingLevel.H3: "###",
            HeadingLevel.H4: "####",
            HeadingLevel.H5: "#####",
            HeadingLevel.H6: "######",
        }
        prefix = level_map.get(block.heading_level, "###")
        text = block.text.strip()
        # Markdown headings are already bold by convention; don't double-wrap
        return f"{prefix} {text}"

    def _render_paragraph(self, block: LayoutBlock) -> str:
        text = block.text.strip()
        # Apply inline formatting
        if block.style:
            if block.style.is_bold:
                text = f"**{text}**"
            if block.style.is_italic:
                text = f"*{text}*"
        # Handle alignment (limited in Markdown)
        if block.style and block.style.alignment == Alignment.CENTER:
            text = f"<div align=\"center\">\n\n{text}\n\n</div>"
        return text

    def _render_table(self, block: LayoutBlock) -> str:
        if not block.table_structure:
            return f"```\n{block.text}\n```"

        ts = block.table_structure

        # For complex tables (spans), use HTML fallback
        has_spans = any(
            c.rowspan > 1 or c.colspan > 1 for c in ts.cells
        )
        if has_spans and self.table_format == "html_fallback":
            return self._render_table_html_fallback(ts)

        return self._render_table_pipe(ts)

    def _render_table_pipe(self, ts: TableStructure) -> str:
        """Render as Markdown pipe table."""
        # Build text grid
        grid: dict[tuple[int, int], str] = {}
        for cell in ts.cells:
            text = cell.text.replace("\n", " <br> ").strip()
            grid[(cell.row, cell.col)] = text

        # Compute column widths
        col_widths: dict[int, int] = {}
        for c in range(ts.num_cols):
            max_w = 3  # minimum
            for r in range(ts.num_rows):
                text = grid.get((r, c), "")
                max_w = max(max_w, len(text))
            col_widths[c] = min(max_w, 40)

        lines: list[str] = []

        for r in range(ts.num_rows):
            cells_text = []
            for c in range(ts.num_cols):
                text = grid.get((r, c), "")
                cells_text.append(f" {text} ")
            line = "|" + "|".join(cells_text) + "|"
            lines.append(line)

            # After first row (header), add separator
            if r == 0:
                sep_parts = []
                for c in range(ts.num_cols):
                    sep_parts.append("-" * (col_widths[c] + 2))
                lines.append("|" + "|".join(sep_parts) + "|")

        return "\n".join(lines)

    def _render_table_html_fallback(self, ts: TableStructure) -> str:
        """Render complex tables as HTML within Markdown."""
        grid: dict[tuple[int, int], TableCell] = {}
        for cell in ts.cells:
            grid[(cell.row, cell.col)] = cell

        parts = ['<table>']
        occupied: set[tuple[int, int]] = set()

        for r in range(ts.num_rows):
            tag = "th" if r == 0 else "td"
            parts.append("<tr>")
            for c in range(ts.num_cols):
                if (r, c) in occupied:
                    continue
                cell = grid.get((r, c))
                if cell:
                    attrs = ""
                    if cell.rowspan > 1:
                        attrs += f' rowspan="{cell.rowspan}"'
                    if cell.colspan > 1:
                        attrs += f' colspan="{cell.colspan}"'
                    for dr in range(cell.rowspan):
                        for dc in range(cell.colspan):
                            if dr > 0 or dc > 0:
                                occupied.add((r + dr, c + dc))
                    text = cell.text.replace("\n", "<br>")
                    parts.append(f"<{tag}{attrs}>{text}</{tag}>")
                else:
                    parts.append(f"<{tag}></{tag}>")
            parts.append("</tr>")

        parts.append("</table>")
        return "\n".join(parts)

    def _render_figure(self, block: LayoutBlock) -> str:
        if block.image_path:
            rel_path = os.path.relpath(block.image_path, start=".")
            alt = block.caption or block.block_type.value
            result = f"![{alt}]({rel_path})"
            if block.caption:
                result += f"\n\n*{block.caption}*"
            return result
        return ""

    def _render_list(self, block: LayoutBlock) -> str:
        lines = block.text.split("\n")
        result_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Check if ordered
            m = re.match(r"^(\d+)[\.\)]\s*(.*)", line)
            if m:
                result_lines.append(f"{m.group(1)}. {m.group(2)}")
            else:
                line = re.sub(r"^[-•·]\s*", "", line)
                result_lines.append(f"- {line}")
        return "\n".join(result_lines)

    def _render_caption(self, block: LayoutBlock) -> str:
        return f"*{block.text.strip()}*"

    def _render_box(self, block: LayoutBlock) -> str:
        lines = block.text.strip().split("\n")
        quoted = "\n".join(f"> {line}" for line in lines)
        return quoted

    def _render_balloon(self, block: LayoutBlock) -> str:
        lines = block.text.strip().split("\n")
        quoted = "\n".join(f"> {line}" for line in lines)
        return f"> **Note:**\n{quoted}"


# ---------------------------------------------------------------------------
# Standalone HTML → Markdown converter (for Hancom/office document output)
# ---------------------------------------------------------------------------

def html_to_markdown(html: str) -> str:
    """Convert an HTML string to Markdown.

    This is a lightweight converter used for office document HTML output
    (from Hancom DocsConverter). For structured PDF output, the
    MarkdownRenderer class above is preferred.
    """
    if not html or not html.strip():
        return ""

    # Extract body content if full HTML document
    body_start = html.find("<body")
    body_end = html.rfind("</body>")
    if body_start != -1 and body_end != -1:
        body_close = html.find(">", body_start)
        if body_close != -1:
            html = html[body_close + 1:body_end]

    from html.parser import HTMLParser

    parts: list[str] = []
    tag_stack: list[str] = []
    in_pre = False
    in_table = False
    first_row_done = False
    col_count = 0
    row_col_count = 0

    class MdParser(HTMLParser):
        nonlocal in_pre, in_table, first_row_done, col_count, row_col_count

        def handle_starttag(self, tag, attrs):
            nonlocal in_pre, in_table, first_row_done, col_count, row_col_count
            tag = tag.lower()
            tag_stack.append(tag)

            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(tag[1])
                parts.append("\n" + "#" * level + " ")
            elif tag == "p":
                parts.append("\n")
            elif tag == "br":
                parts.append("\n")
            elif tag in ("strong", "b"):
                parts.append("**")
            elif tag in ("em", "i"):
                parts.append("*")
            elif tag == "li":
                parts.append("\n- ")
            elif tag == "pre":
                in_pre = True
                parts.append("\n```\n")
            elif tag == "code" and not in_pre:
                parts.append("`")
            elif tag == "table":
                in_table = True
                first_row_done = False
                col_count = 0
                parts.append("\n")
            elif tag == "tr":
                row_col_count = 0
                parts.append("\n")
            elif tag in ("td", "th"):
                parts.append("| ")
                row_col_count += 1
            elif tag == "hr":
                parts.append("\n---\n")
            elif tag == "blockquote":
                parts.append("\n> ")
            elif tag == "img":
                attrs_dict = dict(attrs)
                src = attrs_dict.get("src", "")
                alt = attrs_dict.get("alt", "image")
                parts.append(f"![{alt}]({src})")

        def handle_endtag(self, tag):
            nonlocal in_pre, in_table, first_row_done, col_count, row_col_count
            tag = tag.lower()
            if tag_stack and tag_stack[-1] == tag:
                tag_stack.pop()

            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                parts.append("\n\n")
            elif tag == "p":
                parts.append("\n\n")
            elif tag in ("strong", "b"):
                parts.append("**")
            elif tag in ("em", "i"):
                parts.append("*")
            elif tag == "pre":
                in_pre = False
                parts.append("\n```\n")
            elif tag == "code" and not in_pre:
                parts.append("`")
            elif tag in ("td", "th"):
                parts.append(" ")
            elif tag == "tr":
                parts.append("|")
                if not first_row_done:
                    col_count = row_col_count
                    first_row_done = True
                    parts.append("\n")
                    parts.append("|".join(["---"] * col_count))
                    if col_count > 0:
                        parts.append("|")
            elif tag == "table":
                in_table = False
                parts.append("\n")
            elif tag == "blockquote":
                parts.append("\n")

        def handle_data(self, data):
            if in_pre:
                parts.append(data)
            else:
                parts.append(data)

    try:
        parser = MdParser()
        parser.feed(html)
    except Exception:
        # Fallback: strip tags
        result = re.sub(r"<[^>]+>", "", html)
        return result.strip()

    result = "".join(parts)
    # Collapse excessive newlines
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result.strip()
