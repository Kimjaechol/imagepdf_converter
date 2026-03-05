"""Render PDF pages to images and extract vector / text metadata."""

from __future__ import annotations

import uuid
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from backend.models.schema import (
    Alignment,
    BBox,
    BlockType,
    LayoutBlock,
    PdfChunk,
    TextStyle,
)


class PageRenderer:
    """Render PDF pages to high-DPI images and extract raw text/vector info."""

    def __init__(self, dpi: int = 300):
        self.dpi = dpi
        self._zoom = dpi / 72.0  # PDF default is 72 DPI

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def render_chunk(
        self,
        chunk: PdfChunk,
        images_dir: Path,
    ) -> list[dict]:
        """Render all pages in *chunk* to images.

        Returns a list of dicts, one per page:
            {
                "page_index": int (absolute page number in original doc),
                "image_path": str,
                "width": float,
                "height": float,
                "digital_blocks": list[LayoutBlock],  # text objects found in PDF layer
                "lines": list[dict],  # vector lines / rectangles
            }
        """
        images_dir.mkdir(parents=True, exist_ok=True)
        doc = fitz.open(str(chunk.pdf_path))
        results = []

        for local_idx in range(len(doc)):
            abs_page = chunk.start_page + local_idx
            page = doc[local_idx]

            # 1. Render raster image
            mat = fitz.Matrix(self._zoom, self._zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_path = images_dir / f"page_{abs_page:04d}.png"
            pix.save(str(img_path))

            width = pix.width
            height = pix.height

            # 2. Extract digital text spans (if present)
            digital_blocks = self._extract_text_blocks(page, abs_page)

            # 3. Extract vector objects (lines, rects) for table / box detection
            line_objects = self._extract_drawings(page)

            results.append({
                "page_index": abs_page,
                "image_path": str(img_path),
                "width": width,
                "height": height,
                "digital_blocks": digital_blocks,
                "lines": line_objects,
            })

        doc.close()
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_text_blocks(self, page: fitz.Page, abs_page: int) -> list[LayoutBlock]:
        """Use PyMuPDF to pull text spans with font/style info."""
        blocks: list[LayoutBlock] = []
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

        for blk in text_dict.get("blocks", []):
            if blk.get("type") != 0:  # type 0 = text block
                continue

            full_text_parts = []
            max_font_size = 0.0
            is_bold = False
            is_italic = False
            is_underline = False
            font_name = ""
            text_color = "#000000"
            total_chars = 0

            for line in blk.get("lines", []):
                line_text_parts = []
                for span in line.get("spans", []):
                    span_text = span.get("text", "")
                    line_text_parts.append(span_text)
                    sz = span.get("size", 12)
                    char_count = len(span_text.strip())
                    if sz > max_font_size:
                        max_font_size = sz
                        font_name = span.get("font", "")
                    # Extract color (PyMuPDF gives int RGB)
                    if char_count > total_chars:
                        color_int = span.get("color", 0)
                        if isinstance(color_int, int):
                            text_color = f"#{color_int:06x}"
                        total_chars = char_count
                    flags = span.get("flags", 0)
                    if flags & (1 << 4):  # bit 4 = bold
                        is_bold = True
                    if flags & (1 << 1):  # bit 1 = italic
                        is_italic = True
                    if flags & (1 << 3):  # bit 3 = underline (PyMuPDF undocumented but present)
                        is_underline = True
                    # Also detect bold/italic from font name
                    fn = span.get("font", "").lower()
                    if "bold" in fn:
                        is_bold = True
                    if "italic" in fn or "oblique" in fn:
                        is_italic = True
                full_text_parts.append("".join(line_text_parts))

            text = "\n".join(full_text_parts).strip()
            if not text:
                continue

            bbox_raw = blk.get("bbox", (0, 0, 0, 0))
            bbox = BBox(
                x0=bbox_raw[0] * self._zoom,
                y0=bbox_raw[1] * self._zoom,
                x1=bbox_raw[2] * self._zoom,
                y1=bbox_raw[3] * self._zoom,
            )

            # Guess alignment from horizontal position
            page_width = page.rect.width * self._zoom
            page_left_margin = page_width * 0.08  # typical left margin ~8%
            page_right_edge = page_width * 0.92
            cx = bbox.center_x
            block_width = bbox.x1 - bbox.x0
            # Justify: block spans nearly full width
            if block_width > page_width * 0.75:
                align = Alignment.JUSTIFY
            elif abs(cx - page_width / 2) < page_width * 0.08:
                align = Alignment.CENTER
            elif bbox.x0 > page_width * 0.55:
                align = Alignment.RIGHT
            else:
                align = Alignment.LEFT

            # Estimate indentation: distance from typical left margin
            indent_px = max(0.0, bbox.x0 - page_left_margin * self._zoom / (self._zoom if self._zoom != 0 else 1))
            # Convert to approximate em units (1em ≈ font_size pixels)
            indent_em = round(indent_px / max(max_font_size * self._zoom, 1.0), 1) if indent_px > max_font_size * self._zoom * 0.5 else 0.0

            # Estimate line spacing from line bboxes
            line_spacing = 1.0
            lines_data = blk.get("lines", [])
            if len(lines_data) >= 2:
                line_heights = []
                for i in range(1, len(lines_data)):
                    prev_origin_y = lines_data[i - 1].get("spans", [{}])[0].get("origin", (0, 0))[1] if lines_data[i - 1].get("spans") else 0
                    curr_origin_y = lines_data[i].get("spans", [{}])[0].get("origin", (0, 0))[1] if lines_data[i].get("spans") else 0
                    if prev_origin_y > 0 and curr_origin_y > 0:
                        line_heights.append(curr_origin_y - prev_origin_y)
                if line_heights and max_font_size > 0:
                    avg_gap = sum(line_heights) / len(line_heights)
                    line_spacing = round(avg_gap / max_font_size, 2)
                    line_spacing = max(1.0, min(line_spacing, 3.0))

            style = TextStyle(
                font_size=max_font_size,
                is_bold=is_bold,
                is_italic=is_italic,
                is_underline=is_underline,
                font_name=font_name,
                alignment=align,
                line_spacing=line_spacing,
                color=text_color,
            )

            blocks.append(LayoutBlock(
                id=f"dig_{abs_page}_{uuid.uuid4().hex[:8]}",
                block_type=BlockType.PARAGRAPH,
                bbox=bbox,
                text=text,
                style=style,
                page_index=abs_page,
                confidence=1.0,  # digital text is exact
            ))

        return blocks

    def _extract_drawings(self, page: fitz.Page) -> list[dict]:
        """Extract vector lines & rectangles – useful for table detection."""
        drawings = []
        for item in page.get_drawings():
            for path_item in item.get("items", []):
                kind = path_item[0]  # "l" for line, "re" for rect, etc.
                if kind == "l":
                    p1, p2 = path_item[1], path_item[2]
                    drawings.append({
                        "type": "line",
                        "x0": p1.x * self._zoom,
                        "y0": p1.y * self._zoom,
                        "x1": p2.x * self._zoom,
                        "y1": p2.y * self._zoom,
                    })
                elif kind == "re":
                    r = path_item[1]
                    drawings.append({
                        "type": "rect",
                        "x0": r.x0 * self._zoom,
                        "y0": r.y0 * self._zoom,
                        "x1": r.x1 * self._zoom,
                        "y1": r.y1 * self._zoom,
                    })
        return drawings
