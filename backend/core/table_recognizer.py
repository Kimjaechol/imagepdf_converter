"""Table structure recognition – detect cells, rows, columns, spans."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

from backend.models.schema import BBox, BlockType, LayoutBlock, TableCell, TableStructure

logger = logging.getLogger(__name__)


class TableRecognizer:
    """Recognize internal table structure (cells, spans, headers)."""

    def __init__(self, engine: str = "table_transformer"):
        self.engine = engine
        self._model = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def recognize(
        self,
        image_path: str,
        table_block: LayoutBlock,
    ) -> TableStructure:
        """Recognize the structure of a single table.

        *table_block* must have block_type == TABLE and a valid bbox.
        Returns a TableStructure with cells populated.
        """
        if table_block.bbox is None:
            return TableStructure()

        img = Image.open(image_path).convert("RGB")
        # Crop table region
        table_img = img.crop((
            int(table_block.bbox.x0),
            int(table_block.bbox.y0),
            int(table_block.bbox.x1),
            int(table_block.bbox.y1),
        ))

        if self.engine == "table_transformer":
            return self._recognize_table_transformer(table_img, table_block)
        else:
            return self._recognize_rule_based(table_img, table_block)

    def recognize_all(
        self,
        image_path: str,
        blocks: list[LayoutBlock],
    ) -> list[LayoutBlock]:
        """Recognize table structures for all TABLE blocks in *blocks*."""
        for block in blocks:
            if block.block_type == BlockType.TABLE:
                block.table_structure = self.recognize(image_path, block)
        return blocks

    def merge_multipage_tables(
        self,
        pages: list[dict],
    ) -> list[dict]:
        """Detect and merge tables that span across page boundaries.

        Heuristic: if the last block on page N is a TABLE and the first block
        on page N+1 is also a TABLE with similar column structure, merge them.
        """
        for i in range(len(pages) - 1):
            curr_blocks = pages[i].get("blocks", [])
            next_blocks = pages[i + 1].get("blocks", [])

            if not curr_blocks or not next_blocks:
                continue

            last_curr = curr_blocks[-1] if curr_blocks else None
            first_next = next_blocks[0] if next_blocks else None

            if (
                last_curr
                and first_next
                and last_curr.block_type == BlockType.TABLE
                and first_next.block_type == BlockType.TABLE
                and last_curr.table_structure
                and first_next.table_structure
            ):
                if self._tables_are_continuations(
                    last_curr.table_structure, first_next.table_structure
                ):
                    merged = self._merge_two_tables(
                        last_curr.table_structure, first_next.table_structure
                    )
                    last_curr.table_structure = merged
                    # Mark second table as merged (to be removed later)
                    first_next.block_type = BlockType.UNKNOWN
                    first_next.text = ""

        return pages

    # ------------------------------------------------------------------
    # Table Transformer (Microsoft)
    # ------------------------------------------------------------------

    def _recognize_table_transformer(
        self,
        table_img: Image.Image,
        table_block: LayoutBlock,
    ) -> TableStructure:
        try:
            import torch
            from torchvision import transforms
            from transformers import (
                AutoModelForObjectDetection,
                TableTransformerForObjectDetection,
            )

            if self._model is None:
                self._model = {
                    "structure": TableTransformerForObjectDetection.from_pretrained(
                        "microsoft/table-transformer-structure-recognition"
                    ),
                }
                self._model["structure"].eval()

            transform = transforms.Compose([
                transforms.Resize((800, 800)),
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ])

            pixel_values = transform(table_img).unsqueeze(0)
            with torch.no_grad():
                outputs = self._model["structure"](pixel_values)

            # Process detections into cells
            return self._process_structure_output(
                outputs, table_img.size, table_block
            )

        except ImportError:
            logger.warning("table-transformer not installed, using rule-based.")
            return self._recognize_rule_based(table_img, table_block)
        except Exception as exc:
            logger.error("Table transformer failed: %s", exc)
            return self._recognize_rule_based(table_img, table_block)

    def _process_structure_output(
        self,
        outputs,
        img_size: tuple[int, int],
        table_block: LayoutBlock,
    ) -> TableStructure:
        """Convert model outputs to TableStructure."""
        import torch

        w, h = img_size
        probas = outputs.logits.softmax(-1)[0]
        keep = probas.max(-1).values > 0.5
        boxes = outputs.pred_boxes[0][keep]
        labels = probas[keep].argmax(-1)

        rows: list[BBox] = []
        cols: list[BBox] = []
        cells: list[BBox] = []

        id2label = {
            0: "table",
            1: "table column",
            2: "table row",
            3: "table column header",
            4: "table projected row header",
            5: "table spanning cell",
        }

        for box, label_id in zip(boxes, labels):
            cx, cy, bw, bh = box.tolist()
            x0 = (cx - bw / 2) * w
            y0 = (cy - bh / 2) * h
            x1 = (cx + bw / 2) * w
            y1 = (cy + bh / 2) * h
            bbox = BBox(x0=x0, y0=y0, x1=x1, y1=y1)
            label = id2label.get(label_id.item(), "unknown")

            if label == "table row":
                rows.append(bbox)
            elif label in ("table column", "table column header"):
                cols.append(bbox)

        # Sort rows top to bottom, columns left to right
        rows.sort(key=lambda b: b.y0)
        cols.sort(key=lambda b: b.x0)

        if not rows or not cols:
            return self._recognize_rule_based_from_grid(len(rows) or 1, len(cols) or 1, table_block)

        # Build cell grid
        table_cells: list[TableCell] = []
        for ri, row in enumerate(rows):
            for ci, col in enumerate(cols):
                cell_bbox = BBox(
                    x0=col.x0, y0=row.y0,
                    x1=col.x1, y1=row.y1,
                )
                table_cells.append(TableCell(
                    row=ri,
                    col=ci,
                    bbox=cell_bbox,
                    is_header=(ri == 0),
                ))

        return TableStructure(
            num_rows=len(rows),
            num_cols=len(cols),
            cells=table_cells,
            has_visible_borders=True,
            bbox=table_block.bbox,
        )

    # ------------------------------------------------------------------
    # Rule-based fallback
    # ------------------------------------------------------------------

    def _recognize_rule_based(
        self,
        table_img: Image.Image,
        table_block: LayoutBlock,
    ) -> TableStructure:
        """Simple heuristic: split by detected horizontal / vertical lines."""
        import cv2

        img_array = np.array(table_img)
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

        h, w = binary.shape

        # Detect horizontal lines
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (w // 4, 1))
        h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)

        # Detect vertical lines
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, h // 4))
        v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)

        # Find line positions
        h_positions = self._find_line_positions(h_lines, axis=0)
        v_positions = self._find_line_positions(v_lines, axis=1)

        if len(h_positions) < 2 or len(v_positions) < 2:
            return self._recognize_rule_based_from_grid(3, 3, table_block)

        # Build cells
        cells: list[TableCell] = []
        for ri in range(len(h_positions) - 1):
            for ci in range(len(v_positions) - 1):
                cells.append(TableCell(
                    row=ri,
                    col=ci,
                    bbox=BBox(
                        x0=float(v_positions[ci]),
                        y0=float(h_positions[ri]),
                        x1=float(v_positions[ci + 1]),
                        y1=float(h_positions[ri + 1]),
                    ),
                    is_header=(ri == 0),
                ))

        return TableStructure(
            num_rows=len(h_positions) - 1,
            num_cols=len(v_positions) - 1,
            cells=cells,
            has_visible_borders=True,
            bbox=table_block.bbox,
        )

    def _recognize_rule_based_from_grid(
        self, num_rows: int, num_cols: int, table_block: LayoutBlock
    ) -> TableStructure:
        if table_block.bbox is None:
            return TableStructure()
        w = table_block.bbox.width / max(num_cols, 1)
        h = table_block.bbox.height / max(num_rows, 1)
        cells = []
        for r in range(num_rows):
            for c in range(num_cols):
                cells.append(TableCell(
                    row=r, col=c,
                    bbox=BBox(
                        x0=table_block.bbox.x0 + c * w,
                        y0=table_block.bbox.y0 + r * h,
                        x1=table_block.bbox.x0 + (c + 1) * w,
                        y1=table_block.bbox.y0 + (r + 1) * h,
                    ),
                    is_header=(r == 0),
                ))
        return TableStructure(
            num_rows=num_rows, num_cols=num_cols, cells=cells,
            has_visible_borders=False, bbox=table_block.bbox,
        )

    def _find_line_positions(self, mask: np.ndarray, axis: int) -> list[int]:
        projection = np.sum(mask, axis=axis)
        threshold = projection.max() * 0.3
        positions = []
        in_line = False
        start = 0
        for i, val in enumerate(projection):
            if val > threshold and not in_line:
                in_line = True
                start = i
            elif val <= threshold and in_line:
                positions.append((start + i) // 2)
                in_line = False
        if in_line:
            positions.append((start + len(projection) - 1) // 2)
        return positions

    # ------------------------------------------------------------------
    # Multi-page table merging helpers
    # ------------------------------------------------------------------

    def _tables_are_continuations(
        self, t1: TableStructure, t2: TableStructure
    ) -> bool:
        """Check if t2 is a continuation of t1 (same column structure)."""
        if t1.num_cols != t2.num_cols:
            return False
        # Check if header of t2 matches header of t1
        h1_texts = [c.text.strip() for c in t1.cells if c.is_header]
        h2_texts = [c.text.strip() for c in t2.cells if c.is_header]
        if not h1_texts or not h2_texts:
            return t1.num_cols == t2.num_cols
        matches = sum(1 for a, b in zip(h1_texts, h2_texts) if a == b)
        return matches / max(len(h1_texts), 1) > 0.7

    def _merge_two_tables(
        self, t1: TableStructure, t2: TableStructure
    ) -> TableStructure:
        """Merge t2 rows into t1, skipping t2's header if identical."""
        offset = t1.num_rows
        non_header_cells = [c for c in t2.cells if not c.is_header]
        for cell in non_header_cells:
            cell.row += offset
            t1.cells.append(cell)
        t1.num_rows += t2.num_rows - (1 if any(c.is_header for c in t2.cells) else 0)
        return t1
