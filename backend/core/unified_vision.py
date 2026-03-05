"""Unified Vision Processor – single Gemini API call per page.

Instead of separate calls for reading order, heading classification, and text
correction, this module sends the page image along with any locally-extracted
text to Gemini 3.1 Flash-Lite in ONE call.  The model performs:

  1. Layout verification & block detection
  2. Table structure recognition (cells, headers, merged cells)
  3. Reading order determination
  4. Heading level classification
  5. Text correction (OCR errors, Hanja/Korean, spelling)

This reduces API round-trips from 3 to 1 per page, cutting latency by ~60%.
"""

from __future__ import annotations

import json
import logging
import os
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

# Maximum pages to batch in a single API call.
# Gemini 3.1 Flash-Lite supports ~200K context; each page image ≈ 1,500 tokens.
# 20 images ≈ 30K tokens, well within limits.
MAX_PAGES_PER_CALL = 20


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

        *page_data_list*: list of dicts with keys:
            page_index, image_path, width, height, digital_blocks, lines

        *ocr_blocks_per_page*: optional pre-extracted OCR text per page
            (from Surya or digital extraction).  If provided, Gemini will
            verify / correct this text instead of doing full OCR.

        Returns list of PageResult with fully populated blocks.
        """
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set; unified vision mode unavailable.")
            return []

        results: list[PageResult] = []

        # Batch pages into groups for efficient API usage
        for i in range(0, len(page_data_list), MAX_PAGES_PER_CALL):
            batch = page_data_list[i : i + MAX_PAGES_PER_CALL]
            batch_results = self._process_batch(
                batch, ocr_blocks_per_page or {}, api_key
            )
            results.extend(batch_results)

        return results

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def _process_batch(
        self,
        page_data_list: list[dict],
        ocr_blocks: dict[int, list[LayoutBlock]],
        api_key: str,
    ) -> list[PageResult]:
        """Send a batch of pages to Gemini in a single API call."""
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

                # Build text context for this page
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

                page_infos.append({
                    "page_index": page_idx,
                    "width": width,
                    "height": height,
                    "existing_text": existing_text,
                })

            # Add the unified prompt
            prompt = self._build_unified_prompt(page_infos)
            parts.append(prompt)

            # Call Gemini
            response = model.generate_content(parts)

            return self._parse_unified_response(response.text, page_data_list)

        except Exception as exc:
            logger.error("Unified vision processing failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------

    def _build_unified_prompt(self, page_infos: list[dict]) -> str:
        pages_desc = []
        for pi in page_infos:
            desc = f"Page {pi['page_index']} ({pi['width']:.0f}x{pi['height']:.0f}px)"
            if pi["existing_text"]:
                desc += f":\n  OCR text (may contain errors):\n{pi['existing_text']}"
            pages_desc.append(desc)

        pages_section = "\n\n".join(pages_desc)

        return f"""You are an expert document analyzer. For each page image provided, perform ALL of the following tasks in a single analysis:

1. **Layout Detection**: Identify every content block (paragraphs, headings, tables, figures, captions, footnotes, page numbers, headers, footers).
2. **Table Recognition**: For any table, extract the full cell structure (row, col, rowspan, colspan, text, is_header).
3. **Reading Order**: Determine the natural reading order. Structural lines (borders, separators) define reading zones. Read each zone top-to-bottom, left-to-right. Multi-column layouts: left column first, then right. Footnotes come after body.
4. **Heading Classification**: Classify heading levels (h1-h6). Korean patterns like "제1장", "제1절" are typically h2 or h3. Levels must not skip.
5. **Text Correction**: Fix OCR errors, especially:
   - Korean Hanja (甲→甲, 乙→乙, etc. in legal/exam contexts)
   - Confused characters (갑/甲, 을/乙)
   - Spelling and spacing errors in Korean text
   - If existing OCR text is provided, verify and correct it against the image.

{pages_section}

Return a JSON object with this exact structure:
{{
  "pages": [
    {{
      "page_index": 0,
      "blocks": [
        {{
          "id": "p0_b0",
          "type": "heading|paragraph|table|figure|caption|footnote|header|footer|page_number|list",
          "text": "corrected text content",
          "bbox": [x0, y0, x1, y1],
          "reading_order": 0,
          "heading_level": "h1|h2|h3|h4|h5|h6|none",
          "style": {{
            "bold": false,
            "italic": false,
            "font_size_relative": "large|normal|small",
            "alignment": "left|center|right"
          }},
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
            "rows": 3,
            "cols": 4,
            "cells": [
              {{"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "header text", "is_header": true}}
            ]
          }}
        }}
      ]
    }}
  ]
}}

CRITICAL RULES:
- bbox coordinates are in pixels matching the page dimensions given above.
- reading_order is a zero-based integer indicating the natural reading sequence.
- Return ONLY valid JSON. No markdown fences, no explanation.
- Include ALL text visible on the page. Do not omit any content.
- For tables, include EVERY cell with its exact row/col position."""

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
            # Strip markdown code fences if present
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            data = json.loads(text)
            pages_data = data.get("pages", [])

            # Build a lookup for page dimensions
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
            "h1": HeadingLevel.H1,
            "h2": HeadingLevel.H2,
            "h3": HeadingLevel.H3,
            "h4": HeadingLevel.H4,
            "h5": HeadingLevel.H5,
            "h6": HeadingLevel.H6,
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
                    x0=float(bbox_raw[0]),
                    y0=float(bbox_raw[1]),
                    x1=float(bbox_raw[2]),
                    y1=float(bbox_raw[3]),
                )

            block_type = type_map.get(bj.get("type", ""), BlockType.PARAGRAPH)
            heading_level = level_map.get(
                bj.get("heading_level", "none"), HeadingLevel.NONE
            )

            # Parse style
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

            # Parse table structure
            table_structure = None
            table_json = bj.get("table")
            if table_json and block_type == BlockType.TABLE:
                cells = []
                for cj in table_json.get("cells", []):
                    cells.append(TableCell(
                        row=cj.get("row", 0),
                        col=cj.get("col", 0),
                        rowspan=cj.get("rowspan", 1),
                        colspan=cj.get("colspan", 1),
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

            # Determine role
            role = "paragraph"
            if block_type == BlockType.HEADING:
                if heading_level == HeadingLevel.H1:
                    role = "title"
                else:
                    role = "section_heading"
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

        # Sort by reading_order
        blocks.sort(key=lambda b: b.reading_order)

        return blocks
