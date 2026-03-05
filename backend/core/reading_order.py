"""Reading order refinement – determine natural reading sequence.

Core principle: reading order is determined by **structural lines** (borders,
separators, table edges) that humans intentionally draw to partition content
into distinct reading zones.  Within each zone the text is read top-to-bottom,
left-to-right.  XY-coordinate clustering is only a fallback when no lines are
present.

Non-orthogonal (diagonal) lines are classified as **annotation lines** (arrows,
leader lines, callout connectors) and do NOT define reading zones – they link
an annotation to its target.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from backend.models.schema import BBox, BlockType, LayoutBlock, PageResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@dataclass
class _Zone:
    """A rectangular reading zone defined by structural lines."""
    bbox: BBox
    blocks: list[LayoutBlock] = field(default_factory=list)
    zone_type: str = "content"  # "content" | "footnote" | "header" | "footer"


class ReadingOrderRefiner:
    """Determine the correct reading order of layout blocks.

    Supports three modes:
      - rule_based: line-based zone detection + top-to-bottom ordering
      - vlm: use a VLM (Gemini / Ollama) to determine reading order
      - hybrid: rule-based first, then VLM for complex pages
    """

    def __init__(
        self,
        mode: str = "hybrid",
        vlm_provider: str = "gemini",
        gemini_model: str = "gemini-3.1-flash-lite",
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
        vector_lines: list[dict] | None = None,
    ) -> list[LayoutBlock]:
        """Sort *blocks* into natural reading order.

        *vector_lines* are the classified line/rect objects extracted from the
        PDF page (each has a ``line_class`` of ``"structural"`` or
        ``"annotation"``).

        Returns blocks with ``reading_order`` and ``column_index`` set.
        """
        if not blocks:
            return blocks

        # 1. Primary strategy: line-based zone segmentation
        structural_lines = self._filter_structural(vector_lines or [])
        if structural_lines:
            blocks = self._line_based_order(
                blocks, structural_lines, page_width, page_height,
            )
        else:
            # Fallback: heuristic column detection (no lines available)
            blocks = self._column_heuristic_order(blocks, page_width, page_height)

        # 2. If hybrid/vlm and page is complex, refine with VLM
        if self.mode in ("vlm", "hybrid") and self._is_complex_page(blocks):
            blocks = self._vlm_refine(blocks, page_width, page_height, image_path)

        # 3. Link annotations (footnotes, arrows, balloons)
        blocks = self._link_annotations(blocks, vector_lines or [])

        return blocks

    # ------------------------------------------------------------------
    # Line-based zone ordering (primary strategy)
    # ------------------------------------------------------------------

    def _filter_structural(self, lines: list[dict]) -> list[dict]:
        """Keep only structural (horizontal / vertical) lines & rects."""
        result: list[dict] = []
        for ln in lines:
            if ln["type"] == "rect":
                result.append(ln)
            elif ln["type"] == "line" and ln.get("line_class") == "structural":
                result.append(ln)
        return result

    def _line_based_order(
        self,
        blocks: list[LayoutBlock],
        structural_lines: list[dict],
        page_width: float,
        page_height: float,
    ) -> list[LayoutBlock]:
        """Segment the page into zones using structural lines, then order
        blocks within each zone top-to-bottom, left-to-right."""

        zones = self._build_zones(structural_lines, page_width, page_height)

        if not zones:
            return self._column_heuristic_order(blocks, page_width, page_height)

        # Assign each block to the zone whose bbox best contains it
        unassigned: list[LayoutBlock] = []
        for block in blocks:
            if block.bbox is None:
                unassigned.append(block)
                continue
            best_zone: _Zone | None = None
            best_overlap = 0.0
            for zone in zones:
                overlap = block.bbox.overlap_ratio(zone.bbox)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_zone = zone
            if best_zone is not None and best_overlap > 0.15:
                best_zone.blocks.append(block)
            else:
                unassigned.append(block)

        # Classify zone types (footnote zones are below a horizontal separator
        # in the bottom quarter of the page)
        for zone in zones:
            if zone.bbox.y0 > page_height * 0.75:
                zone.zone_type = "footnote"

        # Sort zones: content zones top-to-bottom / left-to-right first,
        # then footnote zones
        content_zones = [z for z in zones if z.zone_type == "content"]
        footnote_zones = [z for z in zones if z.zone_type == "footnote"]

        content_zones.sort(key=lambda z: (z.bbox.y0, z.bbox.x0))
        footnote_zones.sort(key=lambda z: (z.bbox.y0, z.bbox.x0))

        # Within each zone, sort blocks top-to-bottom, left-to-right
        ordered: list[LayoutBlock] = []
        for zone in content_zones + footnote_zones:
            zone.blocks.sort(key=lambda b: (b.bbox.y0, b.bbox.x0) if b.bbox else (0, 0))
            ordered.extend(zone.blocks)

        # Append any unassigned blocks at the end, sorted by position
        unassigned.sort(key=lambda b: (b.bbox.y0, b.bbox.x0) if b.bbox else (0, 0))
        ordered.extend(unassigned)

        # Set reading_order and column_index
        for i, block in enumerate(ordered):
            block.reading_order = i

        # Assign column indices based on zone x-position
        if len(content_zones) > 1:
            zone_xs = sorted(set(round(z.bbox.center_x) for z in content_zones))
            for zone in content_zones:
                col_idx = 0
                for j, zx in enumerate(zone_xs):
                    if abs(round(zone.bbox.center_x) - zx) < page_width * 0.1:
                        col_idx = j
                        break
                for b in zone.blocks:
                    b.column_index = col_idx

        return ordered

    def _build_zones(
        self,
        structural_lines: list[dict],
        page_width: float,
        page_height: float,
    ) -> list[_Zone]:
        """Build rectangular reading zones from structural lines.

        Strategy:
        1. Collect all rectangles – each rect IS a zone (table cell, boxed
           region, etc.).
        2. Collect horizontal separators that span a significant width – these
           split the page into horizontal bands.
        3. Collect vertical separators – these split bands into columns.
        4. Combine into non-overlapping zones covering the page.
        """
        rects: list[BBox] = []
        h_separators: list[float] = []  # y-positions of horizontal separators
        v_separators: list[float] = []  # x-positions of vertical separators

        for ln in structural_lines:
            if ln["type"] == "rect":
                rw = abs(ln["x1"] - ln["x0"])
                rh = abs(ln["y1"] - ln["y0"])
                if rw > 15 and rh > 10:
                    rects.append(BBox(
                        x0=min(ln["x0"], ln["x1"]),
                        y0=min(ln["y0"], ln["y1"]),
                        x1=max(ln["x0"], ln["x1"]),
                        y1=max(ln["y0"], ln["y1"]),
                    ))
            elif ln["type"] == "line":
                dx = abs(ln["x1"] - ln["x0"])
                dy = abs(ln["y1"] - ln["y0"])
                if dy < 5 and dx > page_width * 0.15:
                    # Horizontal separator
                    y_mid = (ln["y0"] + ln["y1"]) / 2
                    h_separators.append(y_mid)
                elif dx < 5 and dy > page_height * 0.10:
                    # Vertical separator
                    x_mid = (ln["x0"] + ln["x1"]) / 2
                    v_separators.append(x_mid)

        # Deduplicate close separators
        h_separators = self._dedupe_positions(sorted(h_separators), threshold=15)
        v_separators = self._dedupe_positions(sorted(v_separators), threshold=15)

        zones: list[_Zone] = []

        # Rect-based zones (table cells, boxes)
        merged_rects = self._merge_overlapping_rects(rects)
        for r in merged_rects:
            zones.append(_Zone(bbox=r))

        # Separator-based zones: build a grid from h/v separators
        h_bands = [0.0] + h_separators + [page_height]
        v_bands = [0.0] + v_separators + [page_width]

        for hi in range(len(h_bands) - 1):
            for vi in range(len(v_bands) - 1):
                zone_bbox = BBox(
                    x0=v_bands[vi],
                    y0=h_bands[hi],
                    x1=v_bands[vi + 1],
                    y1=h_bands[hi + 1],
                )
                # Skip if too small or already covered by a rect zone
                if zone_bbox.width < 20 or zone_bbox.height < 20:
                    continue
                already_covered = any(
                    zone_bbox.overlap_ratio(z.bbox) > 0.7 for z in zones
                )
                if not already_covered:
                    zones.append(_Zone(bbox=zone_bbox))

        return zones

    @staticmethod
    def _dedupe_positions(positions: list[float], threshold: float = 15) -> list[float]:
        if not positions:
            return []
        result = [positions[0]]
        for p in positions[1:]:
            if p - result[-1] > threshold:
                result.append(p)
        return result

    @staticmethod
    def _merge_overlapping_rects(rects: list[BBox]) -> list[BBox]:
        """Merge rectangles that overlap significantly."""
        if not rects:
            return []
        merged: list[BBox] = []
        used = [False] * len(rects)
        for i, r in enumerate(rects):
            if used[i]:
                continue
            current = BBox(x0=r.x0, y0=r.y0, x1=r.x1, y1=r.y1)
            used[i] = True
            changed = True
            while changed:
                changed = False
                for j in range(len(rects)):
                    if used[j]:
                        continue
                    if current.overlap_ratio(rects[j]) > 0.3:
                        current = BBox(
                            x0=min(current.x0, rects[j].x0),
                            y0=min(current.y0, rects[j].y0),
                            x1=max(current.x1, rects[j].x1),
                            y1=max(current.y1, rects[j].y1),
                        )
                        used[j] = True
                        changed = True
            merged.append(current)
        return merged

    # ------------------------------------------------------------------
    # Column heuristic fallback (no structural lines)
    # ------------------------------------------------------------------

    def _column_heuristic_order(
        self,
        blocks: list[LayoutBlock],
        page_width: float,
        page_height: float,
    ) -> list[LayoutBlock]:
        """Heuristic reading order based on column detection (fallback)."""
        num_cols = self._detect_columns(blocks, page_width)

        if num_cols > 1:
            col_boundaries = self._compute_column_boundaries(blocks, num_cols, page_width)
            for block in blocks:
                if block.bbox:
                    block.column_index = self._assign_column(block.bbox, col_boundaries)
        else:
            for block in blocks:
                block.column_index = 0

        # Sort: column_index ASC, then y0 ASC
        full_width_blocks = []
        column_blocks: dict[int, list[LayoutBlock]] = {}

        for block in blocks:
            if block.bbox and block.bbox.width > page_width * 0.7:
                full_width_blocks.append(block)
            else:
                col = block.column_index
                column_blocks.setdefault(col, []).append(block)

        full_width_blocks.sort(key=lambda b: b.bbox.y0 if b.bbox else 0)
        for col in column_blocks:
            column_blocks[col].sort(key=lambda b: b.bbox.y0 if b.bbox else 0)

        col_indices = sorted(column_blocks.keys())

        if num_cols > 1:
            ordered = self._interleave_columns(
                full_width_blocks, column_blocks, col_indices
            )
        else:
            all_blocks = full_width_blocks + [
                b for col in col_indices for b in column_blocks[col]
            ]
            ordered = sorted(all_blocks, key=lambda b: b.bbox.y0 if b.bbox else 0)

        for i, block in enumerate(ordered):
            block.reading_order = i

        return ordered

    def _detect_columns(self, blocks: list[LayoutBlock], page_width: float) -> int:
        centers = []
        for b in blocks:
            if b.bbox and b.bbox.width < page_width * 0.7:
                centers.append(b.bbox.center_x)

        if len(centers) < 3:
            return 1

        mid = page_width / 2
        left_count = sum(1 for c in centers if c < mid * 0.85)
        right_count = sum(1 for c in centers if c > mid * 1.15)

        if left_count >= 2 and right_count >= 2:
            return 2

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
        result: list[LayoutBlock] = []
        fw_iter = iter(full_width)
        current_fw = next(fw_iter, None)

        col_iters = {}
        col_nexts: dict[int, LayoutBlock | None] = {}
        for col in col_indices:
            col_iters[col] = iter(column_blocks.get(col, []))
            col_nexts[col] = next(col_iters[col], None)

        while True:
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
                result.append(block)
                col_nexts[col] = next(col_iters[col], None)

        return result

    # ------------------------------------------------------------------
    # VLM-based refinement
    # ------------------------------------------------------------------

    def _is_complex_page(self, blocks: list[LayoutBlock]) -> bool:
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
1. Reading zones are defined by visible border lines (structural lines).
   Read each zone completely (top-to-bottom, left-to-right) before moving
   to the next zone.
2. For multi-column layouts separated by a vertical line: read the left
   column top-to-bottom first, then the right column.
3. For tables: read cell by cell following the border structure – finish
   one cell completely before moving to the next cell to the right, then
   the next row.
4. Full-width headings/paragraphs should be read at their y-position.
5. Footnotes (below a horizontal separator at page bottom) come after
   all body content.
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
        try:
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

            id_to_block = {b.id: b for b in blocks}
            ordered: list[LayoutBlock] = []
            seen = set()

            for bid in ordered_ids:
                if bid in id_to_block and bid not in seen:
                    ordered.append(id_to_block[bid])
                    seen.add(bid)

            for b in blocks:
                if b.id not in seen:
                    ordered.append(b)

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

    def _link_annotations(
        self,
        blocks: list[LayoutBlock],
        vector_lines: list[dict],
    ) -> list[LayoutBlock]:
        """Link footnotes, balloons, and arrow annotations to their anchors.

        Annotation lines (non-orthogonal / diagonal lines) are used to find
        the connection between an annotation block and the body block it
        points to.
        """
        annotations = [b for b in blocks if b.block_type in (
            BlockType.FOOTNOTE, BlockType.ENDNOTE,
            BlockType.BALLOON, BlockType.ARROW_ANNOTATION,
        )]
        body_blocks = [b for b in blocks if b.block_type not in (
            BlockType.FOOTNOTE, BlockType.ENDNOTE,
            BlockType.BALLOON, BlockType.ARROW_ANNOTATION,
            BlockType.HEADER, BlockType.FOOTER, BlockType.PAGE_NUMBER,
        )]

        # Collect annotation (diagonal) lines – these are pointers
        annotation_lines = [
            ln for ln in vector_lines
            if ln["type"] == "line" and ln.get("line_class") == "annotation"
        ]

        for ann in annotations:
            if ann.linked_block_ids:
                continue  # already linked

            if ann.bbox and annotation_lines:
                # Try to find an annotation line that starts/ends near this block
                linked = self._find_linked_block_via_line(
                    ann, annotation_lines, body_blocks,
                )
                if linked:
                    ann.linked_block_ids.append(linked.id)
                    continue

            # Fallback: nearest body block by distance
            if ann.bbox and body_blocks:
                nearest = min(
                    body_blocks,
                    key=lambda b: self._distance(ann.bbox, b.bbox) if b.bbox else float("inf"),
                )
                ann.linked_block_ids.append(nearest.id)

        return blocks

    def _find_linked_block_via_line(
        self,
        ann_block: LayoutBlock,
        annotation_lines: list[dict],
        body_blocks: list[LayoutBlock],
    ) -> LayoutBlock | None:
        """Find the body block linked to *ann_block* via an annotation line.

        An annotation line connects two regions.  One endpoint should be near
        the annotation block; the other endpoint should be near a body block.
        """
        if ann_block.bbox is None:
            return None

        for ln in annotation_lines:
            p1 = (ln["x0"], ln["y0"])
            p2 = (ln["x1"], ln["y1"])

            # Check if either endpoint is inside / near the annotation block
            near_ann = (
                self._point_near_bbox(p1, ann_block.bbox, margin=30)
                or self._point_near_bbox(p2, ann_block.bbox, margin=30)
            )
            if not near_ann:
                continue

            # The OTHER endpoint should be near a body block
            far_point = p2 if self._point_near_bbox(p1, ann_block.bbox, margin=30) else p1
            best_block: LayoutBlock | None = None
            best_dist = float("inf")
            for bb in body_blocks:
                if bb.bbox is None:
                    continue
                d = self._point_to_bbox_dist(far_point, bb.bbox)
                if d < best_dist:
                    best_dist = d
                    best_block = bb

            if best_block is not None and best_dist < 80:
                return best_block

        return None

    @staticmethod
    def _point_near_bbox(point: tuple[float, float], bbox: BBox, margin: float = 20) -> bool:
        px, py = point
        return (
            bbox.x0 - margin <= px <= bbox.x1 + margin
            and bbox.y0 - margin <= py <= bbox.y1 + margin
        )

    @staticmethod
    def _point_to_bbox_dist(point: tuple[float, float], bbox: BBox) -> float:
        px, py = point
        cx = max(bbox.x0, min(px, bbox.x1))
        cy = max(bbox.y0, min(py, bbox.y1))
        return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5

    def _distance(self, a: BBox, b: BBox) -> float:
        dx = a.center_x - b.center_x
        dy = a.center_y - b.center_y
        return (dx * dx + dy * dy) ** 0.5
