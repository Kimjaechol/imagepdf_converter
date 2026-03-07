"""Digital PDF text and structure extractor using PyMuPDF (fitz).

For digital PDFs (those with embedded text), this module extracts text,
tables, and layout directly from the PDF structure — much more accurate
than OCR for digital text. The extracted content is then sent to Gemini
for visual comparison and refinement.

This is used as the 1st stage for digital PDFs in the hybrid workflow,
replacing Upstage Document Parse (which is better suited for image/scanned PDFs).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from backend.models.schema import (
    Alignment,
    BBox,
    BlockType,
    HeadingLevel,
    LayoutBlock,
    PageResult,
    TableCell,
    TableStructure,
    TextStyle,
)

logger = logging.getLogger(__name__)


def _is_cjk_or_korean(char: str) -> bool:
    """Check if a character is CJK (Chinese/Japanese/Korean)."""
    cp = ord(char)
    return (
        (0xAC00 <= cp <= 0xD7AF)    # Korean Hangul Syllables
        or (0x1100 <= cp <= 0x11FF)  # Korean Hangul Jamo
        or (0x3130 <= cp <= 0x318F)  # Korean Hangul Compatibility Jamo
        or (0x4E00 <= cp <= 0x9FFF)  # CJK Unified Ideographs
        or (0x3400 <= cp <= 0x4DBF)  # CJK Extension A
        or (0x3000 <= cp <= 0x303F)  # CJK Symbols
    )


class DigitalPdfExtractor:
    """Extract text, tables, and layout from digital PDFs using PyMuPDF.

    For digital PDFs, the embedded text layer is far more accurate than OCR.
    This extractor leverages PyMuPDF's ability to:
    - Extract text with font information (size, bold, italic, color)
    - Detect tables with cell-level text
    - Extract vector drawings and images
    - Preserve reading order from the PDF structure
    """

    def __init__(self, dpi: int = 300):
        self.dpi = dpi
        self._zoom = dpi / 72.0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def extract(
        self,
        pdf_path: Path,
        render_images: bool = True,
        images_dir: Path | None = None,
    ) -> tuple[list[PageResult], dict[int, str]]:
        """Extract all pages from a digital PDF.

        Args:
            pdf_path: Path to the PDF file
            render_images: Whether to render page images for Gemini comparison
            images_dir: Directory to save rendered images

        Returns:
            (page_results, page_images) where page_images maps page_index → image_path
        """
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
        total_pages = len(doc)

        page_results: list[PageResult] = []
        page_images: dict[int, str] = {}

        if images_dir:
            images_dir.mkdir(parents=True, exist_ok=True)

        for page_idx in range(total_pages):
            page = doc[page_idx]

            # Extract text blocks with style information
            blocks = self._extract_page_blocks(page, page_idx)

            # Extract tables
            table_blocks = self._extract_tables(page, page_idx, len(blocks))
            blocks.extend(table_blocks)

            # Sort by reading order (top-to-bottom, left-to-right)
            blocks.sort(key=lambda b: (
                b.bbox.y0 if b.bbox else 0,
                b.bbox.x0 if b.bbox else 0,
            ))
            for i, b in enumerate(blocks):
                b.reading_order = i

            # Page dimensions in rendered pixels
            width = page.rect.width * self._zoom
            height = page.rect.height * self._zoom

            page_results.append(PageResult(
                page_index=page_idx,
                width=width,
                height=height,
                blocks=blocks,
            ))

            # Render page image for Gemini visual comparison
            if render_images and images_dir:
                img_path = self._render_page_image(page, page_idx, images_dir)
                if img_path:
                    page_images[page_idx] = img_path

        doc.close()

        logger.info(
            "Digital PDF extraction: %d pages, %d total blocks",
            total_pages, sum(len(p.blocks) for p in page_results),
        )

        return page_results, page_images

    # ------------------------------------------------------------------
    # Text extraction
    # ------------------------------------------------------------------

    def _extract_page_blocks(
        self,
        page: Any,  # fitz.Page
        page_idx: int,
    ) -> list[LayoutBlock]:
        """Extract text blocks with font/style information from a single page."""
        blocks: list[LayoutBlock] = []

        # Use "dict" format for detailed text extraction with font info
        text_dict = page.get_text("dict", sort=True)
        page_width = page.rect.width * self._zoom
        page_height = page.rect.height * self._zoom

        for block_idx, block_data in enumerate(text_dict.get("blocks", [])):
            if block_data.get("type") == 1:
                # Image block
                bbox = block_data.get("bbox", (0, 0, 0, 0))
                scaled_bbox = BBox(
                    x0=bbox[0] * self._zoom,
                    y0=bbox[1] * self._zoom,
                    x1=bbox[2] * self._zoom,
                    y1=bbox[3] * self._zoom,
                )
                blocks.append(LayoutBlock(
                    id=f"dig_{page_idx}_{block_idx}",
                    block_type=BlockType.FIGURE,
                    bbox=scaled_bbox,
                    text="",
                    confidence=1.0,
                    page_index=page_idx,
                ))
                continue

            if block_data.get("type") != 0:
                continue

            # Text block - extract with style info
            lines_data = block_data.get("lines", [])
            if not lines_data:
                continue

            bbox = block_data.get("bbox", (0, 0, 0, 0))
            scaled_bbox = BBox(
                x0=bbox[0] * self._zoom,
                y0=bbox[1] * self._zoom,
                x1=bbox[2] * self._zoom,
                y1=bbox[3] * self._zoom,
            )

            # Aggregate text and determine dominant style
            text_parts: list[str] = []
            font_sizes: list[float] = []
            bold_count = 0
            italic_count = 0
            span_count = 0
            colors: list[int] = []

            for line in lines_data:
                # ── FIX: PyMuPDF BiDi numeral reordering bug ──
                # PyMuPDF sometimes pushes Arabic numerals (0-9) to the
                # end of a line due to incorrect BiDi handling. To fix this,
                # we sort spans by their x-coordinate (origin position) to
                # restore the correct visual reading order.
                spans = line.get("spans", [])
                # Sort spans by x-position to counteract BiDi reordering
                spans_with_pos = []
                for span in spans:
                    span_text = span.get("text", "")
                    if span_text.strip():
                        x_pos = span.get("origin", (0, 0))[0] if "origin" in span else span.get("bbox", (0,))[0]
                        spans_with_pos.append((x_pos, span))
                spans_with_pos.sort(key=lambda t: t[0])

                line_text_parts = []
                for _, span in spans_with_pos:
                    span_text = span.get("text", "")
                    if span_text.strip():
                        line_text_parts.append(span_text)
                        font_sizes.append(span.get("size", 12.0))
                        span_count += 1
                        flags = span.get("flags", 0)
                        # fitz font flags: bit 0=superscript, 1=italic, 4=bold
                        if flags & (1 << 4):  # bold
                            bold_count += 1
                        if flags & (1 << 1):  # italic
                            italic_count += 1
                        colors.append(span.get("color", 0))

                if line_text_parts:
                    # Join with space if spans don't naturally connect
                    reconstructed = self._join_spans_naturally(line_text_parts)
                    text_parts.append(reconstructed)

            text = "\n".join(text_parts)
            if not text.strip():
                continue

            # Determine dominant style
            avg_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 12.0
            is_bold = bold_count > span_count / 2 if span_count > 0 else False
            is_italic = italic_count > span_count / 2 if span_count > 0 else False

            # Detect alignment from position
            alignment = Alignment.LEFT
            block_center_x = (bbox[0] + bbox[2]) / 2
            page_center_x = page.rect.width / 2
            block_width = bbox[2] - bbox[0]

            if block_width < page.rect.width * 0.7:
                if abs(block_center_x - page_center_x) < page.rect.width * 0.05:
                    alignment = Alignment.CENTER
                elif bbox[0] > page.rect.width * 0.55:
                    alignment = Alignment.RIGHT

            style = TextStyle(
                font_size=round(avg_font_size, 1),
                is_bold=is_bold,
                is_italic=is_italic,
                alignment=alignment,
            )

            # Classify block type based on style and content
            block_type, heading_level, role = self._classify_block(
                text, style, scaled_bbox, page_width, page_height,
            )

            blocks.append(LayoutBlock(
                id=f"dig_{page_idx}_{block_idx}",
                block_type=block_type,
                bbox=scaled_bbox,
                text=text,
                style=style,
                confidence=1.0,  # Digital text is exact
                page_index=page_idx,
                heading_level=heading_level,
                role=role,
            ))

        return blocks

    def _classify_block(
        self,
        text: str,
        style: TextStyle,
        bbox: BBox,
        page_width: float,
        page_height: float,
    ) -> tuple[BlockType, HeadingLevel, str]:
        """Classify a text block based on style and position.

        Returns (block_type, heading_level, role).
        """
        # Page number detection (small text at top/bottom edge)
        if bbox.y0 < page_height * 0.05 or bbox.y1 > page_height * 0.95:
            if len(text.strip()) < 20:
                stripped = text.strip()
                if re.match(r"^[\d\-/\s]+$", stripped) or len(stripped) < 5:
                    return BlockType.PAGE_NUMBER, HeadingLevel.NONE, "page_number"

        # Header/footer detection
        if bbox.y0 < page_height * 0.08 and style.font_size <= 10:
            return BlockType.HEADER, HeadingLevel.NONE, "header"
        if bbox.y1 > page_height * 0.92 and style.font_size <= 10:
            return BlockType.FOOTER, HeadingLevel.NONE, "footer"

        # Heading detection by style
        if style.font_size >= 20 and style.is_bold:
            if style.alignment == Alignment.CENTER:
                return BlockType.HEADING, HeadingLevel.H1, "title"
            return BlockType.HEADING, HeadingLevel.H1, "title"
        if style.font_size >= 16 and style.is_bold:
            return BlockType.HEADING, HeadingLevel.H2, "section_heading"
        if style.font_size >= 14 and style.is_bold:
            return BlockType.HEADING, HeadingLevel.H3, "subheading"
        if style.is_bold and len(text) < 100 and "\n" not in text:
            return BlockType.HEADING, HeadingLevel.H4, "subheading"

        # Korean numbered headings
        if re.match(r"^제\s*\d+\s*[장편]", text):
            return BlockType.HEADING, HeadingLevel.H2, "numbered_heading"
        if re.match(r"^제\s*\d+\s*[절관]", text):
            return BlockType.HEADING, HeadingLevel.H3, "numbered_heading"
        if re.match(r"^제\s*\d+\s*조", text):
            return BlockType.HEADING, HeadingLevel.H4, "numbered_heading"

        # List detection
        lines = text.split("\n")
        list_patterns = [
            r"^\s*[-•·]\s",
            r"^\s*\d+[\.\)]\s",
            r"^\s*[가-힣]\.\s",
            r"^\s*[①②③④⑤⑥⑦⑧⑨⑩]",
        ]
        list_matches = sum(
            1 for line in lines
            if any(re.match(p, line) for p in list_patterns)
        )
        if list_matches >= 2 and list_matches >= len(lines) * 0.5:
            return BlockType.LIST, HeadingLevel.NONE, "list"

        # Footnote detection (small font at bottom of page)
        if bbox.y0 > page_height * 0.75 and style.font_size < 10:
            return BlockType.FOOTNOTE, HeadingLevel.NONE, "footnote"

        # Caption detection (small font, short text near figures)
        if style.font_size < 10 and len(text) < 200:
            caption_pattern = re.compile(
                r"^(표|그림|Table|Figure|Fig\.)\s*\d+", re.IGNORECASE,
            )
            if caption_pattern.match(text.strip()):
                return BlockType.CAPTION, HeadingLevel.NONE, "caption"

        return BlockType.PARAGRAPH, HeadingLevel.NONE, "paragraph"

    # ------------------------------------------------------------------
    # PyMuPDF BiDi numeral fix helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _join_spans_naturally(span_texts: list[str]) -> str:
        """Join span texts, preserving natural spacing.

        Handles the case where PyMuPDF splits text into spans at numeral
        boundaries due to BiDi processing. Joins without extra spaces when
        the spans naturally connect (e.g., "제" + "1" + "조").
        """
        if not span_texts:
            return ""
        if len(span_texts) == 1:
            return span_texts[0]

        result = span_texts[0]
        for i in range(1, len(span_texts)):
            prev = result
            curr = span_texts[i]
            # If previous ends with a space or current starts with one, just concat
            if prev.endswith(" ") or curr.startswith(" "):
                result += curr
            # If connecting Korean/CJK text with number, no space needed
            elif (prev and curr and (
                prev[-1].isdigit() or curr[0].isdigit()
            )):
                # Check if it looks like a natural join (Korean + number or number + Korean)
                if prev and curr and (
                    _is_cjk_or_korean(prev[-1]) or _is_cjk_or_korean(curr[0])
                ):
                    result += curr
                else:
                    result += " " + curr
            else:
                result += curr
        return result

    # ------------------------------------------------------------------
    # Table extraction
    # ------------------------------------------------------------------

    def _extract_tables(
        self,
        page: Any,  # fitz.Page
        page_idx: int,
        block_offset: int,
    ) -> list[LayoutBlock]:
        """Extract tables from the page using PyMuPDF's table finder."""
        blocks: list[LayoutBlock] = []

        try:
            tables = page.find_tables()
            if not tables or not tables.tables:
                return blocks

            for t_idx, table in enumerate(tables.tables):
                bbox = table.bbox
                scaled_bbox = BBox(
                    x0=bbox[0] * self._zoom,
                    y0=bbox[1] * self._zoom,
                    x1=bbox[2] * self._zoom,
                    y1=bbox[3] * self._zoom,
                )

                # Extract table data
                table_data = table.extract()
                if not table_data:
                    continue

                num_rows = len(table_data)
                num_cols = max(len(row) for row in table_data) if table_data else 0

                cells: list[TableCell] = []
                for r_idx, row in enumerate(table_data):
                    for c_idx, cell_text in enumerate(row):
                        cells.append(TableCell(
                            row=r_idx,
                            col=c_idx,
                            text=cell_text or "",
                            is_header=r_idx == 0,
                        ))

                table_structure = TableStructure(
                    num_rows=num_rows,
                    num_cols=num_cols,
                    cells=cells,
                    has_visible_borders=True,
                    bbox=scaled_bbox,
                )

                block_id = f"dig_{page_idx}_t{t_idx}"
                blocks.append(LayoutBlock(
                    id=block_id,
                    block_type=BlockType.TABLE,
                    bbox=scaled_bbox,
                    text="",  # Table text is in cells
                    confidence=1.0,
                    page_index=page_idx,
                    table_structure=table_structure,
                    role="table",
                ))

        except Exception as exc:
            logger.warning("Table extraction failed for page %d: %s", page_idx, exc)

        return blocks

    # ------------------------------------------------------------------
    # Page image rendering
    # ------------------------------------------------------------------

    def _render_page_image(
        self,
        page: Any,  # fitz.Page
        page_idx: int,
        images_dir: Path,
    ) -> str | None:
        """Render a page to image for Gemini visual comparison."""
        try:
            import fitz
            mat = fitz.Matrix(self._zoom, self._zoom)
            pix = page.get_pixmap(matrix=mat)
            img_path = images_dir / f"page_{page_idx:04d}.png"
            pix.save(str(img_path))
            return str(img_path)
        except Exception as exc:
            logger.warning("Failed to render page %d image: %s", page_idx, exc)
            return None

    # ------------------------------------------------------------------
    # PDF type detection
    # ------------------------------------------------------------------

    @staticmethod
    def is_digital_pdf(pdf_path: Path) -> bool:
        """Detect if a PDF has embedded text (digital) vs image-only (scanned).

        Checks first few pages for extractable text.
        Returns True if the PDF has meaningful digital text.
        """
        try:
            import fitz
            doc = fitz.open(str(pdf_path))
            check_pages = min(3, len(doc))
            text_pages = 0

            for i in range(check_pages):
                page = doc[i]
                text = page.get_text("text").strip()
                if len(text) > 50:
                    text_pages += 1

            doc.close()
            return text_pages >= check_pages / 2

        except Exception:
            return False
