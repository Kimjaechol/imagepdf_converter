"""Heading / role classification for layout blocks."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from backend.models.schema import (
    Alignment,
    BlockType,
    HeadingLevel,
    LayoutBlock,
    TextStyle,
)

logger = logging.getLogger(__name__)


class HeadingClassifier:
    """Classify blocks into roles (heading, paragraph, caption, …) and heading levels."""

    def __init__(
        self,
        mode: str = "hybrid",
        llm_provider: str = "gemini",
        ollama_model: str = "qwen2.5:0.5b-instruct",
        ollama_base_url: str = "http://localhost:11434",
        gemini_model: str = "gemini-3.1-flash-lite-preview",
    ):
        self.mode = mode
        self.llm_provider = llm_provider
        self.ollama_model = ollama_model
        self.ollama_base_url = ollama_base_url
        self.gemini_model = gemini_model

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def classify(self, blocks: list[LayoutBlock]) -> list[LayoutBlock]:
        """Classify heading levels and roles for all blocks."""
        if not blocks:
            return blocks

        # 1. Compute style statistics across the document
        style_stats = self._compute_style_stats(blocks)

        # 2. Rule-based classification first
        blocks = self._rule_based_classify(blocks, style_stats)

        # 3. LLM refinement (if mode allows)
        if self.mode in ("llm", "hybrid"):
            blocks = self._llm_classify(blocks, style_stats)

        # 4. Post-process: enforce hierarchy rules
        blocks = self._enforce_heading_hierarchy(blocks)

        return blocks

    # ------------------------------------------------------------------
    # Style statistics
    # ------------------------------------------------------------------

    def _compute_style_stats(self, blocks: list[LayoutBlock]) -> dict[str, Any]:
        """Compute font size distribution and other style stats."""
        font_sizes: list[float] = []
        for b in blocks:
            if b.style and b.style.font_size > 0:
                font_sizes.append(b.style.font_size)

        if not font_sizes:
            return {"median_size": 12, "max_size": 12, "min_size": 12}

        font_sizes.sort()
        median = font_sizes[len(font_sizes) // 2]
        return {
            "median_size": median,
            "max_size": max(font_sizes),
            "min_size": min(font_sizes),
            "sizes": font_sizes,
        }

    # ------------------------------------------------------------------
    # Rule-based classification
    # ------------------------------------------------------------------

    def _rule_based_classify(
        self,
        blocks: list[LayoutBlock],
        style_stats: dict[str, Any],
    ) -> list[LayoutBlock]:
        """Classify blocks based on font size, bold, alignment heuristics."""
        median_size = style_stats.get("median_size", 12)
        max_size = style_stats.get("max_size", 12)

        for block in blocks:
            if block.block_type in (
                BlockType.TABLE, BlockType.FIGURE, BlockType.EQUATION,
                BlockType.HEADER, BlockType.FOOTER, BlockType.PAGE_NUMBER,
            ):
                continue  # Keep existing type

            style = block.style or TextStyle()
            fs = style.font_size
            text = block.text.strip()

            if not text:
                continue

            # Detect headings by style
            if fs > median_size * 1.5 and style.is_bold and style.alignment == Alignment.CENTER:
                block.heading_level = HeadingLevel.H1
                block.role = "title"
                block.block_type = BlockType.HEADING
            elif fs > median_size * 1.3 and style.is_bold:
                block.heading_level = HeadingLevel.H2
                block.role = "section_heading"
                block.block_type = BlockType.HEADING
            elif fs > median_size * 1.1 and style.is_bold:
                block.heading_level = HeadingLevel.H3
                block.role = "subheading"
                block.block_type = BlockType.HEADING
            elif style.is_bold and len(text) < 80 and "\n" not in text:
                block.heading_level = HeadingLevel.H4
                block.role = "subheading"
                block.block_type = BlockType.HEADING
            elif fs < median_size * 0.85:
                # Smaller text might be footnote or caption
                if block.block_type == BlockType.PARAGRAPH:
                    block.role = "caption"
            else:
                block.role = "paragraph"
                block.heading_level = HeadingLevel.NONE

            # Detect numbered headings (제1장, 제2절, etc.)
            import re
            heading_patterns = [
                r"^제\s*\d+\s*[장편절조항]",
                r"^[IⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\.\s",
                r"^[일이삼사오육칠팔구십]+\.\s",
                r"^\d+\.\s+[가-힣]",
                r"^[A-Z]\.\s",
            ]
            for pattern in heading_patterns:
                if re.match(pattern, text):
                    if block.heading_level == HeadingLevel.NONE:
                        block.heading_level = HeadingLevel.H3
                        block.role = "numbered_heading"
                        block.block_type = BlockType.HEADING
                    break

        return blocks

    # ------------------------------------------------------------------
    # LLM-based classification
    # ------------------------------------------------------------------

    def _llm_classify(
        self,
        blocks: list[LayoutBlock],
        style_stats: dict[str, Any],
    ) -> list[LayoutBlock]:
        """Use LLM to refine heading classification."""
        # Prepare block info for LLM
        block_infos = []
        for b in blocks:
            if b.block_type in (BlockType.TABLE, BlockType.FIGURE, BlockType.EQUATION):
                continue
            style = b.style or TextStyle()
            info = {
                "id": b.id,
                "text": b.text[:200] if b.text else "",
                "font_size": style.font_size,
                "bold": style.is_bold,
                "italic": style.is_italic,
                "alignment": style.alignment.value,
                "current_role": b.role,
                "current_level": b.heading_level.value,
            }
            block_infos.append(info)

        if not block_infos:
            return blocks

        prompt = self._build_heading_prompt(block_infos, style_stats)

        if self.llm_provider == "ollama":
            result = self._call_ollama(prompt)
        elif self.llm_provider == "gemini":
            result = self._call_gemini(prompt)
        else:
            return blocks

        if result:
            self._apply_llm_result(result, blocks)

        return blocks

    def _build_heading_prompt(
        self,
        block_infos: list[dict],
        style_stats: dict[str, Any],
    ) -> str:
        return f"""You are a document structure analyzer. Given text blocks with their style attributes,
classify each block's role and heading level.

Document style statistics:
- Median font size: {style_stats.get('median_size', 12):.1f}
- Maximum font size: {style_stats.get('max_size', 12):.1f}

Blocks to classify:
{json.dumps(block_infos, ensure_ascii=False, indent=2)}

Rules:
1. Title (h1): largest font, often centered and bold. Usually only ONE per document.
2. Section heading (h2): bold, larger than body text.
3. Subsection heading (h3-h4): bold, slightly larger or same size as body.
4. Paragraph: normal body text.
5. Caption: smaller text near figures/tables.
6. Footnote: small text at page bottom.
7. Korean numbered headings like "제1장", "제1절" are typically h2 or h3.
8. Heading levels must not skip (h1→h3 without h2 is invalid).

Return JSON array:
[
  {{"id": "block_id", "role": "title|section_heading|subheading|paragraph|caption|footnote", "heading_level": "h1|h2|h3|h4|h5|h6|none"}}
]

Return ONLY the JSON array."""

    def _call_ollama(self, prompt: str) -> list[dict] | None:
        try:
            import httpx
            resp = httpx.post(
                f"{self.ollama_base_url}/api/generate",
                json={
                    "model": self.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
                timeout=60,
            )
            resp.raise_for_status()
            text = resp.json().get("response", "")
            return self._parse_json_response(text)
        except Exception as exc:
            logger.error("Ollama heading classification failed: %s", exc)
            return None

    def _call_gemini(self, prompt: str) -> list[dict] | None:
        try:
            import google.generativeai as genai
            api_key = os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                return None
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(self.gemini_model)
            response = model.generate_content(prompt)
            return self._parse_json_response(response.text)
        except Exception as exc:
            logger.error("Gemini heading classification failed: %s", exc)
            return None

    def _parse_json_response(self, text: str) -> list[dict] | None:
        try:
            text = text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            return json.loads(text)
        except (json.JSONDecodeError, IndexError):
            return None

    def _apply_llm_result(
        self,
        result: list[dict],
        blocks: list[LayoutBlock],
    ) -> None:
        id_to_block = {b.id: b for b in blocks}
        for item in result:
            bid = item.get("id", "")
            if bid not in id_to_block:
                continue
            block = id_to_block[bid]
            role = item.get("role", "")
            level = item.get("heading_level", "none")
            if role:
                block.role = role
            try:
                block.heading_level = HeadingLevel(level)
            except ValueError:
                pass
            if block.heading_level != HeadingLevel.NONE:
                block.block_type = BlockType.HEADING

    # ------------------------------------------------------------------
    # Hierarchy enforcement
    # ------------------------------------------------------------------

    def _enforce_heading_hierarchy(self, blocks: list[LayoutBlock]) -> list[LayoutBlock]:
        """Ensure heading levels don't skip (h1→h3 without h2)."""
        level_order = [
            HeadingLevel.H1, HeadingLevel.H2, HeadingLevel.H3,
            HeadingLevel.H4, HeadingLevel.H5, HeadingLevel.H6,
        ]
        level_to_idx = {lvl: i for i, lvl in enumerate(level_order)}

        last_level_idx = -1
        for block in blocks:
            if block.heading_level == HeadingLevel.NONE:
                continue

            current_idx = level_to_idx.get(block.heading_level, -1)
            if current_idx < 0:
                continue

            # If there's a gap, fill it
            if last_level_idx >= 0 and current_idx > last_level_idx + 1:
                # Adjust to be at most one level deeper
                block.heading_level = level_order[last_level_idx + 1]
                current_idx = last_level_idx + 1

            last_level_idx = current_idx

        return blocks
