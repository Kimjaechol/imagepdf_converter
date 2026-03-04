"""Reading order refinement – determine natural reading sequence."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import numpy as np

from backend.models.schema import BBox, BlockType, LayoutBlock, PageResult

logger = logging.getLogger(__name__)


class ReadingOrderRefiner:
    """Determine the correct reading order of layout blocks.

    Supports three modes:
      - rule_based: heuristic column detection + top-to-bottom ordering
      - vlm: use a VLM (Gemini / Ollama) to determine reading order
      - hybrid: rule-based first, then VLM for complex pages
    """

    def __init__(
        self,
        mode: str = "hybrid",
        vlm_provider: str = "gemini",
        gemini_model: str = "gemini-2.5-flash",
        ollama_model: str = "qwen2.5:7b",
        ollama_base_url: str = "http://localhost:11434",
    ):
        self.mode = mode
        self.vlm_provider = vlm_provider
        self.gemini_model = gemini_model
        self.ollama_model = ollama_model
        self.ollama_base_url = ollama_base_url

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def refine(
        self,
        blocks: list[LayoutBlock],
        page_width: float,
        page_height: float,
        image_path: str | None = None,
        page_index: int = 0,
    ) -> list[LayoutBlock]:
        """Sort *blocks* into natural reading order.

        Returns blocks with `reading_order` and `column_index` set.
        """
        if not blocks:
            return blocks

        # 1. Always run rule-based first
        blocks = self._rule_based_order(blocks, page_width, page_height)

        # 2. If hybrid/vlm and page is complex, refine with VLM
        if self.mode in ("vlm", "hybrid") and self._is_complex_page(blocks):
            blocks = self._vlm_refine(blocks, page_width, page_height, image_path)

        # Resolve footnote / annotation links
        blocks = self._link_annotations(blocks)

        return blocks

    # ------------------------------------------------------------------
    # Rule-based ordering
    # ------------------------------------------------------------------

    def _rule_based_order(
        self,
        blocks: list[LayoutBlock],
        page_width: float,
        page_height: float,
    ) -> list[LayoutBlock]:
        """Heuristic reading order based on column detection."""
        # 1. Detect columns via x-position clustering
        num_cols = self._detect_columns(blocks, page_width)

        # 2. Assign column index
        if num_cols > 1:
            col_boundaries = self._compute_column_boundaries(blocks, num_cols, page_width)
            for block in blocks:
                if block.bbox:
                    block.column_index = self._assign_column(block.bbox, col_boundaries)
        else:
            for block in blocks:
                block.column_index = 0

        # 3. Sort: column_index ASC, then y0 ASC (top to bottom within column)
        # Special handling: full-width blocks (headings spanning all columns) come first
        full_width_blocks = []
        column_blocks: dict[int, list[LayoutBlock]] = {}

        for block in blocks:
            if block.bbox and block.bbox.width > page_width * 0.7:
                full_width_blocks.append(block)
            else:
                col = block.column_index
                column_blocks.setdefault(col, []).append(block)

        # Sort full-width blocks by y position
        full_width_blocks.sort(key=lambda b: b.bbox.y0 if b.bbox else 0)

        # Sort each column by y position
        for col in column_blocks:
            column_blocks[col].sort(key=lambda b: b.bbox.y0 if b.bbox else 0)

        # Interleave: insert full-width blocks at their y-position
        ordered: list[LayoutBlock] = []
        fw_idx = 0
        col_indices = sorted(column_blocks.keys())

        # Process columns in order, interleaving full-width blocks
        all_column_blocks = []
        for col in col_indices:
            all_column_blocks.extend(column_blocks[col])

        # Merge all blocks and sort by reading sequence
        all_blocks = full_width_blocks + all_column_blocks

        # Create y-band based ordering
        if num_cols > 1:
            ordered = self._interleave_columns(
                full_width_blocks, column_blocks, col_indices
            )
        else:
            ordered = sorted(all_blocks, key=lambda b: b.bbox.y0 if b.bbox else 0)

        # Assign reading_order
        for i, block in enumerate(ordered):
            block.reading_order = i

        return ordered

    def _detect_columns(self, blocks: list[LayoutBlock], page_width: float) -> int:
        """Detect number of columns using x-center clustering."""
        centers = []
        for b in blocks:
            if b.bbox and b.bbox.width < page_width * 0.7:
                centers.append(b.bbox.center_x)

        if len(centers) < 3:
            return 1

        centers_arr = np.array(centers).reshape(-1, 1)

        # Simple gap-based detection
        sorted_centers = sorted(centers)
        gaps = []
        for i in range(1, len(sorted_centers)):
            gaps.append(sorted_centers[i] - sorted_centers[i - 1])

        if not gaps:
            return 1

        # If there's a significant gap in the middle, it's multi-column
        mid = page_width / 2
        left_count = sum(1 for c in centers if c < mid * 0.85)
        right_count = sum(1 for c in centers if c > mid * 1.15)

        if left_count >= 2 and right_count >= 2:
            return 2

        # Check for 3-column
        third = page_width / 3
        c1 = sum(1 for c in centers if c < third * 1.1)
        c2 = sum(1 for c in centers if third * 0.9 < c < third * 2.1)
        c3 = sum(1 for c in centers if c > third * 1.9)
        if c1 >= 2 and c2 >= 2 and c3 >= 2:
            return 3

        return 1

    def _compute_column_boundaries(
        self, blocks: list[LayoutBlock], num_cols: int, page_width: float
    ) -> list[float]:
        """Return column boundary x-coordinates."""
        step = page_width / num_cols
        return [step * (i + 1) for i in range(num_cols - 1)]

    def _assign_column(self, bbox: BBox, boundaries: list[float]) -> int:
        cx = bbox.center_x
        for i, boundary in enumerate(boundaries):
            if cx < boundary:
                return i
        return len(boundaries)

    def _interleave_columns(
        self,
        full_width: list[LayoutBlock],
        column_blocks: dict[int, list[LayoutBlock]],
        col_indices: list[int],
    ) -> list[LayoutBlock]:
        """Interleave full-width blocks with column-ordered blocks.

        Reading order: for each y-band, read left column fully, then right column,
        inserting full-width elements at their y-position.
        """
        result: list[LayoutBlock] = []
        fw_iter = iter(full_width)
        current_fw = next(fw_iter, None)

        # Create y-sorted list per column
        col_iters = {}
        col_nexts: dict[int, LayoutBlock | None] = {}
        for col in col_indices:
            col_iters[col] = iter(column_blocks.get(col, []))
            col_nexts[col] = next(col_iters[col], None)

        while True:
            # Find the next item across all sources
            candidates: list[tuple[float, str, int | None, LayoutBlock]] = []

            if current_fw and current_fw.bbox:
                candidates.append((current_fw.bbox.y0, "fw", None, current_fw))

            for col in col_indices:
                blk = col_nexts[col]
                if blk and blk.bbox:
                    candidates.append((blk.bbox.y0, "col", col, blk))

            if not candidates:
                break

            candidates.sort(key=lambda x: (x[0], 0 if x[1] == "fw" else 1))
            _, src, col, block = candidates[0]

            if src == "fw":
                result.append(block)
                current_fw = next(fw_iter, None)
            else:
                # Read all blocks in this column that are in the same y-band
                # before moving to next column
                result.append(block)
                col_nexts[col] = next(col_iters[col], None)

        return result

    # ------------------------------------------------------------------
    # VLM-based refinement
    # ------------------------------------------------------------------

    def _is_complex_page(self, blocks: list[LayoutBlock]) -> bool:
        """Determine if a page needs VLM refinement (tables, multi-col, annotations)."""
        has_table = any(b.block_type == BlockType.TABLE for b in blocks)
        has_footnote = any(b.block_type in (
            BlockType.FOOTNOTE, BlockType.BALLOON, BlockType.ARROW_ANNOTATION
        ) for b in blocks)
        num_cols = len(set(b.column_index for b in blocks))
        return has_table or has_footnote or num_cols > 1

    def _vlm_refine(
        self,
        blocks: list[LayoutBlock],
        page_width: float,
        page_height: float,
        image_path: str | None,
    ) -> list[LayoutBlock]:
        """Use VLM to refine reading order."""
        if self.vlm_provider == "gemini":
            return self._refine_with_gemini(blocks, page_width, page_height, image_path)
        elif self.vlm_provider == "ollama":
            return self._refine_with_ollama(blocks, page_width, page_height)
        return blocks

    def _refine_with_gemini(
        self,
        blocks: list[LayoutBlock],
        page_width: float,
        page_height: float,
        image_path: str | None,
    ) -> list[LayoutBlock]:
        try:
            import google.generativeai as genai

            api_key = os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                logger.warning("GEMINI_API_KEY not set, skipping VLM refinement.")
                return blocks

            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(self.gemini_model)

            # Prepare block descriptions for the prompt
            block_descs = []
            for b in blocks:
                desc = {
                    "id": b.id,
                    "type": b.block_type.value,
                    "text_preview": b.text[:100] if b.text else "",
                    "bbox": [b.bbox.x0, b.bbox.y0, b.bbox.x1, b.bbox.y1] if b.bbox else [],
                    "column": b.column_index,
                    "current_order": b.reading_order,
                }
                block_descs.append(desc)

            prompt = self._build_reading_order_prompt(block_descs, page_width, page_height)

            parts = [prompt]
            if image_path:
                from PIL import Image
                img = Image.open(image_path)
                parts.insert(0, img)

            response = model.generate_content(parts)
            order_result = self._parse_vlm_response(response.text, blocks)
            if order_result:
                return order_result

        except Exception as exc:
            logger.error("Gemini VLM refinement failed: %s", exc)

        return blocks

    def _refine_with_ollama(
        self,
        blocks: list[LayoutBlock],
        page_width: float,
        page_height: float,
    ) -> list[LayoutBlock]:
        try:
            import httpx

            block_descs = []
            for b in blocks:
                desc = {
                    "id": b.id,
                    "type": b.block_type.value,
                    "text_preview": b.text[:100] if b.text else "",
                    "bbox": [b.bbox.x0, b.bbox.y0, b.bbox.x1, b.bbox.y1] if b.bbox else [],
                    "column": b.column_index,
                }
                block_descs.append(desc)

            prompt = self._build_reading_order_prompt(block_descs, page_width, page_height)

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
            result = resp.json()
            order_result = self._parse_vlm_response(result.get("response", ""), blocks)
            if order_result:
                return order_result

        except Exception as exc:
            logger.error("Ollama VLM refinement failed: %s", exc)

        return blocks

    def _build_reading_order_prompt(
        self,
        block_descs: list[dict],
        page_width: float,
        page_height: float,
    ) -> str:
        return f"""You are a document reading order expert. Given the following layout blocks
detected from a document page ({page_width:.0f}x{page_height:.0f} pixels),
determine the natural reading order.

Rules:
1. For multi-column layouts: read left column top-to-bottom first, then right column.
2. For tables: read row by row, left to right within each row.
3. Full-width headings/paragraphs should be read at their y-position.
4. Footnotes should be linked to their anchor text in the main body.
5. Captions should follow their associated figure/table.
6. Balloon/arrow annotations should be linked to the text they annotate.

Blocks:
{json.dumps(block_descs, ensure_ascii=False, indent=2)}

Return a JSON object with:
{{
  "ordered_ids": ["id1", "id2", ...],
  "links": {{
    "footnote_id": "anchor_id",
    "balloon_id": "target_id"
  }}
}}

Return ONLY the JSON, no other text."""

    def _parse_vlm_response(
        self,
        response_text: str,
        blocks: list[LayoutBlock],
    ) -> list[LayoutBlock] | None:
        """Parse VLM JSON response and reorder blocks."""
        try:
            # Try to extract JSON from response
            text = response_text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            data = json.loads(text)
            ordered_ids = data.get("ordered_ids", [])
            links = data.get("links", {})

            if not ordered_ids:
                return None

            # Reorder blocks
            id_to_block = {b.id: b for b in blocks}
            ordered: list[LayoutBlock] = []
            seen = set()

            for bid in ordered_ids:
                if bid in id_to_block and bid not in seen:
                    ordered.append(id_to_block[bid])
                    seen.add(bid)

            # Add any blocks not mentioned by VLM
            for b in blocks:
                if b.id not in seen:
                    ordered.append(b)

            # Apply links
            for src_id, tgt_id in links.items():
                if src_id in id_to_block:
                    id_to_block[src_id].linked_block_ids.append(tgt_id)

            for i, b in enumerate(ordered):
                b.reading_order = i

            return ordered

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Failed to parse VLM reading order response: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Annotation linking
    # ------------------------------------------------------------------

    def _link_annotations(self, blocks: list[LayoutBlock]) -> list[LayoutBlock]:
        """Link footnotes, balloons, and arrow annotations to their anchors."""
        # Find footnote/annotation blocks
        annotations = [b for b in blocks if b.block_type in (
            BlockType.FOOTNOTE, BlockType.ENDNOTE,
            BlockType.BALLOON, BlockType.ARROW_ANNOTATION,
        )]
        body_blocks = [b for b in blocks if b.block_type not in (
            BlockType.FOOTNOTE, BlockType.ENDNOTE,
            BlockType.BALLOON, BlockType.ARROW_ANNOTATION,
            BlockType.HEADER, BlockType.FOOTER, BlockType.PAGE_NUMBER,
        )]

        for ann in annotations:
            if ann.linked_block_ids:
                continue  # already linked
            # Find nearest body block by position
            if ann.bbox and body_blocks:
                nearest = min(
                    body_blocks,
                    key=lambda b: self._distance(ann.bbox, b.bbox) if b.bbox else float("inf"),
                )
                ann.linked_block_ids.append(nearest.id)

        return blocks

    def _distance(self, a: BBox, b: BBox) -> float:
        dx = a.center_x - b.center_x
        dy = a.center_y - b.center_y
        return (dx * dx + dy * dy) ** 0.5
