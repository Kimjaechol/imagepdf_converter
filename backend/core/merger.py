"""Merge chunk results back into a unified document."""

from __future__ import annotations

import logging

from backend.models.schema import (
    BlockType,
    ChunkResult,
    DocumentResult,
    PageResult,
    TableStructure,
)

logger = logging.getLogger(__name__)


class ChunkMerger:
    """Merge parallel-processed chunk results into a single document."""

    def __init__(self, merge_multipage_tables: bool = True):
        self.merge_multipage_tables = merge_multipage_tables

    def merge(self, chunks: list[ChunkResult]) -> list[PageResult]:
        """Merge chunks in order, handling cross-chunk table continuations."""
        # Sort by chunk index
        chunks.sort(key=lambda c: c.chunk_index)

        all_pages: list[PageResult] = []
        for chunk in chunks:
            all_pages.extend(chunk.pages)

        # Sort pages by page_index
        all_pages.sort(key=lambda p: p.page_index)

        # Merge multi-page tables across chunk boundaries
        if self.merge_multipage_tables:
            all_pages = self._merge_cross_page_tables(all_pages)

        return all_pages

    def _merge_cross_page_tables(self, pages: list[PageResult]) -> list[PageResult]:
        """Detect and merge tables that span page boundaries."""
        for i in range(len(pages) - 1):
            curr_page = pages[i]
            next_page = pages[i + 1]

            if not curr_page.blocks or not next_page.blocks:
                continue

            # Find last table on current page
            last_table = None
            last_table_idx = -1
            for idx, b in enumerate(curr_page.blocks):
                if b.block_type == BlockType.TABLE and b.table_structure:
                    last_table = b
                    last_table_idx = idx

            # Find first table on next page
            first_table = None
            first_table_idx = -1
            for idx, b in enumerate(next_page.blocks):
                if b.block_type == BlockType.TABLE and b.table_structure:
                    first_table = b
                    first_table_idx = idx
                    break

            if (
                last_table
                and first_table
                and last_table.table_structure
                and first_table.table_structure
            ):
                if self._should_merge(
                    last_table.table_structure, first_table.table_structure
                ):
                    self._do_merge(
                        last_table.table_structure, first_table.table_structure
                    )
                    # Remove the merged table from next page
                    next_page.blocks.pop(first_table_idx)
                    logger.info(
                        "Merged table across pages %d and %d",
                        curr_page.page_index,
                        next_page.page_index,
                    )

        return pages

    def _should_merge(self, t1: TableStructure, t2: TableStructure) -> bool:
        """Check if two tables should be merged (same column count + similar headers)."""
        if t1.num_cols != t2.num_cols:
            return False

        # Check position: last table should be at bottom of page,
        # first table should be at top of next page
        if t1.bbox and t2.bbox:
            # t2 should be near the top of its page
            if t2.bbox.y0 > 200:  # More than 200px from top → probably not a continuation
                return False

        # Compare header texts
        h1 = sorted(
            [(c.col, c.text.strip()) for c in t1.cells if c.is_header],
            key=lambda x: x[0],
        )
        h2 = sorted(
            [(c.col, c.text.strip()) for c in t2.cells if c.is_header],
            key=lambda x: x[0],
        )

        if h1 and h2:
            # If headers match, it's likely a continuation
            h1_texts = [t for _, t in h1]
            h2_texts = [t for _, t in h2]
            matches = sum(1 for a, b in zip(h1_texts, h2_texts) if a == b)
            return matches / max(len(h1_texts), 1) > 0.7

        # If no headers to compare, use column count as heuristic
        return True

    def _do_merge(self, t1: TableStructure, t2: TableStructure) -> None:
        """Append t2's non-header rows into t1."""
        offset = t1.num_rows
        has_header = any(c.is_header for c in t2.cells)
        for cell in t2.cells:
            if cell.is_header and has_header:
                continue
            cell.row += offset
            t1.cells.append(cell)
        added_rows = t2.num_rows - (1 if has_header else 0)
        t1.num_rows += added_rows
