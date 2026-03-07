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
_BATCH_COMPLEX = 5      # complex pages: 5 is optimal for accuracy + cost
                        # - 10 pages risks output truncation (8K output limit)
                        # - 5 pages keeps all content within output budget
                        # - More parallel batches = faster total processing


@dataclass
class TranslationContext:
    """Translation settings passed through the pipeline."""
    enabled: bool = False
    source_language: str = ""      # empty = auto-detect
    target_language: str = "ko"

    @property
    def instruction(self) -> str:
        """Build the translation instruction for the AI prompt."""
        if not self.enabled:
            return ""
        src = self.source_language or "the source language (auto-detect)"
        lang_names = {
            "ko": "Korean", "en": "English", "ja": "Japanese",
            "zh": "Chinese", "de": "German", "fr": "French",
            "es": "Spanish", "vi": "Vietnamese", "th": "Thai",
            "ru": "Russian", "pt": "Portuguese", "it": "Italian",
            "ar": "Arabic", "id": "Indonesian",
        }
        src_name = lang_names.get(self.source_language, src)
        tgt_name = lang_names.get(self.target_language, self.target_language)
        return (
            f"\n\n**TRANSLATION**: Translate ALL text from {src_name} to {tgt_name}. "
            f"The 'text' field in your JSON output must contain the TRANSLATED text in {tgt_name}. "
            f"Preserve the original meaning accurately. "
            f"For proper nouns, technical terms, and abbreviations, keep the original in parentheses "
            f"after the translation, e.g. '대한민국(大韓民國)' or '인공지능(AI)'. "
            f"Table cell text must also be translated. "
            f"Do NOT include the original text separately – only the translated version."
        )


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
    is_scanned: bool = False  # True if page has no digital text (scanned/image PDF)

    @property
    def is_complex(self) -> bool:
        return (
            self.has_tables
            or self.has_figures
            or self.has_multi_column
            or (self.num_vector_rects > 3)
            or self.is_scanned  # Scanned pages MUST use vision AI
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
        translate: bool = False,
        source_language: str = "",
        target_language: str = "ko",
    ) -> list[PageResult]:
        """Process multiple pages via unified Gemini Vision call(s).

        1. Fast local pre-scan → classify each page as TAG=0 or TAG=1
        2. TAG=0 pages: text-only correction (no images sent to Gemini)
           - With translation: text still sent to Gemini for translation
        3. TAG=1 pages: full vision analysis (images sent to Gemini)

        When translate=True, ALL pages are sent to Gemini (TAG=0 pages
        still send only text, not images) because translation requires AI.
        The translation instruction is embedded in the same single prompt,
        so conversion + translation happen in one API call with no extra cost.
        """
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set; unified vision mode unavailable.")
            return []

        ocr_blocks = ocr_blocks_per_page or {}

        # Build translation context if needed
        translation_ctx = None
        if translate:
            translation_ctx = TranslationContext(
                enabled=True,
                source_language=source_language,
                target_language=target_language,
            )
            logger.info(
                "Translation enabled: %s → %s",
                source_language or "auto-detect",
                target_language,
            )

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
        # With translation: Gemini still gets only text, but also translates
        for i in range(0, len(text_only_pages), _BATCH_TEXT_ONLY):
            batch = text_only_pages[i : i + _BATCH_TEXT_ONLY]
            batch_results = self._process_text_only_batch(
                batch, ocr_blocks, api_key, classifications,
                translation_ctx=translation_ctx,
            )
            results.extend(batch_results)

        # Step 3b: TAG=1 pages – full VISION analysis (images sent)
        for i in range(0, len(complex_pages), _BATCH_COMPLEX):
            batch = complex_pages[i : i + _BATCH_COMPLEX]
            batch_results = self._process_complex_batch(
                batch, ocr_blocks, api_key, classifications,
                translation_ctx=translation_ctx,
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
            # Scanned pages MUST be TAG=1 because:
            # 1. Without the image, AI cannot detect multi-column layout
            # 2. Local OCR alone cannot determine reading order for complex layouts
            # 3. Colored backgrounds, boxes, special symbols need vision AI
            if cls.num_text_blocks == 0:
                cls.is_scanned = True

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
        translation_ctx: TranslationContext | None = None,
    ) -> list[PageResult]:
        """TAG=0: Send only OCR TEXT to Gemini (no images).

        Local OCR (Surya) already extracted high-quality text.
        Gemini corrects errors, classifies headings, and optionally translates.
        This saves ~1,500 input tokens per page (no image tokens).

        When translation is enabled, translation happens in the SAME single
        API call – no additional cost beyond the extra output tokens for
        translated text.
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

            prompt = self._build_text_only_prompt(
                page_infos, translation_ctx=translation_ctx,
            )

            # Single text-only call – no images in parts
            response = model.generate_content(
                prompt,
                generation_config={"max_output_tokens": 8192},
            )

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
        translation_ctx: TranslationContext | None = None,
    ) -> list[PageResult]:
        """TAG=1: Send page IMAGES to Gemini for full vision analysis.

        Complex pages need Gemini to see the actual image for table
        structure, figure detection, multi-column layout, etc.
        When translation is enabled, translation is done in the same call.
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

            prompt = self._build_complex_prompt(
                page_infos, translation_ctx=translation_ctx,
            )
            parts.append(prompt)

            # Multimodal call – images + text prompt
            response = model.generate_content(
                parts,
                generation_config={"max_output_tokens": 8192},
            )

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

    def _build_text_only_prompt(
        self,
        page_infos: list[dict],
        translation_ctx: TranslationContext | None = None,
    ) -> str:
        """Lighter prompt for text-only pages – no table schema needed."""
        pages_desc = []
        for pi in page_infos:
            desc = f"Page {pi['page_index']} {pi['hints']} ({pi['width']:.0f}x{pi['height']:.0f}px)"
            if pi["existing_text"]:
                desc += f":\n  OCR text (verify/correct):\n{pi['existing_text']}"
            pages_desc.append(desc)

        pages_section = "\n\n".join(pages_desc)

        # Translation instruction (empty string if not translating)
        translate_instruction = ""
        if translation_ctx and translation_ctx.enabled:
            translate_instruction = translation_ctx.instruction

        return f"""You are a document layout analysis expert. Analyze OCR-extracted text from digital PDF pages (TAG=0).
No images are provided. Use the bbox coordinates and style metadata to understand layout.

Each text block includes: bbox [x0,y0,x1,y1], font size (sz), BOLD, alignment.

## Page Layout Zone Classification
Classify each block into one of 4 page zones:
1. **Header zone**: blocks at the very top (y0 < 8% of page height), small font or edge-aligned → type "header" or "page_number"
2. **Body zone**: main content area → paragraphs, headings, subtitles, lists, tables
3. **Footnote zone**: blocks below a horizontal separator in the bottom 25% → type "footnote"
4. **Footer zone**: blocks at the very bottom (y1 > 88% of page height), small font or digit-only → type "footer" or "page_number"

## Multi-Column Detection (even without separator lines)
Analyze bbox x-coordinates to detect multi-column layout:
- If blocks cluster into distinct left/right groups with a consistent vertical gap → this is multi-column
- Detect 2-column, 3-column, or N-column layouts from x-coordinate clustering
- Read each column top-to-bottom in order: leftmost column FIRST → next → rightmost
- Full-width blocks (spanning >70% page width) are read at their Y-position between columns

## Reading Order Rules
1. Skip header/page_number at top
2. Read body: if multi-column, left column first → next column → right column
3. Read footnotes after all body content
4. Skip footer/page_number at bottom

## Heading Classification
- Large font (sz>16pt) + BOLD + center → h1 or h2
- Large font (sz>14pt) + BOLD → h2 or h3
- Slightly larger font + BOLD directly under a heading → subtitle
- Korean: "제1장"/"제1편" = h2, "제1절"/"제1관" = h3, "제1조" = h4
- No level skipping (h1→h3 invalid, must be h1→h2→h3)

## Relation Extraction
- If a caption-like block exists (e.g. "표 1.", "그림 2.", "Table 1"), set parent_id to the nearest table/figure block's id
- For footnotes with markers (e.g. [1], *, †): set footnote_marker to the marker text

## Tasks
1. **Correct OCR errors**: Korean Hanja (甲→갑), spelling, spacing (띄어쓰기).
2. **Classify blocks**: heading, subtitle, paragraph, list, caption, footnote, header, footer, page_number.
3. **Determine reading order**: Following the zone + column rules above.
4. **Relation extraction**: Link captions to tables/figures, set footnote markers.
5. **Preserve bbox and styles**: Keep original bbox. Set font_size_relative from sz metadata.{translate_instruction}

{pages_section}

Return JSON:
{{
  "pages": [
    {{
      "page_index": 0,
      "blocks": [
        {{
          "id": "p0_b0",
          "type": "heading|subtitle|paragraph|caption|footnote|header|footer|page_number|list",
          "text": "corrected text",
          "bbox": [x0, y0, x1, y1],
          "reading_order": 0,
          "heading_level": "h1|h2|h3|h4|h5|h6|none",
          "column_index": 0,
          "footnote_marker": null,
          "style": {{"bold": false, "italic": false, "font_size_relative": "large|normal|small", "alignment": "left|center|right"}},
          "parent_id": null
        }}
      ]
    }}
  ]
}}

Return ONLY valid JSON. No fences. Include ALL visible text."""

    def _build_complex_prompt(
        self,
        page_infos: list[dict],
        translation_ctx: TranslationContext | None = None,
    ) -> str:
        """Full prompt for complex pages with tables/figures/multi-column."""
        pages_desc = []
        for pi in page_infos:
            desc = f"Page {pi['page_index']} {pi['hints']} ({pi['width']:.0f}x{pi['height']:.0f}px)"
            if pi["existing_text"]:
                desc += f":\n  OCR text (verify/correct):\n{pi['existing_text']}"
            pages_desc.append(desc)

        pages_section = "\n\n".join(pages_desc)

        # Translation instruction (empty string if not translating)
        translate_instruction = ""
        if translation_ctx and translation_ctx.enabled:
            translate_instruction = translation_ctx.instruction

        return f"""You are a document layout analysis expert. Analyze each page image below using the 3-step process:

## Step 1: Element Detection
Detect EVERY element on the page:
- heading (main title or section heading), subtitle (sub-heading directly under a heading)
- paragraph, list, table, figure, equation, caption
- header (running head at page top), footer (running foot at page bottom), page_number
- footnote (below horizontal separator near page bottom)
- box (text inside colored/bordered rectangles), balloon (callout/speech bubble)

## Step 2: Context-Aware Reading Order (CRITICAL)
Determine reading order as a HUMAN would read this document:

**Page Layout Zones (process in this order):**
1. SKIP header/page_number at the very top of the page
2. READ the body content zone (main area between header and footnotes)
3. READ footnotes (below the horizontal separator line near page bottom)
4. SKIP footer/page_number at the very bottom of the page

**Multi-Column Detection (even without visible separator lines):**
- Look for consistent vertical whitespace gaps that divide the page into columns
- Detect 2-column, 3-column, or N-column layouts from whitespace patterns
- Read each column top-to-bottom in order: leftmost column FIRST → next column → ... → rightmost column
- A column separator can be an explicit vertical line OR just a wide consistent gap (3+ character widths)
- Full-width elements (spanning all columns): read at their Y-position in document flow
- Cross-column tables/figures: read as a single unit at their Y-position; set column_index to the column where the table's LEFT edge starts

**Within each column, read top-to-bottom:**
- When you encounter a table, figure, equation, or graph: read it as a unit at its position
- Captions belong to the nearest table/figure above or below them
- Numbered items (①②③ etc.) are read sequentially within their column

## Step 3: Relation Extraction
- Link each caption to its parent table/figure by setting parent_id to the table/figure's id
- For footnotes with markers (e.g. [1], *, †): set footnote_marker to the marker text
- Identify which column each block belongs to (set column_index)
- For cross-column elements: column_index = column where LEFT edge starts

{translate_instruction}

{pages_section}

Return JSON:
{{
  "pages": [
    {{
      "page_index": 0,
      "blocks": [
        {{
          "id": "p0_b0",
          "type": "heading|subtitle|paragraph|table|figure|caption|footnote|header|footer|page_number|list|equation|box",
          "text": "corrected text (fix OCR errors: Hanja 甲乙丙丁, Korean spelling/spacing 띄어쓰기)",
          "bbox": [x0, y0, x1, y1],
          "reading_order": 0,
          "heading_level": "h1|h2|h3|h4|h5|h6|none",
          "column_index": 0,
          "footnote_marker": null,
          "style": {{"bold": false, "italic": false, "font_size_relative": "large|normal|small", "alignment": "left|center|right"}},
          "table": null,
          "parent_id": null
        }},
        {{
          "id": "p0_t0",
          "type": "table",
          "text": "",
          "bbox": [x0, y0, x1, y1],
          "reading_order": 3,
          "heading_level": "none",
          "column_index": 0,
          "footnote_marker": null,
          "style": null,
          "table": {{
            "rows": 3, "cols": 4,
            "cells": [
              {{"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "header", "is_header": true}}
            ]
          }},
          "parent_id": null
        }}
      ]
    }}
  ]
}}

CRITICAL RULES:
- Korean heading patterns: "제1장"/"제1편" = h2, "제1절"/"제1관" = h3, "제1조" = h4. No level skipping.
- bbox in pixels matching page dimensions above.
- Include ALL visible text on the page. Do NOT omit any content.
- For tables, include EVERY cell with exact row/col position.
- For captions, set parent_id to the ID of the related table/figure.
- For footnotes, set footnote_marker to the marker (e.g. "1", "*", "†") if present.
- Return ONLY valid JSON. No markdown fences."""

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_unified_response(
        self,
        response_text: str,
        page_data_list: list[dict],
    ) -> list[PageResult]:
        """Parse the unified JSON response into PageResult objects.

        Safety: validates that AI-returned page_index values match the
        pages we actually sent.  If the AI returns wrong indices (e.g.
        0,1,2 instead of 5,6,7), we remap them by positional order to
        the expected page indices.
        """
        try:
            text = response_text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            data = json.loads(text)
            pages_data = data.get("pages", [])

            # Expected page indices (in the order we sent them)
            expected_indices = [pd["page_index"] for pd in page_data_list]
            expected_set = set(expected_indices)

            page_dims = {
                pd["page_index"]: (pd["width"], pd["height"])
                for pd in page_data_list
            }

            # ── Safety check: detect if AI returned wrong page indices ──
            returned_indices = [p.get("page_index", -1) for p in pages_data]
            returned_set = set(returned_indices)

            # Case 1: AI returned correct indices (subset of expected)
            indices_valid = returned_set.issubset(expected_set)

            # Case 2: AI returned sequential 0,1,2... instead of real indices
            # Detect this by checking if returned indices don't overlap with
            # expected but match positionally (same count or close)
            needs_remap = False
            if not indices_valid and len(pages_data) <= len(expected_indices):
                needs_remap = True
                logger.warning(
                    "AI returned wrong page indices %s, expected %s. "
                    "Remapping by positional order.",
                    returned_indices, expected_indices,
                )

            results: list[PageResult] = []
            for pos, page_json in enumerate(pages_data):
                if needs_remap:
                    # Remap: use positional order to assign correct page_index
                    if pos < len(expected_indices):
                        page_idx = expected_indices[pos]
                    else:
                        logger.warning(
                            "AI returned more pages (%d) than expected (%d), "
                            "skipping extra page at position %d.",
                            len(pages_data), len(expected_indices), pos,
                        )
                        continue
                else:
                    page_idx = page_json.get("page_index", 0)
                    # Even when indices seem valid, reject any page_index
                    # that wasn't in our batch
                    if page_idx not in expected_set:
                        logger.warning(
                            "AI returned unexpected page_index %d "
                            "(expected one of %s), skipping.",
                            page_idx, expected_indices,
                        )
                        continue

                width, height = page_dims.get(page_idx, (0, 0))
                blocks = self._parse_blocks(page_json.get("blocks", []), page_idx)

                results.append(PageResult(
                    page_index=page_idx,
                    width=width,
                    height=height,
                    blocks=blocks,
                ))

            # ── Safety: detect duplicate page indices in results ──
            seen_indices: set[int] = set()
            deduped: list[PageResult] = []
            for pr in results:
                if pr.page_index in seen_indices:
                    logger.warning(
                        "Duplicate page_index %d in AI response, keeping first occurrence.",
                        pr.page_index,
                    )
                    continue
                seen_indices.add(pr.page_index)
                deduped.append(pr)

            # ── Safety: log any pages the AI failed to return ──
            missing = expected_set - seen_indices
            if missing:
                logger.warning(
                    "AI did not return results for pages %s (batch had %s). "
                    "These pages will need fallback processing.",
                    sorted(missing), expected_indices,
                )

            return deduped

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
            "subtitle": BlockType.SUBTITLE,
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
            "box": BlockType.BOX,
            "balloon": BlockType.BALLOON,
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
            elif block_type == BlockType.SUBTITLE:
                role = "subtitle"
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
                column_index=bj.get("column_index", 0),
            )
            # Caption → parent table/figure linking
            parent_id = bj.get("parent_id")
            if parent_id:
                block.linked_block_ids = [parent_id]
                block.parent_block_id = parent_id
            # Footnote marker (e.g. "1", "*", "†")
            fn_marker = bj.get("footnote_marker")
            if fn_marker:
                block.footnote_marker = str(fn_marker)
            blocks.append(block)

        blocks.sort(key=lambda b: b.reading_order)
        return blocks
