"""Layout analysis – detect blocks (heading, paragraph, table, figure, …)."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

from backend.models.schema import BBox, BlockType, LayoutBlock

logger = logging.getLogger(__name__)

# Map category names coming from various models to our BlockType enum.
_CATEGORY_MAP: dict[str, BlockType] = {
    "title": BlockType.HEADING,
    "section-header": BlockType.HEADING,
    "heading": BlockType.HEADING,
    "text": BlockType.PARAGRAPH,
    "paragraph": BlockType.PARAGRAPH,
    "plain text": BlockType.PARAGRAPH,
    "table": BlockType.TABLE,
    "figure": BlockType.FIGURE,
    "image": BlockType.FIGURE,
    "picture": BlockType.FIGURE,
    "equation": BlockType.EQUATION,
    "formula": BlockType.EQUATION,
    "list": BlockType.LIST,
    "list-item": BlockType.LIST,
    "caption": BlockType.CAPTION,
    "footnote": BlockType.FOOTNOTE,
    "header": BlockType.HEADER,
    "footer": BlockType.FOOTER,
    "page-number": BlockType.PAGE_NUMBER,
    "page-footer": BlockType.FOOTER,
    "page-header": BlockType.HEADER,
}


class LayoutDetector:
    """Detect document layout regions using Surya or rule-based fallback."""

    def __init__(self, engine: str = "surya", confidence_threshold: float = 0.5):
        self.engine = engine
        self.confidence_threshold = confidence_threshold
        self._surya_model = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def detect(
        self,
        image_path: str,
        page_index: int,
        existing_digital_blocks: list[LayoutBlock] | None = None,
        vector_lines: list[dict] | None = None,
    ) -> list[LayoutBlock]:
        """Run layout detection on a page image.

        If *existing_digital_blocks* are provided (from a digital PDF layer),
        they are used as a fallback / enrichment source.
        """
        if self.engine == "surya":
            blocks = self._detect_surya(image_path, page_index)
        else:
            blocks = []

        # If the model returned nothing usable, fall back to digital blocks
        if not blocks and existing_digital_blocks:
            blocks = list(existing_digital_blocks)

        # Enrich: if we have vector lines, try to identify table regions
        if vector_lines:
            table_regions = self._detect_tables_from_lines(vector_lines, page_index)
            blocks = self._merge_table_regions(blocks, table_regions)

        # Filter low confidence
        blocks = [b for b in blocks if b.confidence >= self.confidence_threshold]

        return blocks

    # ------------------------------------------------------------------
    # Surya layout engine
    # ------------------------------------------------------------------

    def _detect_surya(self, image_path: str, page_index: int) -> list[LayoutBlock]:
        """Use Surya layout model."""
        try:
            from surya.detection import batch_text_detection
            from surya.layout import batch_layout_detection
            from surya.model.detection.model import load_model as load_det_model
            from surya.model.detection.model import load_processor as load_det_proc

            if self._surya_model is None:
                self._surya_model = {
                    "det_model": load_det_model(),
                    "det_proc": load_det_proc(),
                }

            img = Image.open(image_path).convert("RGB")
            det_results = batch_text_detection(
                [img],
                self._surya_model["det_model"],
                self._surya_model["det_proc"],
            )
            layout_results = batch_layout_detection(
                [img],
                self._surya_model["det_model"],
                self._surya_model["det_proc"],
                det_results,
            )

            blocks: list[LayoutBlock] = []
            if layout_results:
                for bbox_obj in layout_results[0].bboxes:
                    raw_label = getattr(bbox_obj, "label", "text").lower()
                    block_type = _CATEGORY_MAP.get(raw_label, BlockType.UNKNOWN)
                    conf = getattr(bbox_obj, "confidence", 0.0)
                    b = getattr(bbox_obj, "bbox", [0, 0, 0, 0])
                    blocks.append(LayoutBlock(
                        id=f"lay_{page_index}_{uuid.uuid4().hex[:8]}",
                        block_type=block_type,
                        bbox=BBox(x0=b[0], y0=b[1], x1=b[2], y1=b[3]),
                        confidence=conf,
                        page_index=page_index,
                    ))
            return blocks

        except ImportError:
            logger.warning("Surya not installed, falling back to rule-based layout.")
            return []
        except Exception as exc:
            logger.error("Surya layout detection failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Rule-based table detection from vector lines
    # ------------------------------------------------------------------

    def _detect_tables_from_lines(
        self,
        lines: list[dict],
        page_index: int,
    ) -> list[LayoutBlock]:
        """Detect potential table regions from horizontal/vertical lines."""
        h_lines: list[dict] = []
        v_lines: list[dict] = []

        for ln in lines:
            if ln["type"] == "line":
                dx = abs(ln["x1"] - ln["x0"])
                dy = abs(ln["y1"] - ln["y0"])
                if dy < 3 and dx > 20:  # horizontal
                    h_lines.append(ln)
                elif dx < 3 and dy > 20:  # vertical
                    v_lines.append(ln)
            elif ln["type"] == "rect":
                # Rectangles indicate table or box regions
                pass

        if len(h_lines) < 2 or len(v_lines) < 2:
            return []

        # Cluster lines into potential table regions
        all_points = []
        for ln in h_lines + v_lines:
            all_points.extend([(ln["x0"], ln["y0"]), (ln["x1"], ln["y1"])])

        if not all_points:
            return []

        pts = np.array(all_points)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)

        return [LayoutBlock(
            id=f"tbl_vec_{page_index}_{uuid.uuid4().hex[:8]}",
            block_type=BlockType.TABLE,
            bbox=BBox(x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1)),
            confidence=0.7,
            page_index=page_index,
        )]

    def _merge_table_regions(
        self,
        blocks: list[LayoutBlock],
        table_regions: list[LayoutBlock],
    ) -> list[LayoutBlock]:
        """Merge vector-detected table regions with model-detected blocks."""
        for tr in table_regions:
            # Check if any existing block already covers this table region
            already_covered = False
            for blk in blocks:
                if blk.block_type == BlockType.TABLE and blk.bbox and tr.bbox:
                    if blk.bbox.overlap_ratio(tr.bbox) > 0.5:
                        already_covered = True
                        break
            if not already_covered:
                blocks.append(tr)
        return blocks
