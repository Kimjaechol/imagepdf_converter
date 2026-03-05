"""Unified Vision Processor – single Gemini API call per page batch.

Instead of separate calls for reading order, heading classification, and text
correction, this module sends page images to Gemini 3.1 Flash-Lite in ONE call.

Key optimization: a fast LOCAL pre-scan classifies each page as "text_only" or
"complex" (has tables, figures, graphs, multi-column).  Pages are then batched
by type and processed with type-specific prompts:

  - text_only pages:  larger batches (up to 30), lighter prompt, less output
  - complex pages:    smaller batches (up to 10), full analysis prompt

This reduces output tokens for simple pages and gives more attention to complex
pages, improving both speed and accuracy.
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

        1. Fast local pre-scan to classify pages
        2. Separate into text_only vs complex batches
        3. Send each batch with type-specific prompt
        """
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set; unified vision mode unavailable.")
            return []

        ocr_blocks = ocr_blocks_per_page or {}

        # Step 1: Fast local pre-scan
        classifications = self._prescan_pages(page_data_list)

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
            "Pre-scan: %d text-only, %d complex pages",
            len(text_only_pages), len(complex_pages),
        )

        results: list[PageResult] = []

        # Step 3a: Process text-only pages in large batches
        for i in range(0, len(text_only_pages), _BATCH_TEXT_ONLY):
            batch = text_only_pages[i : i + _BATCH_TEXT_ONLY]
            batch_results = self._process_batch(
                batch, ocr_blocks, api_key, page_type="text_only",
                classifications=classifications,
            )
            results.extend(batch_results)

        # Step 3b: Process complex pages in smaller batches
        for i in range(0, len(complex_pages), _BATCH_COMPLEX):
            batch = complex_pages[i : i + _BATCH_COMPLEX]
            batch_results = self._process_batch(
                batch, ocr_blocks, api_key, page_type="complex",
                classifications=classifications,
            )
            results.extend(batch_results)

        # Sort results by page_index to restore original order
        results.sort(key=lambda pr: pr.page_index)

        return results

    # ------------------------------------------------------------------
    # Fast local pre-scan (NO API call, pure local analysis)
    # ------------------------------------------------------------------

    def _prescan_pages(
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

    def _process_batch(
        self,
        page_data_list: list[dict],
        ocr_blocks: dict[int, list[LayoutBlock]],
        api_key: str,
        page_type: str = "complex",
        classifications: dict[int, PageClassification] | None = None,
    ) -> list[PageResult]:
        """Send a batch of pages to Gemini in a single API call."""
        if not page_data_list:
            return []

        try:
            import google.generativeai as genai
            from PIL import Image

            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(self.gemini_model)

            # Build the multimodal prompt parts
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

                # Build text context + classification hints for this page
                existing_text = ""
                if page_idx in ocr_blocks and ocr_blocks[page_idx]:
                    text_parts = []
                    for b in ocr_blocks[page_idx]:
                        if b.text:
                            bbox_str = ""
                            if b.bbox:
                                bbox_str = f" [{b.bbox.x0:.0f},{b.bbox.y0:.0f},{b.bbox.x1:.0f},{b.bbox.y1:.0f}]"
                            text_parts.append(f"  - \"{b.text[:200]}\"{bbox_str}")
                    existing_text = "\n".join(text_parts)

                # Add 0/1 complexity tag + detail hints
                # 0 = text only, 1 = complex (tables/figures/graphs/equations)
                cls = (classifications or {}).get(page_idx)
                tag = 1 if (cls and cls.is_complex) else 0
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
                hints = f"  [TAG={tag}]"
                if detail_hints:
                    hints += " " + ",".join(detail_hints)

                page_infos.append({
                    "page_index": page_idx,
                    "width": width,
                    "height": height,
                    "existing_text": existing_text,
                    "hints": hints,
                })

            # Build type-specific prompt
            prompt = self._build_prompt(page_infos, page_type)
            parts.append(prompt)

            # Call Gemini
            response = model.generate_content(parts)

            return self._parse_unified_response(response.text, page_data_list)

        except Exception as exc:
            logger.error("Unified vision processing failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Prompts (type-specific)
    # ------------------------------------------------------------------

    def _build_prompt(self, page_infos: list[dict], page_type: str) -> str:
        if page_type == "text_only":
            return self._build_text_only_prompt(page_infos)
        return self._build_complex_prompt(page_infos)

    def _build_text_only_prompt(self, page_infos: list[dict]) -> str:
        """Lighter prompt for text-only pages – no table schema needed."""
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
All pages in this batch are TAG=0 (text only).

Tasks:
1. Extract all text blocks with bbox coordinates.
2. Classify headings (h1-h6). Korean "제1장"/"제1절" = h2/h3. No level skipping.
3. Determine reading order (top-to-bottom, left-to-right).
4. Fix any OCR errors (Korean Hanja, spelling, spacing).

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
