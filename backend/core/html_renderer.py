"""Render ordered document blocks to simplified HTML."""

from __future__ import annotations

import html
import os
from pathlib import Path

from backend.models.schema import (
    Alignment,
    BlockType,
    HeadingLevel,
    LayoutBlock,
    PageResult,
    TableCell,
    TableStructure,
    TextStyle,
)


class HtmlRenderer:
    """Convert structured document blocks into clean, simplified HTML."""

    def __init__(
        self,
        simplified: bool = True,
        inline_css: bool = True,
        preserve_font_size_ratio: bool = True,
        image_base_path: str = "images",
    ):
        self.simplified = simplified
        self.inline_css = inline_css
        self.preserve_font_size_ratio = preserve_font_size_ratio
        self.image_base_path = image_base_path

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def render(self, pages: list[PageResult]) -> str:
        """Render all pages into a single HTML document."""
        body_parts: list[str] = []
        footnotes: list[tuple[str, str]] = []  # (id, text)

        for page in pages:
            page_html = self._render_page(page, footnotes)
            body_parts.append(page_html)

        footnotes_html = self._render_footnotes(footnotes)

        return self._wrap_document(
            "\n".join(body_parts),
            footnotes_html,
        )

    # ------------------------------------------------------------------
    # Page rendering
    # ------------------------------------------------------------------

    def _render_page(
        self,
        page: PageResult,
        footnotes: list[tuple[str, str]],
    ) -> str:
        parts: list[str] = []
        parts.append(f'<div class="page" data-page="{page.page_index + 1}">')

        for block in page.blocks:
            block_html = self._render_block(block, footnotes)
            if block_html:
                parts.append(block_html)

        parts.append("</div>")
        return "\n".join(parts)

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
            return ""  # rendered at bottom
        elif btype == BlockType.BOX:
            return self._render_box(block)
        elif btype == BlockType.BALLOON:
            return self._render_balloon(block)
        elif btype in (BlockType.HEADER, BlockType.FOOTER, BlockType.PAGE_NUMBER):
            return ""  # skip headers/footers
        else:
            if block.text:
                return self._render_paragraph(block)
            return ""

    def _render_heading(self, block: LayoutBlock) -> str:
        level_map = {
            HeadingLevel.H1: "h1", HeadingLevel.H2: "h2",
            HeadingLevel.H3: "h3", HeadingLevel.H4: "h4",
            HeadingLevel.H5: "h5", HeadingLevel.H6: "h6",
        }
        tag = level_map.get(block.heading_level, "h3")
        style = self._build_inline_style(block.style)
        text = self._escape(block.text)
        text = self._add_footnote_refs(text, block)
        return f'<{tag}{style}>{text}</{tag}>'

    def _render_paragraph(self, block: LayoutBlock) -> str:
        style = self._build_inline_style(block.style)
        text = self._escape(block.text)
        # Preserve line breaks
        text = text.replace("\n", "<br>\n")
        text = self._add_footnote_refs(text, block)
        return f"<p{style}>{text}</p>"

    def _render_table(self, block: LayoutBlock) -> str:
        if not block.table_structure:
            # Fallback: render text as preformatted
            return f"<pre>{self._escape(block.text)}</pre>"

        ts = block.table_structure
        parts: list[str] = ['<table border="1" cellpadding="4" cellspacing="0">']

        # Build grid
        grid: dict[tuple[int, int], TableCell] = {}
        for cell in ts.cells:
            grid[(cell.row, cell.col)] = cell

        # Detect header rows
        header_rows = set()
        for cell in ts.cells:
            if cell.is_header:
                header_rows.add(cell.row)

        # Render occupied cells tracking
        occupied: set[tuple[int, int]] = set()

        for r in range(ts.num_rows):
            tag = "th" if r in header_rows else "td"
            row_in_section = "thead" if r in header_rows else "tbody"

            if r == 0 and r in header_rows:
                parts.append("<thead>")
            elif r == min(set(range(ts.num_rows)) - header_rows, default=ts.num_rows):
                if header_rows:
                    parts.append("</thead>")
                parts.append("<tbody>")

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
                    # Mark spanned cells
                    for dr in range(cell.rowspan):
                        for dc in range(cell.colspan):
                            if dr > 0 or dc > 0:
                                occupied.add((r + dr, c + dc))
                    text = self._escape(cell.text).replace("\n", "<br>")
                    parts.append(f"<{tag}{attrs}>{text}</{tag}>")
                else:
                    parts.append(f"<{tag}></{tag}>")
            parts.append("</tr>")

        if header_rows:
            if not (set(range(ts.num_rows)) - header_rows):
                parts.append("</thead>")
            else:
                parts.append("</tbody>")
        else:
            parts.append("</tbody>")

        parts.append("</table>")
        return "\n".join(parts)

    def _render_figure(self, block: LayoutBlock) -> str:
        parts = ["<figure>"]
        if block.image_path:
            rel_path = os.path.relpath(block.image_path, start=".")
            parts.append(f'  <img src="{rel_path}" alt="{self._escape(block.caption or block.block_type.value)}">')
        if block.caption:
            parts.append(f"  <figcaption>{self._escape(block.caption)}</figcaption>")
        parts.append("</figure>")
        return "\n".join(parts)

    def _render_list(self, block: LayoutBlock) -> str:
        lines = block.text.split("\n")
        # Detect ordered vs unordered
        import re
        is_ordered = any(re.match(r"^\s*\d+[\.\)]\s", line) for line in lines[:3])
        tag = "ol" if is_ordered else "ul"
        items = []
        for line in lines:
            line = re.sub(r"^\s*[\d]+[\.\)]\s*", "", line)
            line = re.sub(r"^\s*[-•·]\s*", "", line)
            if line.strip():
                items.append(f"  <li>{self._escape(line.strip())}</li>")
        return f"<{tag}>\n" + "\n".join(items) + f"\n</{tag}>"

    def _render_caption(self, block: LayoutBlock) -> str:
        return f'<p class="caption">{self._escape(block.text)}</p>'

    def _render_box(self, block: LayoutBlock) -> str:
        style = ' style="border: 1px solid #ccc; padding: 8px; margin: 8px 0;"'
        text = self._escape(block.text).replace("\n", "<br>")
        return f"<div class=\"box\"{style}>{text}</div>"

    def _render_balloon(self, block: LayoutBlock) -> str:
        text = self._escape(block.text).replace("\n", "<br>")
        return f'<aside class="callout">{text}</aside>'

    def _render_footnotes(self, footnotes: list[tuple[str, str]]) -> str:
        if not footnotes:
            return ""
        parts = ['<hr>', '<section class="footnotes">']
        for i, (fid, text) in enumerate(footnotes, 1):
            parts.append(
                f'  <p id="fn-{fid}"><sup>{i}</sup> {self._escape(text)}</p>'
            )
        parts.append("</section>")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_inline_style(self, style: TextStyle | None) -> str:
        if not self.inline_css or style is None:
            return ""
        parts = []
        if style.alignment != Alignment.LEFT:
            parts.append(f"text-align:{style.alignment.value}")
        if style.is_bold:
            parts.append("font-weight:bold")
        if style.is_italic:
            parts.append("font-style:italic")
        if style.is_underline:
            parts.append("text-decoration:underline")
        if not parts:
            return ""
        return f' style="{"; ".join(parts)}"'

    def _escape(self, text: str) -> str:
        return html.escape(text, quote=False)

    def _add_footnote_refs(self, text: str, block: LayoutBlock) -> str:
        """Insert footnote reference links if block has linked footnotes."""
        # Simple pattern: look for superscript numbers like ¹, ², ³ or [1], [2]
        import re
        text = re.sub(
            r"\[(\d+)\]",
            lambda m: f'<sup><a href="#fn-{block.id}-{m.group(1)}">[{m.group(1)}]</a></sup>',
            text,
        )
        return text

    def _wrap_document(self, body: str, footnotes: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Converted Document</title>
<style>
body {{
  font-family: 'Noto Sans KR', 'Malgun Gothic', sans-serif;
  max-width: 900px;
  margin: 0 auto;
  padding: 20px;
  line-height: 1.6;
  color: #333;
}}
.page {{
  margin-bottom: 40px;
  page-break-after: always;
}}
table {{
  border-collapse: collapse;
  width: 100%;
  margin: 12px 0;
}}
th, td {{
  border: 1px solid #666;
  padding: 6px 10px;
  text-align: left;
  vertical-align: top;
}}
th {{
  background-color: #f5f5f5;
  font-weight: bold;
}}
figure {{
  margin: 16px 0;
  text-align: center;
}}
figure img {{
  max-width: 100%;
  height: auto;
}}
figcaption {{
  font-size: 0.9em;
  color: #666;
  margin-top: 4px;
}}
.caption {{
  font-size: 0.9em;
  color: #666;
}}
.box {{
  border: 1px solid #ccc;
  padding: 8px;
  margin: 8px 0;
  background: #fafafa;
}}
.callout {{
  border-left: 3px solid #4a90d9;
  padding: 8px 12px;
  margin: 8px 0;
  background: #f0f6ff;
  font-size: 0.95em;
}}
.footnotes {{
  font-size: 0.85em;
  color: #555;
}}
h1 {{ font-size: 1.8em; text-align: center; margin: 24px 0 16px; }}
h2 {{ font-size: 1.4em; margin: 20px 0 12px; }}
h3 {{ font-size: 1.2em; margin: 16px 0 8px; }}
h4 {{ font-size: 1.1em; margin: 12px 0 6px; }}
</style>
</head>
<body>
{body}
{footnotes}
</body>
</html>"""
