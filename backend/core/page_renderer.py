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
            font_name = ""

            for line in blk.get("lines", []):
                line_text_parts = []
                for span in line.get("spans", []):
                    line_text_parts.append(span.get("text", ""))
                    sz = span.get("size", 12)
                    if sz > max_font_size:
                        max_font_size = sz
                        font_name = span.get("font", "")
                    flags = span.get("flags", 0)
                    if flags & 2**4:  # bold flag
                        is_bold = True
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
            cx = bbox.center_x
            if abs(cx - page_width / 2) < page_width * 0.1:
                align = Alignment.CENTER
            elif bbox.x0 > page_width * 0.55:
                align = Alignment.RIGHT
            else:
                align = Alignment.LEFT

            style = TextStyle(
                font_size=max_font_size,
                is_bold=is_bold,
                font_name=font_name,
                alignment=align,
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
