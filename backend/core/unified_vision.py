"""Unified Vision Processor – single Gemini API call per page batch.

Instead of separate calls for reading order, heading classification, and text
correction, this module sends page images to Gemini 3.1 Flash-Lite in ONE call.

Key optimizations:

1. Fast LOCAL pre-scan classifies each page as TAG=0 (text_only) or TAG=1
   (complex: tables, figures, graphs, multi-column).

2. TAG=0 pages: Local high-quality OCR (Surya) runs FIRST, then only the
   extracted TEXT is sent to Gemini for correction/heading classification.
   No images are sent → ~7.5x fewer input tokens per page.

3. TAG=1 pages: Page images are sent directly to Gemini for full vision
   analysis (OCR + table extraction + layout + headings + correction).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
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

# Batch sizes per page type
_BATCH_TEXT_ONLY = 30   # text-only pages can be batched aggressively
_BATCH_COMPLEX = 10     # complex pages need more output per page


@dataclass
class PageClassification:
    """Result of the fast local pre-scan for a single page."""
    page_index: int
    has_tables: bool = False
    has_figures: bool = False
    has_multi_column: bool = False
    has_structural_lines: bool = False
    num_text_blocks: int = 0
    num_vector_rects: int = 0
    num_vector_lines: int = 0

    @property
    def is_complex(self) -> bool:
        return (
            self.has_tables
            or self.has_figures
            or self.has_multi_column
            or (self.num_vector_rects > 3)
        )


class UnifiedVisionProcessor:
    """Process document pages with a single Gemini Vision API call."""

    def __init__(
        self,
        gemini_model: str = "gemini-3.1-flash-lite-preview",
    ):
        self.gemini_model = gemini_model

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def process_pages(
        self,
        page_data_list: list[dict],
        ocr_blocks_per_page: dict[int, list[LayoutBlock]] | None = None,
    ) -> list[PageResult]:
        """Process multiple pages via unified Gemini Vision call(s).

        1. Fast local pre-scan → classify each page as TAG=0 or TAG=1
        2. TAG=0 pages: text-only correction (no images sent to Gemini)
        3. TAG=1 pages: full vision analysis (images sent to Gemini)
        """
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set; unified vision mode unavailable.")
            return []

        ocr_blocks = ocr_blocks_per_page or {}

        # Step 1: Fast local pre-scan
        classifications = self.prescan_pages(page_data_list)

        # Step 2: Split by type
        text_only_pages = []
        complex_pages = []
        for pd in page_data_list:
            cls = classifications.get(pd["page_index"])
            if cls and cls.is_complex:
                complex_pages.append(pd)
            else:
                text_only_pages.append(pd)

        logger.info(
            "Pre-scan: %d text-only (TAG=0), %d complex (TAG=1) pages",
            len(text_only_pages), len(complex_pages),
        )

        results: list[PageResult] = []

        # Step 3a: TAG=0 pages – TEXT ONLY to Gemini (no images)
        # Local OCR already ran; send just text for correction + heading classification
        for i in range(0, len(text_only_pages), _BATCH_TEXT_ONLY):
            batch = text_only_pages[i : i + _BATCH_TEXT_ONLY]
            batch_results = self._process_text_only_batch(
                batch, ocr_blocks, api_key, classifications,
            )
            results.extend(batch_results)

        # Step 3b: TAG=1 pages – full VISION analysis (images sent)
        for i in range(0, len(complex_pages), _BATCH_COMPLEX):
            batch = complex_pages[i : i + _BATCH_COMPLEX]
            batch_results = self._process_complex_batch(
                batch, ocr_blocks, api_key, classifications,
            )
            results.extend(batch_results)

        # Sort results by page_index to restore original order
        results.sort(key=lambda pr: pr.page_index)

        return results

    # ------------------------------------------------------------------
    # Fast local pre-scan (NO API call, pure local analysis)
    # ------------------------------------------------------------------

    def prescan_pages(
        self, page_data_list: list[dict],
    ) -> dict[int, PageClassification]:
        """Classify each page locally by analyzing vector objects and text blocks.

        This is extremely fast (~0.1ms per page) since it only checks metadata
        already extracted by PageRenderer.
        """
        results: dict[int, PageClassification] = {}

        for pd in page_data_list:
            page_idx = pd["page_index"]
            lines = pd.get("lines", [])
            digital_blocks = pd.get("digital_blocks", [])
            width = pd["width"]

            cls = PageClassification(page_index=page_idx)
            cls.num_text_blocks = len(digital_blocks)

            # Count vector objects
            structural_lines = 0
            rects = 0
            for ln in lines:
                if ln["type"] == "rect":
                    rects += 1
                    rw = abs(ln["x1"] - ln["x0"])
                    rh = abs(ln["y1"] - ln["y0"])
                    # Large rect likely a figure placeholder
                    if rw > width * 0.3 and rh > width * 0.2:
                        cls.has_figures = True
                elif ln["type"] == "line" and ln.get("line_class") == "structural":
                    structural_lines += 1

            cls.num_vector_rects = rects
            cls.num_vector_lines = structural_lines
            cls.has_structural_lines = structural_lines > 0

            # Tables: 4+ structural lines + 2+ rects = likely a table
            if structural_lines >= 4 and rects >= 2:
                cls.has_tables = True
            elif rects >= 4:
                cls.has_tables = True

            # Multi-column: check if text blocks cluster into left/right halves
            if len(digital_blocks) >= 4:
                mid = width / 2
                left_count = sum(
                    1 for b in digital_blocks
                    if b.bbox and b.bbox.center_x < mid * 0.85
                    and b.bbox.width < width * 0.6
                )
                right_count = sum(
                    1 for b in digital_blocks
                    if b.bbox and b.bbox.center_x > mid * 1.15
                    and b.bbox.width < width * 0.6
                )
                if left_count >= 2 and right_count >= 2:
                    cls.has_multi_column = True

            # Image-only PDF page (no digital text, probably scanned)
            # → treat as complex since Gemini must do OCR
            if cls.num_text_blocks == 0:
                cls.has_figures = True

            results[page_idx] = cls

        return results

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def _process_text_only_batch(
        self,
        page_data_list: list[dict],
        ocr_blocks: dict[int, list[LayoutBlock]],
        api_key: str,
        classifications: dict[int, PageClassification] | None = None,
    ) -> list[PageResult]:
        """TAG=0: Send only OCR TEXT to Gemini (no images).

        Local OCR (Surya) already extracted high-quality text.
        Gemini only needs to correct errors and classify headings.
        This saves ~1,500 input tokens per page (no image tokens).
        """
        if not page_data_list:
            return []

        try:
            import google.generativeai as genai

            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(self.gemini_model)

            # Build TEXT-ONLY prompt (no images!)
            page_infos = []
            for pd in page_data_list:
                page_idx = pd["page_index"]
                width = pd["width"]
                height = pd["height"]

                # Gather OCR text with style metadata
                ocr_text = self._format_ocr_text(page_idx, ocr_blocks, width)

                page_infos.append({
                    "page_index": page_idx,
                    "width": width,
                    "height": height,
                    "existing_text": ocr_text,
                    "hints": "  [TAG=0]",
                })

            prompt = self._build_text_only_prompt(page_infos)

            # Single text-only call – no images in parts
            response = model.generate_content(prompt)

            return self._parse_unified_response(response.text, page_data_list)

        except Exception as exc:
            logger.error("Text-only batch processing failed: %s", exc)
            return []

    def _process_complex_batch(
        self,
        page_data_list: list[dict],
        ocr_blocks: dict[int, list[LayoutBlock]],
        api_key: str,
        classifications: dict[int, PageClassification] | None = None,
    ) -> list[PageResult]:
        """TAG=1: Send page IMAGES to Gemini for full vision analysis.

        Complex pages need Gemini to see the actual image for table
        structure, figure detection, multi-column layout, etc.
        """
        if not page_data_list:
            return []

        try:
            import google.generativeai as genai
            from PIL import Image

            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(self.gemini_model)

            # Build multimodal prompt: images + text hints
            parts: list[Any] = []

            page_infos = []
            for pd in page_data_list:
                page_idx = pd["page_index"]
                img_path = pd["image_path"]
                width = pd["width"]
                height = pd["height"]

                # Add page image
                img = Image.open(img_path)
                parts.append(img)

                # Gather any existing OCR/digital text as hints
                ocr_text = self._format_ocr_text(page_idx, ocr_blocks, width)

                # Build detail hints from classification
                cls = (classifications or {}).get(page_idx)
                detail_hints = []
                if cls:
                    if cls.has_tables:
                        detail_hints.append("TABLE")
                    if cls.has_figures:
                        detail_hints.append("FIGURE")
                    if cls.has_multi_column:
                        detail_hints.append("MULTI_COL")
                    if cls.num_text_blocks == 0:
                        detail_hints.append("IMAGE_ONLY")
                hints = "  [TAG=1]"
                if detail_hints:
                    hints += " " + ",".join(detail_hints)

                page_infos.append({
                    "page_index": page_idx,
                    "width": width,
                    "height": height,
                    "existing_text": ocr_text,
                    "hints": hints,
                })

            prompt = self._build_complex_prompt(page_infos)
            parts.append(prompt)

            # Multimodal call – images + text prompt
            response = model.generate_content(parts)

            return self._parse_unified_response(response.text, page_data_list)

        except Exception as exc:
            logger.error("Complex batch vision processing failed: %s", exc)
            return []

    def _format_ocr_text(
        self,
        page_idx: int,
        ocr_blocks: dict[int, list[LayoutBlock]],
        page_width: float = 0,
    ) -> str:
        """Format OCR blocks with style metadata for heading classification.

        Includes font size, bold, alignment – enough for Gemini to classify
        headings and determine reading order WITHOUT seeing the image.
        """
        if page_idx not in ocr_blocks or not ocr_blocks[page_idx]:
            return ""
        text_parts = []
        for b in ocr_blocks[page_idx]:
            if not b.text:
                continue
            # bbox coordinates
            bbox_str = ""
            if b.bbox:
                bbox_str = f" bbox=[{b.bbox.x0:.0f},{b.bbox.y0:.0f},{b.bbox.x1:.0f},{b.bbox.y1:.0f}]"

            # Style metadata (crucial for heading classification)
            style_parts = []
            if b.style:
                # Font size (exact from digital PDF, or estimated from bbox height)
                if b.style.font_size and b.style.font_size != 12.0:
                    style_parts.append(f"sz={b.style.font_size:.1f}pt")
                if b.style.is_bold:
                    style_parts.append("BOLD")
                if b.style.is_italic:
                    style_parts.append("italic")
                if b.style.alignment.value != "left":
                    style_parts.append(f"align={b.style.alignment.value}")
            elif b.bbox and page_width > 0:
                # Estimate style from bbox for Surya OCR blocks
                # Font size ≈ bbox height (rough but useful)
                est_size = b.bbox.height * 0.75  # bbox height to pt approximation
                if est_size > 14:
                    style_parts.append(f"sz~{est_size:.0f}pt")
                # Alignment from position
                cx = b.bbox.center_x
                if abs(cx - page_width / 2) < page_width * 0.08:
                    style_parts.append("align=center")
                elif b.bbox.x0 > page_width * 0.55:
                    style_parts.append("align=right")

            style_str = f" ({', '.join(style_parts)})" if style_parts else ""
            text_parts.append(f"  - \"{b.text[:200]}\"{bbox_str}{style_str}")
        return "\n".join(text_parts)

    # ------------------------------------------------------------------
    # Prompts (type-specific)
    # ------------------------------------------------------------------

    def _build_text_only_prompt(self, page_infos: list[dict]) -> str:
        """Lighter prompt for text-only pages – no table schema needed."""
        pages_desc = []
        for pi in page_infos:
            desc = f"Page {pi['page_index']} {pi['hints']} ({pi['width']:.0f}x{pi['height']:.0f}px)"
            if pi["existing_text"]:
                desc += f":\n  OCR text (verify/correct):\n{pi['existing_text']}"
            pages_desc.append(desc)

        pages_section = "\n\n".join(pages_desc)

        return f"""You are given OCR-extracted text from text-only document pages (TAG=0).
No images are provided. The text was extracted by a high-quality local OCR engine.

Each text block includes metadata: bbox coordinates, font size (sz), BOLD, alignment.
Use this metadata to classify headings and determine styles:

Heading classification rules:
- Large font (sz>16pt) + BOLD + align=center → likely h1 or h2 (main title)
- Large font (sz>14pt) + BOLD → likely h2 or h3
- Korean patterns: "제1장"/"제1편" = h2, "제1절"/"제1관" = h3, "제1조" = h4
- No heading level skipping (h1→h3 is invalid, must be h1→h2→h3)

Your tasks:
1. **Correct OCR errors**: Fix Korean Hanja (甲→갑), spelling, spacing errors.
2. **Classify headings**: Using font size, bold, alignment metadata + text patterns.
3. **Determine reading order**: Based on bbox positions (top-to-bottom, left-to-right).
4. **Preserve bbox coordinates and styles**: Keep the original bbox. Reflect font_size_relative from actual sz metadata.

{pages_section}

Return JSON:
{{
  "pages": [
    {{
      "page_index": 0,
      "blocks": [
        {{
          "id": "p0_b0",
          "type": "heading|paragraph|footnote|header|footer|page_number|list",
          "text": "corrected text",
          "bbox": [x0, y0, x1, y1],
          "reading_order": 0,
          "heading_level": "h1|h2|h3|h4|h5|h6|none",
          "style": {{"bold": false, "italic": false, "font_size_relative": "large|normal|small", "alignment": "left|center|right"}}
        }}
      ]
    }}
  ]
}}

Return ONLY valid JSON. No fences. Include ALL visible text."""

    def _build_complex_prompt(self, page_infos: list[dict]) -> str:
        """Full prompt for complex pages with tables/figures/multi-column."""
        pages_desc = []
        for pi in page_infos:
            desc = f"Page {pi['page_index']} {pi['hints']} ({pi['width']:.0f}x{pi['height']:.0f}px)"
            if pi["existing_text"]:
                desc += f":\n  OCR text (verify/correct):\n{pi['existing_text']}"
            pages_desc.append(desc)

        pages_section = "\n\n".join(pages_desc)

        return f"""Analyze each page image below.

Each page has a [TAG=0] or [TAG=1] label:
  TAG=0 → Text-only page (no tables, figures, or complex layout)
  TAG=1 → Complex page (may contain tables, figures, graphs, equations, multi-column)
All pages in this batch are TAG=1 (complex). Pay extra attention to layout structure.

Tasks:
1. **Layout Detection**: Identify every block (paragraphs, headings, tables, figures, captions, footnotes, page numbers, headers, footers).
2. **Table Recognition**: For EVERY table, extract full cell structure (row, col, rowspan, colspan, text, is_header). This is critical.
3. **Reading Order**: Structural lines (borders, separators) define reading zones. Read each zone completely before the next. Multi-column: left first, then right. Footnotes come last.
4. **Heading Classification**: h1-h6. Korean "제1장"/"제1절" = h2/h3. No level skipping.
5. **Text Correction**: Fix OCR errors (Hanja 甲乙丙丁, Korean spelling/spacing).

{pages_section}

Return JSON:
{{
  "pages": [
    {{
      "page_index": 0,
      "blocks": [
        {{
          "id": "p0_b0",
          "type": "heading|paragraph|table|figure|caption|footnote|header|footer|page_number|list",
          "text": "corrected text",
          "bbox": [x0, y0, x1, y1],
          "reading_order": 0,
          "heading_level": "h1|h2|h3|h4|h5|h6|none",
          "style": {{"bold": false, "italic": false, "font_size_relative": "large|normal|small", "alignment": "left|center|right"}},
          "table": null
        }},
        {{
          "id": "p0_b1",
          "type": "table",
          "text": "",
          "bbox": [x0, y0, x1, y1],
          "reading_order": 1,
          "heading_level": "none",
          "style": null,
          "table": {{
            "rows": 3, "cols": 4,
            "cells": [
              {{"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "header", "is_header": true}}
            ]
          }}
        }}
      ]
    }}
  ]
}}

CRITICAL:
- bbox in pixels matching page dimensions above.
- Include ALL text. Do not omit any content.
- For tables, include EVERY cell with exact row/col position.
- Return ONLY valid JSON. No markdown fences."""

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_unified_response(
        self,
        response_text: str,
        page_data_list: list[dict],
    ) -> list[PageResult]:
        """Parse the unified JSON response into PageResult objects."""
        try:
            text = response_text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            data = json.loads(text)
            pages_data = data.get("pages", [])

            page_dims = {
                pd["page_index"]: (pd["width"], pd["height"])
                for pd in page_data_list
            }

            results: list[PageResult] = []
            for page_json in pages_data:
                page_idx = page_json.get("page_index", 0)
                width, height = page_dims.get(page_idx, (0, 0))

                blocks = self._parse_blocks(page_json.get("blocks", []), page_idx)

                results.append(PageResult(
                    page_index=page_idx,
                    width=width,
                    height=height,
                    blocks=blocks,
                ))

            return results

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.error("Failed to parse unified vision response: %s", exc)
            return []

    def _parse_blocks(
        self,
        blocks_json: list[dict],
        page_idx: int,
    ) -> list[LayoutBlock]:
        """Convert JSON block data to LayoutBlock objects."""
        blocks: list[LayoutBlock] = []

        type_map = {
            "heading": BlockType.HEADING,
            "paragraph": BlockType.PARAGRAPH,
            "table": BlockType.TABLE,
            "figure": BlockType.FIGURE,
            "caption": BlockType.CAPTION,
            "footnote": BlockType.FOOTNOTE,
            "header": BlockType.HEADER,
            "footer": BlockType.FOOTER,
            "page_number": BlockType.PAGE_NUMBER,
            "list": BlockType.LIST,
            "equation": BlockType.EQUATION,
        }

        level_map = {
            "h1": HeadingLevel.H1, "h2": HeadingLevel.H2,
            "h3": HeadingLevel.H3, "h4": HeadingLevel.H4,
            "h5": HeadingLevel.H5, "h6": HeadingLevel.H6,
            "none": HeadingLevel.NONE,
        }

        align_map = {
            "left": Alignment.LEFT,
            "center": Alignment.CENTER,
            "right": Alignment.RIGHT,
        }

        for bj in blocks_json:
            bbox_raw = bj.get("bbox")
            bbox = None
            if bbox_raw and len(bbox_raw) == 4:
                bbox = BBox(
                    x0=float(bbox_raw[0]), y0=float(bbox_raw[1]),
                    x1=float(bbox_raw[2]), y1=float(bbox_raw[3]),
                )

            block_type = type_map.get(bj.get("type", ""), BlockType.PARAGRAPH)
            heading_level = level_map.get(
                bj.get("heading_level", "none"), HeadingLevel.NONE
            )

            style = TextStyle()
            style_json = bj.get("style")
            if style_json:
                style.is_bold = style_json.get("bold", False)
                style.is_italic = style_json.get("italic", False)
                size_rel = style_json.get("font_size_relative", "normal")
                if size_rel == "large":
                    style.font_size = 18.0
                elif size_rel == "small":
                    style.font_size = 9.0
                else:
                    style.font_size = 12.0
                style.alignment = align_map.get(
                    style_json.get("alignment", "left"), Alignment.LEFT
                )

            table_structure = None
            table_json = bj.get("table")
            if table_json and block_type == BlockType.TABLE:
                cells = []
                for cj in table_json.get("cells", []):
                    cells.append(TableCell(
                        row=cj.get("row", 0), col=cj.get("col", 0),
                        rowspan=cj.get("rowspan", 1), colspan=cj.get("colspan", 1),
                        text=cj.get("text", ""),
                        is_header=cj.get("is_header", False),
                    ))
                table_structure = TableStructure(
                    num_rows=table_json.get("rows", 0),
                    num_cols=table_json.get("cols", 0),
                    cells=cells,
                    has_visible_borders=True,
                    bbox=bbox,
                )

            role = "paragraph"
            if block_type == BlockType.HEADING:
                role = "title" if heading_level == HeadingLevel.H1 else "section_heading"
            elif block_type == BlockType.CAPTION:
                role = "caption"
            elif block_type == BlockType.FOOTNOTE:
                role = "footnote"

            block = LayoutBlock(
                id=bj.get("id", f"p{page_idx}_b{len(blocks)}"),
                block_type=block_type,
                bbox=bbox,
                text=bj.get("text", ""),
                style=style,
                confidence=0.95,
                page_index=page_idx,
                table_structure=table_structure,
                heading_level=heading_level,
                role=role,
                reading_order=bj.get("reading_order", len(blocks)),
            )
            blocks.append(block)

        blocks.sort(key=lambda b: b.reading_order)
        return blocks
