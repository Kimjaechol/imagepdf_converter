"""Block-level integrity: sequential numbering, caption linkage, reading order validation.

This module is the intra-page safety net.  While page-level ordering is
handled by pipeline.py's triple safety layer, this module ensures that
WITHIN each page, tables, figures, and their captions are:

1. Assigned deterministic, document-wide sequential numbers (Table 1, Figure 2, ...)
2. Linked to their captions by spatial proximity (bbox distance)
3. Placed in correct reading order (validated/corrected using bbox positions)
4. Given stable IDs that encode their page + position for traceability
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from backend.models.schema import (
    BBox,
    BlockType,
    LayoutBlock,
    PageResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assign_content_ids_and_seq(pages: list[PageResult]) -> list[PageResult]:
    """Assign deterministic IDs and document-wide sequential numbers.

    This is the main entry point.  Call after all AI processing is done
    and before rendering to HTML/Markdown.

    Steps:
      1. Validate & correct reading_order within each page (bbox-based)
      2. Assign document-wide sequential numbers to tables and figures
      3. Link captions to their nearest table/figure
      4. Assign stable, deterministic block IDs
    """
    # Step 1: Validate reading order on every page
    for page in pages:
        page.blocks = _validate_reading_order(page.blocks, page.width)

    # Step 2: Sequential numbering across entire document
    _assign_sequential_numbers(pages)

    # Step 3: Link captions to tables/figures
    for page in pages:
        _link_captions(page.blocks)

    # Step 4: Assign deterministic IDs
    for page in pages:
        _assign_deterministic_ids(page.blocks, page.page_index)

    return pages


# ---------------------------------------------------------------------------
# Step 1: Reading order validation
# ---------------------------------------------------------------------------

def _validate_reading_order(
    blocks: list[LayoutBlock],
    page_width: float,
) -> list[LayoutBlock]:
    """Validate and correct reading_order using bbox positions.

    AI may return incorrect reading_order values.  We use the physical
    positions (bbox) to detect and fix obvious errors:
    - A block physically above another should not have a higher reading_order
    - In multi-column layout, left column should come before right column

    Strategy: compute a "geometric reading order" from bbox positions,
    then compare with the AI's reading_order.  If they disagree on the
    relative position of a TABLE or FIGURE block, trust the geometric order.
    """
    if not blocks:
        return blocks

    # Separate blocks that have bbox vs those without
    positioned = [(i, b) for i, b in enumerate(blocks) if b.bbox]
    unpositioned = [(i, b) for i, b in enumerate(blocks) if not b.bbox]

    if not positioned:
        return blocks

    # Detect columns: split at page midpoint
    mid_x = page_width / 2 if page_width > 0 else 1000
    COLUMN_THRESHOLD = 0.4  # block's center must be clearly in one half

    def _geo_sort_key(block: LayoutBlock) -> tuple[int, float, float]:
        """Sort key: (column_index, y_center, x_center)."""
        bbox = block.bbox
        assert bbox is not None
        cx = bbox.center_x
        cy = bbox.center_y

        # Column detection: blocks clearly in left half vs right half
        if cx < mid_x * COLUMN_THRESHOLD * 2:
            col = 0  # left column
        elif cx > mid_x + (mid_x * (1 - COLUMN_THRESHOLD)):
            col = 1  # right column
        else:
            col = 0  # spans both or centered → treat as left/full-width

        # Full-width blocks (>60% of page width) → column 0
        if bbox.width > page_width * 0.6:
            col = 0

        return (col, cy, cx)

    # Sort by geometric position
    geo_sorted = sorted(positioned, key=lambda t: _geo_sort_key(t[1]))

    # Build mapping: original index → geometric order
    geo_order = {}
    for geo_rank, (orig_idx, block) in enumerate(geo_sorted):
        geo_order[orig_idx] = geo_rank

    # Now check if AI's reading_order is consistent with geometric order
    # for critical blocks (TABLE, FIGURE, EQUATION).
    # If AI order disagrees for these blocks, trust geometric order.
    critical_types = {BlockType.TABLE, BlockType.FIGURE, BlockType.EQUATION}

    ai_ordered = sorted(positioned, key=lambda t: t[1].reading_order)
    ai_order = {}
    for ai_rank, (orig_idx, block) in enumerate(ai_ordered):
        ai_order[orig_idx] = ai_rank

    # Detect disagreements for critical blocks
    disagreements = 0
    for orig_idx, block in positioned:
        if block.block_type not in critical_types:
            continue
        ai_rank = ai_order.get(orig_idx, 0)
        geo_rank = geo_order.get(orig_idx, 0)
        # Check neighbors: is this block's AI position far from its geo position?
        if abs(ai_rank - geo_rank) > 2:
            disagreements += 1

    # If >30% of critical blocks disagree, AI order is unreliable → use geometric
    total_critical = sum(
        1 for _, b in positioned if b.block_type in critical_types
    )
    use_geometric = (
        disagreements > 0
        and total_critical > 0
        and disagreements / total_critical > 0.3
    )

    if use_geometric:
        logger.warning(
            "Reading order disagreement: %d/%d critical blocks misplaced. "
            "Falling back to geometric (bbox-based) reading order.",
            disagreements, total_critical,
        )
        # Apply geometric order to ALL blocks
        for geo_rank, (orig_idx, block) in enumerate(geo_sorted):
            block.reading_order = geo_rank
    else:
        # AI order looks reasonable, but fix any critical block that is
        # wildly out of place (>3 positions off from geometric)
        for orig_idx, block in positioned:
            if block.block_type not in critical_types:
                continue
            ai_rank = ai_order.get(orig_idx, 0)
            geo_rank = geo_order.get(orig_idx, 0)
            if abs(ai_rank - geo_rank) > 3:
                logger.warning(
                    "Block %s (type=%s, page=%d) reading_order=%d but "
                    "geometric position=%d. Correcting to geometric.",
                    block.id, block.block_type.value,
                    block.page_index, block.reading_order, geo_rank,
                )
                block.reading_order = geo_rank

    # Assign reading_order to unpositioned blocks (append at end)
    max_order = max((b.reading_order for _, b in positioned), default=-1)
    for _, block in unpositioned:
        max_order += 1
        block.reading_order = max_order

    # Re-sort blocks by final reading_order
    blocks.sort(key=lambda b: b.reading_order)

    # Re-number reading_order to be contiguous 0..N-1
    for i, block in enumerate(blocks):
        block.reading_order = i

    return blocks


# ---------------------------------------------------------------------------
# Step 2: Document-wide sequential numbering
# ---------------------------------------------------------------------------

def _assign_sequential_numbers(pages: list[PageResult]) -> None:
    """Assign document-wide sequential numbers to tables and figures.

    Traverses all pages in order, incrementing counters:
      - Tables: Table 1, Table 2, ...
      - Figures/Equations: Figure 1, Figure 2, ...
    """
    table_seq = 0
    figure_seq = 0

    for page in pages:
        for block in page.blocks:
            if block.block_type == BlockType.TABLE:
                table_seq += 1
                block.content_seq = table_seq
            elif block.block_type in (BlockType.FIGURE, BlockType.EQUATION):
                figure_seq += 1
                block.content_seq = figure_seq

    logger.info(
        "Sequential numbering: %d tables, %d figures/equations",
        table_seq, figure_seq,
    )


# ---------------------------------------------------------------------------
# Step 3: Caption ↔ table/figure linkage
# ---------------------------------------------------------------------------

def _link_captions(blocks: list[LayoutBlock]) -> None:
    """Link each CAPTION block to its nearest TABLE or FIGURE by bbox proximity.

    Rules:
      1. A caption links to the nearest table/figure (by bbox edge distance)
      2. Maximum distance: 80px (if no table/figure is within range, skip)
      3. Each table/figure can have at most one caption
      4. Captions above link to the block below; captions below link to the block above
    """
    MAX_DISTANCE = 80.0  # px

    # Collect candidates
    captions = [b for b in blocks if b.block_type == BlockType.CAPTION]
    targets = [
        b for b in blocks
        if b.block_type in (BlockType.TABLE, BlockType.FIGURE, BlockType.EQUATION)
    ]

    if not captions or not targets:
        return

    # Track which targets already have a caption
    claimed: set[str] = set()

    for cap in captions:
        if not cap.bbox:
            continue

        best_target = None
        best_dist = MAX_DISTANCE

        for tgt in targets:
            if tgt.id in claimed:
                continue
            if not tgt.bbox:
                continue

            dist = _bbox_edge_distance(cap.bbox, tgt.bbox)
            if dist < best_dist:
                best_dist = dist
                best_target = tgt

        if best_target:
            cap.parent_block_id = best_target.id
            best_target.caption = cap.text
            claimed.add(best_target.id)
            logger.debug(
                "Linked caption '%s' → %s %s (dist=%.0fpx)",
                cap.text[:30], best_target.block_type.value,
                best_target.id, best_dist,
            )


def _bbox_edge_distance(a: BBox, b: BBox) -> float:
    """Minimum distance between edges of two bboxes (0 if overlapping)."""
    # Horizontal gap
    if a.x1 < b.x0:
        dx = b.x0 - a.x1
    elif b.x1 < a.x0:
        dx = a.x0 - b.x1
    else:
        dx = 0

    # Vertical gap
    if a.y1 < b.y0:
        dy = b.y0 - a.y1
    elif b.y1 < a.y0:
        dy = a.y0 - b.y1
    else:
        dy = 0

    return math.sqrt(dx * dx + dy * dy)


# ---------------------------------------------------------------------------
# Step 4: Deterministic block IDs
# ---------------------------------------------------------------------------

def _assign_deterministic_ids(
    blocks: list[LayoutBlock],
    page_index: int,
) -> None:
    """Assign stable, deterministic IDs that encode position.

    Format: p{page}_{type}_{seq}_{y}_{x}
    Examples:
      p5_table_1_320_100    → page 5, 1st table, top-left at (100, 320)
      p5_para_3_500_100     → page 5, 3rd paragraph, at y=500
      p5_figure_1_800_200   → page 5, 1st figure, at y=800

    The y_x suffix acts as a position anchor — if a block claims to be
    at reading_order=2 but its ID says y=800 while reading_order=1 is
    at y=200, the inconsistency is immediately visible.
    """
    type_counters: dict[str, int] = {}

    for block in blocks:
        type_key = block.block_type.value
        type_counters[type_key] = type_counters.get(type_key, 0) + 1
        seq = type_counters[type_key]

        if block.bbox:
            y = int(block.bbox.y0)
            x = int(block.bbox.x0)
            block.id = f"p{page_index}_{type_key}_{seq}_{y}_{x}"
        else:
            block.id = f"p{page_index}_{type_key}_{seq}_nobb"
