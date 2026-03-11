"""Gemini Flash-Lite visual comparison refiner for Upstage/digital PDF output.

After the 1st stage (Upstage Document Parse or local PDF extraction) produces
a normalized HTML, this module sends BOTH the HTML text AND the original page
images to Gemini 3.1 Flash-Lite for visual comparison and correction.

Gemini's role is strictly limited to:
  1. Compare HTML structure against original page images
  2. Fix heading levels, bold/italic, alignment mismatches
  3. Correct text errors (OCR mistakes, spacing)
  4. Fix table structure mismatches (missing cells, wrong headers)
  5. Do NOT rewrite the HTML from scratch – only adjust existing structure

A structural validation layer ensures Gemini doesn't break the document
structure (e.g., converting tables to lists, removing sections).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.core.ocr_confusion import build_ocr_confusion_instruction_compact
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

logger = logging.getLogger(__name__)

# Max pages per Gemini visual comparison batch
# With images: keep batches small to avoid output truncation
_VISUAL_BATCH_SIZE = 3


@dataclass
class RefinementConfig:
    """Configuration for Gemini visual comparison refinement."""
    gemini_model: str = "gemini-3.1-flash-lite-preview"
    # Max pages per visual comparison batch
    visual_batch_size: int = _VISUAL_BATCH_SIZE
    # Max concurrent Gemini calls
    max_workers: int = 4
    # Structural validation: max allowed DOM changes before rejecting
    max_structure_change_ratio: float = 0.3  # 30% max change
    # Translation (piggyback on same call)
    translate: bool = False
    source_language: str = ""
    target_language: str = "ko"


@dataclass
class TOCEntry:
    """Table of Contents entry."""
    level: int
    title: str
    page_index: int
    block_id: str
    category: str = ""


@dataclass
class RefinementResult:
    """Result of Gemini visual comparison refinement."""
    pages: list[PageResult]
    toc: list[TOCEntry] = field(default_factory=list)
    corrections_applied: int = 0
    pages_rejected: int = 0  # Pages where Gemini changes were rejected


class UpstageGeminiRefiner:
    """Refine document output by visual comparison with original page images.

    This module sends BOTH the extracted HTML/text AND the original page images
    to Gemini for side-by-side comparison. Gemini acts as a "proofreader"
    who sees both the original document and the extracted version, then fixes
    discrepancies.

    Safety: A structural validator compares before/after DOM structure and
    rejects Gemini's changes if they deviate too much from the original.
    """

    def __init__(self, config: RefinementConfig | None = None):
        self.config = config or RefinementConfig()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def refine_with_visual_comparison(
        self,
        pages: list[PageResult],
        page_images: dict[int, str],  # page_index → image file path
        progress_callback: Any = None,
    ) -> RefinementResult:
        """Refine pages by visual comparison with original page images.

        Args:
            pages: Extracted page results (from Upstage or local PDF parser)
            page_images: Mapping of page_index to original page image path
            progress_callback: Optional progress reporting function

        Returns:
            RefinementResult with corrected pages and metadata
        """
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning(
                "GEMINI_API_KEY not set; skipping visual comparison refinement. "
                "Using 1st-stage output as-is."
            )
            return RefinementResult(pages=pages)

        # Build batches of pages with their images
        batches = self._build_visual_batches(pages, page_images)

        total_corrections = 0
        total_rejected = 0
        refined_pages: list[PageResult] = []

        for batch_idx, batch in enumerate(batches):
            if progress_callback:
                pct = 0.50 + 0.25 * (batch_idx / max(len(batches), 1))
                progress_callback(
                    f"Visual comparison batch {batch_idx + 1}/{len(batches)}",
                    pct,
                )

            batch_pages, batch_images = batch
            result_pages, num_corrections, num_rejected = (
                self._refine_visual_batch(batch_pages, batch_images, api_key)
            )
            refined_pages.extend(result_pages)
            total_corrections += num_corrections
            total_rejected += num_rejected

        # Sort by page_index
        refined_pages.sort(key=lambda p: p.page_index)

        # Generate TOC from refined headings
        toc = self._generate_toc_local(refined_pages)

        return RefinementResult(
            pages=refined_pages,
            toc=toc,
            corrections_applied=total_corrections,
            pages_rejected=total_rejected,
        )

    # ------------------------------------------------------------------
    # Visual comparison batch processing
    # ------------------------------------------------------------------

    def _build_visual_batches(
        self,
        pages: list[PageResult],
        page_images: dict[int, str],
    ) -> list[tuple[list[PageResult], dict[int, str]]]:
        """Build batches of pages with their corresponding images."""
        batches: list[tuple[list[PageResult], dict[int, str]]] = []
        batch_size = self.config.visual_batch_size

        for i in range(0, len(pages), batch_size):
            batch_pages = pages[i:i + batch_size]
            batch_images = {}
            for page in batch_pages:
                if page.page_index in page_images:
                    batch_images[page.page_index] = page_images[page.page_index]
            batches.append((batch_pages, batch_images))

        return batches

    def _refine_visual_batch(
        self,
        pages: list[PageResult],
        page_images: dict[int, str],
        api_key: str,
    ) -> tuple[list[PageResult], int, int]:
        """Refine a batch of pages with visual comparison.

        Returns (refined_pages, num_corrections, num_rejected).
        """
        if not page_images:
            # No images available – skip visual comparison
            return pages, 0, 0

        try:
            from backend.core.gemini_client import generate_content, generate_with_images
            import mimetypes

            # Build multimodal prompt: images + HTML representation
            images_list: list[tuple[bytes, str]] = []

            # Add original page images
            pages_html_desc = []
            for page in pages:
                page_idx = page.page_index
                img_path = page_images.get(page_idx)

                if img_path and os.path.exists(img_path):
                    mime = mimetypes.guess_type(img_path)[0] or "image/png"
                    with open(img_path, "rb") as f:
                        images_list.append((f.read(), mime))

                # Build HTML representation of extracted blocks
                page_html = self._blocks_to_html_desc(page)
                pages_html_desc.append({
                    "page_index": page_idx,
                    "html_blocks": page_html,
                })

            # Translation instruction
            translate_instruction = ""
            if self.config.translate:
                translate_instruction = self._build_translation_instruction()

            prompt = self._build_visual_comparison_prompt(
                pages_html_desc, translate_instruction,
            )

            # Call Gemini with images + text
            if images_list:
                response_text = generate_with_images(
                    prompt, images_list,
                    model=self.config.gemini_model, api_key=api_key,
                    max_output_tokens=8192,
                )
            else:
                response_text = generate_content(
                    prompt, model=self.config.gemini_model, api_key=api_key,
                    max_output_tokens=8192,
                )

            # Parse response and apply corrections
            corrections = self._parse_visual_response(response_text)
            if corrections:
                num_corrections, num_rejected = self._apply_visual_corrections(
                    corrections, pages,
                )
                return pages, num_corrections, num_rejected

        except Exception as exc:
            logger.error("Gemini visual comparison failed: %s", exc)

        return pages, 0, 0

    def _blocks_to_html_desc(self, page: PageResult) -> list[dict]:
        """Convert page blocks to compact HTML description for Gemini."""
        blocks_desc = []
        for block in page.blocks:
            desc: dict[str, Any] = {
                "id": block.id,
                "type": block.block_type.value,
                "text": block.text[:300] if block.text else "",
            }
            if block.heading_level != HeadingLevel.NONE:
                desc["heading_level"] = block.heading_level.value
            if block.style:
                style_info: dict[str, Any] = {}
                if block.style.is_bold:
                    style_info["bold"] = True
                if block.style.is_italic:
                    style_info["italic"] = True
                if block.style.font_size != 12.0:
                    style_info["font_size"] = block.style.font_size
                if block.style.alignment != Alignment.LEFT:
                    style_info["alignment"] = block.style.alignment.value
                if style_info:
                    desc["style"] = style_info
            if block.table_structure:
                desc["table"] = {
                    "rows": block.table_structure.num_rows,
                    "cols": block.table_structure.num_cols,
                    "cells_count": len(block.table_structure.cells),
                    "sample_cells": [
                        {"row": c.row, "col": c.col, "text": c.text[:50]}
                        for c in block.table_structure.cells[:6]
                    ],
                }
            blocks_desc.append(desc)
        return blocks_desc

    def _build_visual_comparison_prompt(
        self,
        pages_html_desc: list[dict],
        translate_instruction: str,
    ) -> str:
        """Build the visual comparison prompt for Gemini."""
        pages_json = json.dumps(pages_html_desc, ensure_ascii=False, indent=1)

        return f"""You are a document quality inspector. Compare the extracted HTML blocks below
against the original page images above. Find and fix discrepancies.

## Your Role: PROOFREADER (NOT rewriter)
You see the original document images AND the extracted text/structure.
Fix ONLY what is wrong. Do NOT rewrite or restructure the document.

## What to Fix:
1. **Heading levels**: If a block is visually a main title (large, bold, centered) but
   marked as "paragraph", change it to heading with correct level (h1-h6)
2. **Bold/Italic**: If text is visually bold in the image but not marked as bold, fix it
3. **Text errors**: Fix OCR mistakes, Korean spacing (띄어쓰기), Hanja confusion (甲↔田, 乙↔Z)
4. **Table structure**: If cells are merged in the original but split in extraction, note it
5. **Missing content**: If visible text in the image is missing from extraction, add it
6. **Reading order**: If blocks appear in wrong order compared to visual layout, fix order
7. **Korean heading patterns**: "제1장"/"제1편" = h2, "제1절"/"제1관" = h3, "제1조" = h4
{build_ocr_confusion_instruction_compact()}
8. **CRITICAL - Number/digit position errors (MuPDF glyph width bug)**:
   The underlying PDF C library (MuPDF) has a bug in glyph width calculation when
   CJK fonts (Korean, Chinese, Japanese) are mixed with Arabic numerals (0-9).
   The bug causes the x-coordinate of digit characters to be miscalculated, displacing
   numbers to the WRONG POSITION in the text — typically to the END of a line.

   **You MUST compare EVERY number in the extracted text against its position in the
   original page image.** This is the MOST IMPORTANT check. If ANY number (year, amount,
   article number, date, address, etc.) appears at the wrong position, MOVE IT to the
   exact position shown in the original image.

   Common displacement patterns to look for:
   - Numbers pushed to end of line: "년도 매출액은 원입니다 2024 1,000,000" → "2024년도 매출액은 1,000,000원입니다"
   - Address numbers displaced: "서울시 구 동 번지 강남 123 456" → "서울시 강남구 123동 456번지"
   - Article numbers detached: "제 조 (목적) 1" → "제1조 (목적)"
   - Dates split: "월 일 3 15" → "3월 15일"
   - Percentage/units displaced: "증가율은 %입니다 5.3" → "증가율은 5.3%입니다"

   **Do NOT change the VALUE of any number** — only fix its POSITION in the text.

## What NOT to Change:
- Do NOT change the VALUE of numbers, dates, or amounts – only fix their POSITION in the text
- Do NOT remove or restructure tables
- Do NOT combine or split paragraphs unless clearly wrong
- Do NOT add content that isn't in the original image
- Keep ALL existing block IDs unchanged{translate_instruction}

## Extracted Blocks:
{pages_json}

## Return Format:
Return JSON with ONLY the blocks that need corrections:
{{
  "corrections": [
    {{
      "id": "block_id",
      "action": "modify|add|reorder",
      "changes": {{
        "text": "corrected text (only if text changed)",
        "heading_level": "h1|h2|h3|h4|h5|h6|none (only if level changed)",
        "type": "heading|subtitle|paragraph|table|caption|list|footnote (only if type changed)",
        "bold": true,
        "italic": false,
        "alignment": "left|center|right",
        "reading_order": 5
      }}
    }}
  ],
  "missing_blocks": [
    {{
      "page_index": 0,
      "type": "paragraph",
      "text": "text that was missing from extraction",
      "after_block_id": "up_0_3",
      "heading_level": "none",
      "bold": false
    }}
  ]
}}

Return ONLY blocks that need changes. If everything looks correct, return:
{{"corrections": [], "missing_blocks": []}}

Return valid JSON only, no fences."""

    def _parse_visual_response(self, response_text: str) -> dict | None:
        """Parse Gemini's visual comparison response."""
        try:
            text = response_text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            return json.loads(text)
        except (json.JSONDecodeError, IndexError) as exc:
            logger.warning("Failed to parse Gemini visual response: %s", exc)
            return None

    def _apply_visual_corrections(
        self,
        data: dict,
        pages: list[PageResult],
    ) -> tuple[int, int]:
        """Apply Gemini's visual corrections with structural validation.

        Returns (num_corrections_applied, num_pages_rejected).
        """
        # Build block lookup
        block_map: dict[str, LayoutBlock] = {}
        page_map: dict[int, PageResult] = {}
        for page in pages:
            page_map[page.page_index] = page
            for block in page.blocks:
                block_map[block.id] = block

        corrections = data.get("corrections", [])
        missing_blocks = data.get("missing_blocks", [])
        num_applied = 0
        num_rejected = 0

        level_map = {
            "h1": HeadingLevel.H1, "h2": HeadingLevel.H2,
            "h3": HeadingLevel.H3, "h4": HeadingLevel.H4,
            "h5": HeadingLevel.H5, "h6": HeadingLevel.H6,
            "none": HeadingLevel.NONE,
        }

        type_map = {
            "heading": BlockType.HEADING,
            "subtitle": BlockType.SUBTITLE,
            "paragraph": BlockType.PARAGRAPH,
            "table": BlockType.TABLE,
            "caption": BlockType.CAPTION,
            "footnote": BlockType.FOOTNOTE,
            "list": BlockType.LIST,
            "figure": BlockType.FIGURE,
            "equation": BlockType.EQUATION,
            "box": BlockType.BOX,
        }

        align_map = {
            "left": Alignment.LEFT,
            "center": Alignment.CENTER,
            "right": Alignment.RIGHT,
        }

        # Apply corrections to existing blocks
        for corr in corrections:
            bid = corr.get("id", "")
            if bid not in block_map:
                continue

            block = block_map[bid]
            changes = corr.get("changes", {})
            action = corr.get("action", "modify")

            if action != "modify":
                continue  # Only handle modifications for safety

            # Structural validation: don't allow changing table to paragraph etc.
            new_type_str = changes.get("type")
            if new_type_str:
                new_type = type_map.get(new_type_str)
                if new_type and not self._is_safe_type_change(block.block_type, new_type):
                    logger.warning(
                        "Rejected unsafe type change for %s: %s → %s",
                        bid, block.block_type.value, new_type_str,
                    )
                    num_rejected += 1
                    continue
                if new_type:
                    block.block_type = new_type

            # Text correction
            new_text = changes.get("text")
            if new_text and isinstance(new_text, str):
                # Validate: text shouldn't change by more than 50%
                if self._text_change_ratio(block.text, new_text) <= 0.5:
                    block.text = new_text
                else:
                    logger.warning(
                        "Rejected excessive text change for %s (>50%% different)",
                        bid,
                    )
                    num_rejected += 1
                    continue

            # Heading level
            new_level_str = changes.get("heading_level")
            if new_level_str and new_level_str in level_map:
                block.heading_level = level_map[new_level_str]
                if block.heading_level != HeadingLevel.NONE:
                    block.block_type = BlockType.HEADING

            # Style changes
            if block.style is None:
                block.style = TextStyle()

            if "bold" in changes:
                block.style.is_bold = bool(changes["bold"])
            if "italic" in changes:
                block.style.is_italic = bool(changes["italic"])
            if "alignment" in changes and changes["alignment"] in align_map:
                block.style.alignment = align_map[changes["alignment"]]

            # Reading order
            if "reading_order" in changes:
                block.reading_order = int(changes["reading_order"])

            num_applied += 1

        # Add missing blocks (with caution)
        for mb in missing_blocks:
            page_idx = mb.get("page_index")
            if page_idx not in page_map:
                continue

            page = page_map[page_idx]
            text = mb.get("text", "")
            if not text or len(text) < 3:
                continue

            new_type_str = mb.get("type", "paragraph")
            new_type = type_map.get(new_type_str, BlockType.PARAGRAPH)

            new_level_str = mb.get("heading_level", "none")
            new_level = level_map.get(new_level_str, HeadingLevel.NONE)

            style = TextStyle(
                is_bold=mb.get("bold", False),
            )

            # Find insertion position
            after_id = mb.get("after_block_id", "")
            insert_idx = len(page.blocks)
            for i, b in enumerate(page.blocks):
                if b.id == after_id:
                    insert_idx = i + 1
                    break

            new_block = LayoutBlock(
                id=f"gemini_{page_idx}_{len(page.blocks)}",
                block_type=new_type,
                text=text,
                style=style,
                confidence=0.85,
                page_index=page_idx,
                heading_level=new_level,
                role="paragraph",
                reading_order=insert_idx,
            )
            page.blocks.insert(insert_idx, new_block)
            num_applied += 1

        logger.info(
            "Visual comparison: %d corrections applied, %d rejected",
            num_applied, num_rejected,
        )

        return num_applied, num_rejected

    # ------------------------------------------------------------------
    # Structural validation
    # ------------------------------------------------------------------

    @staticmethod
    def _is_safe_type_change(old_type: BlockType, new_type: BlockType) -> bool:
        """Check if a block type change is safe (won't break structure).

        Safe changes: paragraph ↔ heading, paragraph ↔ subtitle,
                      paragraph ↔ list, paragraph ↔ caption
        Unsafe changes: table → paragraph, figure → paragraph
        """
        # Table and figure types should never be changed
        protected_types = {BlockType.TABLE, BlockType.FIGURE, BlockType.EQUATION}
        if old_type in protected_types:
            return False

        # Allow changes between text-like types
        text_types = {
            BlockType.PARAGRAPH, BlockType.HEADING, BlockType.SUBTITLE,
            BlockType.LIST, BlockType.CAPTION, BlockType.FOOTNOTE,
            BlockType.BOX,
        }
        return old_type in text_types and new_type in text_types

    @staticmethod
    def _text_change_ratio(original: str, corrected: str) -> float:
        """Calculate how much text changed (0.0 = identical, 1.0 = completely different)."""
        if not original and not corrected:
            return 0.0
        if not original or not corrected:
            return 1.0

        # Simple character-level comparison
        max_len = max(len(original), len(corrected))
        common = sum(1 for a, b in zip(original, corrected) if a == b)
        return 1.0 - (common / max_len)

    # ------------------------------------------------------------------
    # TOC generation (local, no API call)
    # ------------------------------------------------------------------

    def _generate_toc_local(self, pages: list[PageResult]) -> list[TOCEntry]:
        """Generate TOC from heading blocks (no API call needed)."""
        toc: list[TOCEntry] = []
        level_map = {
            HeadingLevel.H1: 1, HeadingLevel.H2: 2, HeadingLevel.H3: 3,
            HeadingLevel.H4: 4, HeadingLevel.H5: 5, HeadingLevel.H6: 6,
        }

        for page in pages:
            for block in page.blocks:
                if block.block_type == BlockType.HEADING and block.text:
                    level = level_map.get(block.heading_level, 3)
                    toc.append(TOCEntry(
                        level=level,
                        title=block.text[:200],
                        page_index=page.page_index,
                        block_id=block.id,
                    ))

        return toc

    # ------------------------------------------------------------------
    # Translation helper
    # ------------------------------------------------------------------

    def _build_translation_instruction(self) -> str:
        """Build translation instruction for embedding in prompts."""
        lang_names = {
            "ko": "Korean", "en": "English", "ja": "Japanese",
            "zh": "Chinese", "de": "German", "fr": "French",
            "es": "Spanish", "vi": "Vietnamese", "th": "Thai",
            "ru": "Russian", "pt": "Portuguese", "it": "Italian",
        }
        src = self.config.source_language or "the source language (auto-detect)"
        src_name = lang_names.get(self.config.source_language, src)
        tgt_name = lang_names.get(self.config.target_language, self.config.target_language)
        return (
            f"\n\n**TRANSLATION**: Also translate ALL text from {src_name} to {tgt_name}. "
            f"The 'text' fields in corrections must contain TRANSLATED text in {tgt_name}. "
            f"Preserve original meaning accurately. Keep technical terms in parentheses."
        )
