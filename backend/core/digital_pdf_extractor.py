"""Digital PDF text and structure extractor using PyMuPDF (fitz).

For digital PDFs (those with embedded text), this module extracts text,
tables, and layout directly from the PDF structure — much more accurate
than OCR for digital text. The extracted content is then sent to Gemini
for visual comparison and refinement.

This is used as the 1st stage for digital PDFs in the hybrid workflow,
replacing Upstage Document Parse (which is better suited for image/scanned PDFs).

Root cause of the BiDi numeral bug (fixed in PyMuPDF >= 1.25.3):
    The bug originated in MuPDF's C library (not PyMuPDF Python wrapper).
    When CJK fonts (Korean, Chinese, Japanese) were mixed with Arabic
    numerals (0-9), MuPDF's glyph width calculation misinterpreted font
    metrics, causing digit glyphs to receive incorrect x-coordinates
    (displaced to end of line). PyMuPDF 1.25.3 (bundling MuPDF 1.25.4)
    fixed the underlying C-level glyph width computation.

    This module includes runtime verification to detect if the bug is
    still present despite the version requirement, plus defensive
    span-reordering as a fallback safety net.
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

# Minimum PyMuPDF version that includes the MuPDF BiDi glyph width fix
_PYMUPDF_BIDI_FIX_VERSION = (1, 25, 3)


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
        self._bidi_bug_verified = False
        self._bidi_bug_present = False

    # ------------------------------------------------------------------
    # PyMuPDF version & BiDi bug verification
    # ------------------------------------------------------------------

    def _check_pymupdf_version(self) -> tuple[bool, str]:
        """Check if PyMuPDF version includes the MuPDF BiDi glyph width fix.

        Returns (is_fixed, version_string).
        """
        try:
            import fitz
            version_str = fitz.VersionBind  # e.g. "1.25.3"
            parts = tuple(int(x) for x in version_str.split(".")[:3])
            is_fixed = parts >= _PYMUPDF_BIDI_FIX_VERSION
            if not is_fixed:
                logger.warning(
                    "PyMuPDF %s is below %s — BiDi numeral displacement "
                    "bug may be present. Upgrade: pip install --upgrade pymupdf",
                    version_str,
                    ".".join(str(x) for x in _PYMUPDF_BIDI_FIX_VERSION),
                )
            return is_fixed, version_str
        except Exception as exc:
            logger.error("Failed to check PyMuPDF version: %s", exc)
            return False, "unknown"

    def _detect_bidi_numeral_displacement(
        self,
        line_spans: list[dict],
    ) -> bool:
        """Detect if a line's spans exhibit the BiDi numeral displacement bug.

        The bug signature: a span containing only digits has an x-coordinate
        significantly greater than the preceding non-digit span's end position,
        AND there are CJK characters in surrounding spans (mixed CJK + numeral
        context where the bug manifests).

        Args:
            line_spans: Raw span dicts from PyMuPDF's get_text("dict")

        Returns:
            True if displacement is detected (bug likely present).
        """
        if len(line_spans) < 2:
            return False

        has_cjk = False
        digit_spans_at_end = []
        non_digit_max_x = 0.0

        for span in line_spans:
            text = span.get("text", "").strip()
            if not text:
                continue

            span_x0 = span.get("origin", (0, 0))[0] if "origin" in span else span.get("bbox", (0, 0, 0, 0))[0]
            span_x1 = span.get("bbox", (0, 0, 0, 0))[2]

            # Check for CJK characters
            if any(_is_cjk_or_korean(c) for c in text):
                has_cjk = True
                non_digit_max_x = max(non_digit_max_x, span_x1)

            # Track digit-only spans
            if re.match(r"^[\d,.\-\s]+$", text):
                digit_spans_at_end.append((span_x0, text))
            else:
                non_digit_max_x = max(non_digit_max_x, span_x1)

        if not has_cjk or not digit_spans_at_end:
            return False

        # Bug signature: digit span's x-coordinate is beyond all non-digit spans
        # AND the gap is suspiciously large (> 20% of line width)
        line_bbox = line_spans[0].get("bbox", (0, 0, 100, 0)) if line_spans else (0, 0, 100, 0)
        line_width = line_bbox[2] - line_bbox[0] if len(line_bbox) >= 3 else 100

        for digit_x, digit_text in digit_spans_at_end:
            if digit_x > non_digit_max_x and (digit_x - non_digit_max_x) > line_width * 0.2:
                logger.warning(
                    "BiDi numeral displacement detected: digits '%s' at x=%.1f "
                    "displaced beyond non-digit content ending at x=%.1f (line width=%.1f). "
                    "This indicates the MuPDF glyph width bug is still present.",
                    digit_text, digit_x, non_digit_max_x, line_width,
                )
                return True

        return False

    def verify_bidi_fix(self, pdf_path: Path, sample_pages: int = 3) -> dict:
        """Run a diagnostic check on a PDF to verify the BiDi fix is working.

        Scans sample pages for CJK+numeral lines and checks if digit spans
        have plausible x-coordinates relative to surrounding text.

        Args:
            pdf_path: Path to a PDF with CJK text + numbers
            sample_pages: How many pages to check

        Returns:
            Diagnostic report dict with:
              - pymupdf_version: str
              - version_includes_fix: bool
              - lines_checked: int
              - displacement_detected: int  (0 = good)
              - status: "PASS" | "FAIL" | "WARNING"
              - details: list of issue descriptions
        """
        import fitz

        is_fixed, version_str = self._check_pymupdf_version()

        doc = fitz.open(str(pdf_path))
        check_pages = min(sample_pages, len(doc))

        lines_checked = 0
        displacement_count = 0
        details: list[str] = []

        for page_idx in range(check_pages):
            page = doc[page_idx]
            text_dict = page.get_text("dict", sort=True)

            for block_data in text_dict.get("blocks", []):
                if block_data.get("type") != 0:
                    continue
                for line in block_data.get("lines", []):
                    spans = line.get("spans", [])
                    if len(spans) < 2:
                        continue

                    # Only check lines that have both CJK and digits
                    line_text = "".join(s.get("text", "") for s in spans)
                    has_cjk = any(_is_cjk_or_korean(c) for c in line_text)
                    has_digit = any(c.isdigit() for c in line_text)
                    if not (has_cjk and has_digit):
                        continue

                    lines_checked += 1
                    if self._detect_bidi_numeral_displacement(spans):
                        displacement_count += 1
                        # Collect detail about the issue
                        span_info = [
                            f"  span[{i}]: x={s.get('origin', (0,0))[0]:.1f} text='{s.get('text', '')[:30]}'"
                            for i, s in enumerate(spans)
                        ]
                        details.append(
                            f"Page {page_idx+1}, line: '{line_text[:60]}...'\n"
                            + "\n".join(span_info)
                        )

        doc.close()

        if displacement_count == 0 and lines_checked > 0:
            status = "PASS"
        elif displacement_count == 0 and lines_checked == 0:
            status = "WARNING"
            details.append("No CJK+numeral lines found in sample pages — cannot verify fix")
        else:
            status = "FAIL"

        report = {
            "pymupdf_version": version_str,
            "version_includes_fix": is_fixed,
            "lines_checked": lines_checked,
            "displacement_detected": displacement_count,
            "status": status,
            "details": details,
        }

        if status == "FAIL":
            logger.error(
                "BiDi numeral fix verification FAILED: %d/%d lines show displacement "
                "(PyMuPDF %s). The MuPDF glyph width bug is still present. "
                "Falling back to span x-coordinate sorting + Gemini visual correction.",
                displacement_count, lines_checked, version_str,
            )
            self._bidi_bug_present = True
        else:
            logger.info(
                "BiDi numeral fix verification %s: %d lines checked, "
                "%d displacement detected (PyMuPDF %s)",
                status, lines_checked, displacement_count, version_str,
            )

        self._bidi_bug_verified = True
        return report

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

        # Run BiDi bug verification on first extraction
        if not self._bidi_bug_verified:
            self._check_pymupdf_version()
            self.verify_bidi_fix(pdf_path, sample_pages=min(3, total_pages))

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
                # ── BiDi numeral displacement handling ──
                # Root cause (MuPDF C library): CJK font glyph width
                # miscalculation causes digit bbox x-coordinates to be
                # displaced to line end. Fixed in PyMuPDF >= 1.25.3.
                #
                # Defense-in-depth strategy:
                # 1. Runtime detection of displacement in each line
                # 2. x-coordinate sorting as fallback reordering
                # 3. Gemini visual comparison as final safety net
                spans = line.get("spans", [])

                # Check this specific line for displacement
                line_has_displacement = (
                    self._bidi_bug_present
                    or self._detect_bidi_numeral_displacement(spans)
                )

                # Sort spans by x-position (always applied as defensive measure,
                # but particularly critical when displacement is detected)
                spans_with_pos = []
                for span in spans:
                    span_text = span.get("text", "")
                    if span_text.strip():
                        x_pos = span.get("origin", (0, 0))[0] if "origin" in span else span.get("bbox", (0,))[0]
                        spans_with_pos.append((x_pos, span))

                if line_has_displacement:
                    # When displacement is detected, x-coordinates are
                    # unreliable. Fall back to PDF content stream order
                    # (the original span order from PyMuPDF before sorting)
                    # which preserves the logical text sequence even when
                    # bbox coordinates are wrong.
                    spans_with_pos = [
                        (i, span) for i, span in enumerate(spans)
                        if span.get("text", "").strip()
                    ]
                else:
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

        Uses a two-tier detection strategy for reliability and speed:

        1. **Font resource check** (fast): Inspect page /Font resources.
           If a page has no /Font entries at all, it is very likely image-only.
        2. **Text layer check** (definitive): Extract text from sampled pages.
           If extractable text length > threshold, the page has a text layer.

        Sampling strategy for large PDFs:
          - Pages from start, middle, and end (up to 5 samples total).
          - A page is considered "digital" if it has both font resources AND
            meaningful extracted text (> 50 chars).
          - The PDF is digital if >= 50 % of sampled pages are digital.

        Returns True if the PDF has meaningful digital text layers.
        """
        try:
            import fitz
            doc = fitz.open(str(pdf_path))
            total_pages = len(doc)

            if total_pages == 0:
                doc.close()
                return False

            # Build sample page indices: start + middle + end
            sample_indices = _build_sample_indices(total_pages, max_samples=5)
            digital_pages = 0

            for idx in sample_indices:
                page = doc[idx]

                # Tier 1: Font resource check (fast)
                has_fonts = _page_has_font_resources(page)

                # Tier 2: Text layer check (definitive)
                text = page.get_text("text").strip()
                has_text = len(text) > 50

                if has_fonts and has_text:
                    digital_pages += 1

            doc.close()

            # Digital if >= 50% of sampled pages have text+fonts
            return digital_pages >= len(sample_indices) / 2

        except Exception as exc:
            logger.warning("PDF type detection failed for %s: %s", pdf_path, exc)
            return False

    @staticmethod
    def detect_pdf_type(pdf_path: Path) -> dict:
        """Detailed PDF type detection with diagnostic info.

        Returns a dict with:
          - is_digital: bool
          - total_pages: int
          - sampled_pages: int
          - digital_pages: int
          - details: list of per-page info
        """
        try:
            import fitz
            doc = fitz.open(str(pdf_path))
            total_pages = len(doc)

            if total_pages == 0:
                doc.close()
                return {
                    "is_digital": False,
                    "total_pages": 0,
                    "sampled_pages": 0,
                    "digital_pages": 0,
                    "details": [],
                }

            sample_indices = _build_sample_indices(total_pages, max_samples=5)
            digital_pages = 0
            details = []

            for idx in sample_indices:
                page = doc[idx]
                has_fonts = _page_has_font_resources(page)
                text = page.get_text("text").strip()
                text_len = len(text)
                has_text = text_len > 50

                is_digital_page = has_fonts and has_text
                if is_digital_page:
                    digital_pages += 1

                details.append({
                    "page_index": idx,
                    "has_fonts": has_fonts,
                    "text_length": text_len,
                    "is_digital": is_digital_page,
                })

            doc.close()

            is_digital = digital_pages >= len(sample_indices) / 2

            return {
                "is_digital": is_digital,
                "total_pages": total_pages,
                "sampled_pages": len(sample_indices),
                "digital_pages": digital_pages,
                "details": details,
            }

        except Exception as exc:
            logger.warning("PDF type detection failed for %s: %s", pdf_path, exc)
            return {
                "is_digital": False,
                "total_pages": 0,
                "sampled_pages": 0,
                "digital_pages": 0,
                "details": [],
                "error": str(exc),
            }


# ------------------------------------------------------------------
# Module-level helpers for PDF type detection
# ------------------------------------------------------------------

def _build_sample_indices(total_pages: int, max_samples: int = 5) -> list[int]:
    """Build a list of page indices to sample: start, middle, end.

    For small PDFs (≤ max_samples pages), samples all pages.
    For larger PDFs, picks pages from start, middle, and end.
    """
    if total_pages <= max_samples:
        return list(range(total_pages))

    indices = set()
    # First 2 pages
    indices.add(0)
    if total_pages > 1:
        indices.add(1)
    # Middle page(s)
    mid = total_pages // 2
    indices.add(mid)
    if total_pages > 4:
        indices.add(mid - 1)
    # Last page
    indices.add(total_pages - 1)

    return sorted(indices)[:max_samples]


def _page_has_font_resources(page) -> bool:
    """Check if a PDF page has /Font entries in its resource dictionary.

    If a page has no font resources, it almost certainly has no text objects
    and is image-only (scanned).  This check is faster than extracting text.
    """
    try:
        # PyMuPDF exposes page resources via xref inspection
        # get_fonts() returns a list of (xref, ext, type, basename, name, enc)
        fonts = page.get_fonts(full=False)
        return len(fonts) > 0
    except Exception:
        # Fallback: if font inspection fails, assume fonts might exist
        return True
