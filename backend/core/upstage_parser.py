"""Upstage Document Parse client – OCR/layout/table extraction via API.

Handles both digital and scanned (image) PDFs. Returns structured LayoutBlock
objects that can be fed into the Gemini refinement stage.

API: https://api.upstage.ai/v1/document-digitization
Docs: https://console.upstage.ai/api/parse/document-parsing
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
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

_UPSTAGE_API_URL = "https://api.upstage.ai/v1/document-digitization"

# Maximum pages per single Upstage API call.
# Upstage Document Parse handles multi-page PDFs but large docs should be chunked.
_MAX_PAGES_PER_CALL = 50


@dataclass
class UpstageParseConfig:
    """Configuration for Upstage Document Parse calls."""
    # "auto" | "standard" | "enhanced"
    # auto: Upstage decides optimal mode per page
    # standard: faster, cheaper – good for digital PDFs
    # enhanced: slower, more accurate – good for scanned/complex PDFs
    mode: str = "auto"
    # Whether to force enhanced mode for scanned pages
    force_enhanced_for_scanned: bool = True
    # Max concurrent API calls
    max_workers: int = 4
    # Retry settings
    max_retries: int = 3
    retry_delay: float = 2.0


class UpstageDocumentParser:
    """Parse PDF documents via Upstage Document Parse API."""

    def __init__(self, config: UpstageParseConfig | None = None):
        self.config = config or UpstageParseConfig()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def parse_pdf(
        self,
        pdf_path: Path,
        progress_callback: Any = None,
    ) -> list[PageResult]:
        """Parse entire PDF via Upstage Document Parse.

        1. Detect page count and determine if digital or scanned.
        2. Split into chunks of _MAX_PAGES_PER_CALL pages.
        3. Send each chunk to Upstage API in parallel.
        4. Parse responses into PageResult objects.

        Returns list of PageResult sorted by page_index.
        """
        api_key = os.environ.get("UPSTAGE_API_KEY", "")
        if not api_key:
            logger.warning("UPSTAGE_API_KEY not set; Upstage Document Parse unavailable.")
            return []

        import fitz
        doc = fitz.open(str(pdf_path))
        total_pages = len(doc)

        # Classify: digital vs scanned (check first few pages for text)
        is_scanned = self._detect_scanned(doc)
        doc.close()

        # Determine mode
        mode = self.config.mode
        if mode == "auto" and is_scanned and self.config.force_enhanced_for_scanned:
            mode = "enhanced"
            logger.info("Scanned PDF detected, using enhanced mode for Upstage Document Parse")

        logger.info(
            "Upstage Document Parse: %d pages, mode=%s, is_scanned=%s",
            total_pages, mode, is_scanned,
        )

        # Split into chunks for parallel processing
        chunks = self._split_pdf_for_upload(pdf_path, total_pages)

        if progress_callback:
            progress_callback("Sending to Upstage Document Parse", 0.15)

        # Process chunks in parallel
        all_results: list[PageResult] = []
        completed = 0

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            futures = {}
            for chunk_path, start_page, end_page in chunks:
                future = executor.submit(
                    self._call_upstage_api,
                    chunk_path, api_key, mode, start_page,
                )
                futures[future] = (start_page, end_page)

            for future in as_completed(futures):
                start_page, end_page = futures[future]
                try:
                    chunk_results = future.result()
                    all_results.extend(chunk_results)
                except Exception as exc:
                    logger.error(
                        "Upstage API failed for pages %d-%d: %s",
                        start_page, end_page, exc,
                    )
                completed += 1
                if progress_callback:
                    pct = 0.15 + 0.35 * (completed / max(len(chunks), 1))
                    progress_callback(
                        f"Upstage chunk {completed}/{len(chunks)} done", pct,
                    )

        # Sort by page_index
        all_results.sort(key=lambda pr: pr.page_index)

        logger.info(
            "Upstage Document Parse complete: %d/%d pages parsed",
            len(all_results), total_pages,
        )

        return all_results

    # ------------------------------------------------------------------
    # API call
    # ------------------------------------------------------------------

    def _call_upstage_api(
        self,
        pdf_chunk_path: Path,
        api_key: str,
        mode: str,
        page_offset: int,
    ) -> list[PageResult]:
        """Call Upstage Document Parse API for a single PDF chunk.

        Returns list of PageResult with page_index adjusted by page_offset.
        """
        import httpx

        for attempt in range(self.config.max_retries):
            try:
                with open(pdf_chunk_path, "rb") as f:
                    response = httpx.post(
                        _UPSTAGE_API_URL,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                        },
                        files={
                            "document": (pdf_chunk_path.name, f, "application/pdf"),
                        },
                        data={
                            "model": "document-parse",
                            "ocr": "force" if mode == "enhanced" else "auto",
                            "mode": mode if mode in ("standard", "enhanced", "auto") else "auto",
                            "output_formats": "['html']",
                            "coordinates": "true",
                        },
                        timeout=300,  # 5 min per chunk
                    )

                if response.status_code == 429:
                    # Rate limited – wait and retry
                    wait = self.config.retry_delay * (2 ** attempt)
                    logger.warning(
                        "Upstage rate limited, retrying in %.1fs", wait,
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                data = response.json()
                return self._parse_upstage_response(data, page_offset)

            except httpx.TimeoutException:
                logger.warning(
                    "Upstage API timeout (attempt %d/%d)",
                    attempt + 1, self.config.max_retries,
                )
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay * (2 ** attempt))
            except httpx.HTTPStatusError as exc:
                logger.error("Upstage API HTTP error: %s", exc)
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay * (2 ** attempt))
                else:
                    raise
            except Exception as exc:
                logger.error("Upstage API call failed: %s", exc)
                raise

        return []

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_upstage_response(
        self,
        data: dict,
        page_offset: int,
    ) -> list[PageResult]:
        """Parse Upstage Document Parse API response into PageResult objects.

        Upstage response structure:
        {
            "api": "...",
            "model": "...",
            "content": {
                "html": "...",
                "markdown": "...",
            },
            "elements": [
                {
                    "id": 0,
                    "type": "paragraph|heading1|heading2|table|figure|...",
                    "content": {
                        "html": "<p>text</p>",
                        "markdown": "text",
                        "text": "plain text",
                    },
                    "page": 1,
                    "coordinates": [
                        {"x": 0.1, "y": 0.1},
                        {"x": 0.9, "y": 0.1},
                        {"x": 0.9, "y": 0.2},
                        {"x": 0.1, "y": 0.2},
                    ],
                    "category": "paragraph",
                    "base_html": "...",
                }
            ],
            "usage": {...}
        }
        """
        elements = data.get("elements", [])
        if not elements:
            logger.warning("Upstage response has no elements")
            return []

        # Group elements by page
        pages_map: dict[int, list[dict]] = {}
        for elem in elements:
            page_num = elem.get("page", 1)
            # Upstage uses 1-based page numbers
            page_idx = page_num - 1 + page_offset
            pages_map.setdefault(page_idx, []).append(elem)

        results: list[PageResult] = []
        for page_idx in sorted(pages_map.keys()):
            page_elements = pages_map[page_idx]
            blocks = self._convert_elements_to_blocks(page_elements, page_idx)

            # Estimate page dimensions from element coordinates
            # Upstage returns normalized coordinates [0-1], we'll use pixel estimates
            width = 2480.0  # A4 at 300 DPI
            height = 3508.0

            results.append(PageResult(
                page_index=page_idx,
                width=width,
                height=height,
                blocks=blocks,
            ))

        return results

    def _convert_elements_to_blocks(
        self,
        elements: list[dict],
        page_idx: int,
    ) -> list[LayoutBlock]:
        """Convert Upstage elements to LayoutBlock objects."""
        blocks: list[LayoutBlock] = []

        # Upstage category → our BlockType mapping
        category_map = {
            "paragraph": BlockType.PARAGRAPH,
            "heading1": BlockType.HEADING,
            "heading2": BlockType.HEADING,
            "heading3": BlockType.HEADING,
            "table": BlockType.TABLE,
            "figure": BlockType.FIGURE,
            "chart": BlockType.FIGURE,
            "image": BlockType.FIGURE,
            "equation": BlockType.EQUATION,
            "list": BlockType.LIST,
            "caption": BlockType.CAPTION,
            "footnote": BlockType.FOOTNOTE,
            "header": BlockType.HEADER,
            "footer": BlockType.FOOTER,
            "page_number": BlockType.PAGE_NUMBER,
            "table_of_contents": BlockType.LIST,
            "index": BlockType.PARAGRAPH,
        }

        heading_level_map = {
            "heading1": HeadingLevel.H1,
            "heading2": HeadingLevel.H2,
            "heading3": HeadingLevel.H3,
        }

        for idx, elem in enumerate(elements):
            category = elem.get("category", "paragraph")
            block_type = category_map.get(category, BlockType.PARAGRAPH)

            # Extract text
            content = elem.get("content", {})
            text = ""
            html_content = ""
            if isinstance(content, dict):
                text = content.get("text", "") or content.get("markdown", "")
                html_content = content.get("html", "")
            elif isinstance(content, str):
                text = content

            # Extract bounding box from coordinates
            bbox = self._extract_bbox(elem.get("coordinates", []))

            # Heading level
            heading_level = heading_level_map.get(category, HeadingLevel.NONE)

            # Build style from Upstage metadata
            style = TextStyle()
            if heading_level != HeadingLevel.NONE:
                style.is_bold = True
                if heading_level == HeadingLevel.H1:
                    style.font_size = 24.0
                    style.alignment = Alignment.CENTER
                elif heading_level == HeadingLevel.H2:
                    style.font_size = 18.0
                elif heading_level == HeadingLevel.H3:
                    style.font_size = 15.0

            # Table structure
            table_structure = None
            if block_type == BlockType.TABLE and html_content:
                table_structure = self._parse_table_html(html_content, bbox)

            # Role
            role = "paragraph"
            if block_type == BlockType.HEADING:
                role = "title" if heading_level == HeadingLevel.H1 else "section_heading"
            elif block_type == BlockType.CAPTION:
                role = "caption"
            elif block_type == BlockType.FOOTNOTE:
                role = "footnote"

            block = LayoutBlock(
                id=f"up_{page_idx}_{idx}",
                block_type=block_type,
                bbox=bbox,
                text=text,
                style=style,
                confidence=0.98,  # Upstage is high-accuracy
                page_index=page_idx,
                table_structure=table_structure,
                heading_level=heading_level,
                role=role,
                reading_order=idx,
                column_index=0,
            )
            blocks.append(block)

        return blocks

    def _extract_bbox(
        self,
        coordinates: list[dict],
    ) -> BBox | None:
        """Convert Upstage normalized coordinates to pixel BBox.

        Upstage returns 4 corner points as normalized [0-1] coordinates.
        We convert to pixel coordinates assuming A4 at 300 DPI.
        """
        if not coordinates or len(coordinates) < 4:
            return None

        # A4 at 300 DPI
        page_width = 2480.0
        page_height = 3508.0

        try:
            xs = [c.get("x", 0) for c in coordinates]
            ys = [c.get("y", 0) for c in coordinates]
            return BBox(
                x0=min(xs) * page_width,
                y0=min(ys) * page_height,
                x1=max(xs) * page_width,
                y1=max(ys) * page_height,
            )
        except (TypeError, ValueError):
            return None

    def _parse_table_html(
        self,
        html_content: str,
        table_bbox: BBox | None,
    ) -> TableStructure | None:
        """Parse Upstage table HTML into TableStructure.

        Upstage returns tables as standard HTML <table> elements with
        proper <tr>/<td>/<th> structure including colspan/rowspan.
        """
        try:
            from html.parser import HTMLParser

            cells: list[TableCell] = []
            current_row = -1
            current_col = 0
            max_cols = 0
            in_cell = False
            cell_text_parts: list[str] = []
            cell_is_header = False
            cell_rowspan = 1
            cell_colspan = 1
            # Track which (row,col) positions are occupied by rowspan/colspan
            occupied: set[tuple[int, int]] = set()

            class TableParser(HTMLParser):
                nonlocal current_row, current_col, max_cols
                nonlocal in_cell, cell_text_parts, cell_is_header
                nonlocal cell_rowspan, cell_colspan, occupied

                def handle_starttag(self, tag, attrs):
                    nonlocal current_row, current_col, max_cols
                    nonlocal in_cell, cell_text_parts, cell_is_header
                    nonlocal cell_rowspan, cell_colspan

                    if tag == "tr":
                        current_row += 1
                        current_col = 0
                        # Skip occupied cells
                        while (current_row, current_col) in occupied:
                            current_col += 1
                    elif tag in ("td", "th"):
                        # Skip occupied cells
                        while (current_row, current_col) in occupied:
                            current_col += 1
                        in_cell = True
                        cell_text_parts = []
                        cell_is_header = tag == "th"
                        attrs_dict = dict(attrs)
                        cell_rowspan = int(attrs_dict.get("rowspan", 1))
                        cell_colspan = int(attrs_dict.get("colspan", 1))

                def handle_endtag(self, tag):
                    nonlocal in_cell, current_col, max_cols

                    if tag in ("td", "th") and in_cell:
                        text = " ".join(cell_text_parts).strip()
                        cells.append(TableCell(
                            row=current_row,
                            col=current_col,
                            rowspan=cell_rowspan,
                            colspan=cell_colspan,
                            text=text,
                            is_header=cell_is_header,
                        ))
                        # Mark occupied positions
                        for dr in range(cell_rowspan):
                            for dc in range(cell_colspan):
                                if dr > 0 or dc > 0:
                                    occupied.add((current_row + dr, current_col + dc))
                        max_cols = max(max_cols, current_col + cell_colspan)
                        current_col += cell_colspan
                        in_cell = False

                def handle_data(self, data):
                    if in_cell:
                        cell_text_parts.append(data.strip())

            parser = TableParser()
            parser.feed(html_content)

            if not cells:
                return None

            num_rows = current_row + 1
            return TableStructure(
                num_rows=num_rows,
                num_cols=max_cols,
                cells=cells,
                has_visible_borders=True,
                bbox=table_bbox,
            )

        except Exception as exc:
            logger.warning("Failed to parse table HTML: %s", exc)
            return None

    # ------------------------------------------------------------------
    # PDF analysis helpers
    # ------------------------------------------------------------------

    def _detect_scanned(self, doc: Any) -> bool:
        """Detect if a PDF is scanned (image-only) by checking first few pages."""
        check_pages = min(3, len(doc))
        text_pages = 0
        for i in range(check_pages):
            page = doc[i]
            text = page.get_text("text").strip()
            if len(text) > 50:  # Has meaningful text
                text_pages += 1

        # If less than half of checked pages have text, likely scanned
        return text_pages < check_pages / 2

    def _split_pdf_for_upload(
        self,
        pdf_path: Path,
        total_pages: int,
    ) -> list[tuple[Path, int, int]]:
        """Split PDF into chunks for parallel Upstage API calls.

        Returns list of (chunk_path, start_page, end_page).
        For small PDFs (<= _MAX_PAGES_PER_CALL), returns the original file.
        """
        if total_pages <= _MAX_PAGES_PER_CALL:
            return [(pdf_path, 0, total_pages)]

        import fitz
        chunks: list[tuple[Path, int, int]] = []
        doc = fitz.open(str(pdf_path))
        tmp_dir = Path(tempfile.mkdtemp(prefix="upstage_"))

        for start in range(0, total_pages, _MAX_PAGES_PER_CALL):
            end = min(start + _MAX_PAGES_PER_CALL, total_pages)
            chunk_doc = fitz.open()
            chunk_doc.insert_pdf(doc, from_page=start, to_page=end - 1)
            chunk_path = tmp_dir / f"chunk_{start:04d}_{end:04d}.pdf"
            chunk_doc.save(str(chunk_path))
            chunk_doc.close()
            chunks.append((chunk_path, start, end))

        doc.close()
        return chunks
