"""Data models / schema definitions for the pipeline."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class BlockType(str, enum.Enum):
    HEADING = "heading"
    SUBTITLE = "subtitle"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    FIGURE = "figure"
    EQUATION = "equation"
    LIST = "list"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    ENDNOTE = "endnote"
    HEADER = "header"
    FOOTER = "footer"
    BOX = "box"
    BALLOON = "balloon"
    ARROW_ANNOTATION = "arrow_annotation"
    PAGE_NUMBER = "page_number"
    UNKNOWN = "unknown"


class HeadingLevel(str, enum.Enum):
    H1 = "h1"
    H2 = "h2"
    H3 = "h3"
    H4 = "h4"
    H5 = "h5"
    H6 = "h6"
    NONE = "none"


class Alignment(str, enum.Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


# ---------------------------------------------------------------------------
# Bounding box
# ---------------------------------------------------------------------------

@dataclass
class BBox:
    """Bounding box (x0, y0, x1, y1) in pixel coordinates."""
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def area(self) -> float:
        return self.width * self.height

    def overlap_ratio(self, other: "BBox") -> float:
        ix0 = max(self.x0, other.x0)
        iy0 = max(self.y0, other.y0)
        ix1 = min(self.x1, other.x1)
        iy1 = min(self.y1, other.y1)
        if ix1 <= ix0 or iy1 <= iy0:
            return 0.0
        inter = (ix1 - ix0) * (iy1 - iy0)
        return inter / min(self.area, other.area) if min(self.area, other.area) > 0 else 0.0


# ---------------------------------------------------------------------------
# Style information
# ---------------------------------------------------------------------------

@dataclass
class TextStyle:
    font_size: float = 12.0
    is_bold: bool = False
    is_italic: bool = False
    is_underline: bool = False
    font_name: str = ""
    alignment: Alignment = Alignment.LEFT
    line_spacing: float = 1.0
    color: str = "#000000"


# ---------------------------------------------------------------------------
# Table structures
# ---------------------------------------------------------------------------

@dataclass
class TableCell:
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    text: str = ""
    bbox: BBox | None = None
    style: TextStyle | None = None
    is_header: bool = False


@dataclass
class TableStructure:
    num_rows: int = 0
    num_cols: int = 0
    cells: list[TableCell] = field(default_factory=list)
    has_visible_borders: bool = True
    bbox: BBox | None = None


# ---------------------------------------------------------------------------
# Layout blocks
# ---------------------------------------------------------------------------

@dataclass
class LayoutBlock:
    """A single detected region on a page."""
    id: str = ""
    block_type: BlockType = BlockType.UNKNOWN
    bbox: BBox | None = None
    text: str = ""
    style: TextStyle | None = None
    confidence: float = 0.0
    page_index: int = 0
    # For tables
    table_structure: TableStructure | None = None
    # For figures/equations/images
    image_path: str | None = None
    caption: str = ""
    # Heading classification results
    heading_level: HeadingLevel = HeadingLevel.NONE
    role: str = ""
    # Reading order
    reading_order: int = -1
    # Link to other blocks (footnotes, annotations)
    linked_block_ids: list[str] = field(default_factory=list)
    # Column assignment
    column_index: int = 0
    # Children blocks (for nested structures)
    children: list["LayoutBlock"] = field(default_factory=list)
    # ── Content anchoring & sequential numbering ──
    # Document-wide sequential number for tables/figures (1-based)
    content_seq: int = 0          # e.g. table_seq=3 → "표 3" / "Table 3"
    # ID of the parent block this caption belongs to
    parent_block_id: str = ""     # for CAPTION → links to TABLE/FIGURE id
    # Footnote marker (e.g. "1", "*", "†") linking body ref to footnote block
    footnote_marker: str = ""


# ---------------------------------------------------------------------------
# Page & Document level
# ---------------------------------------------------------------------------

@dataclass
class PageResult:
    page_index: int = 0
    width: float = 0.0
    height: float = 0.0
    blocks: list[LayoutBlock] = field(default_factory=list)
    num_columns: int = 1
    image_path: str | None = None


@dataclass
class ChunkResult:
    chunk_index: int = 0
    start_page: int = 0
    end_page: int = 0
    pages: list[PageResult] = field(default_factory=list)


@dataclass
class DocumentResult:
    source_path: str = ""
    total_pages: int = 0
    chunks: list[ChunkResult] = field(default_factory=list)
    pages: list[PageResult] = field(default_factory=list)
    html: str = ""
    markdown: str = ""
    viewer_html: str = ""  # High-fidelity viewer HTML (pdf2htmlEX or fallback)
    images: dict[str, str] = field(default_factory=dict)  # id -> path
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pipeline job
# ---------------------------------------------------------------------------

@dataclass
class PdfJob:
    input_path: Path = field(default_factory=lambda: Path())
    output_dir: Path = field(default_factory=lambda: Path())
    filename: str = ""
    output_formats: list[str] = field(default_factory=lambda: ["html", "markdown"])
    # Translation options
    translate: bool = False
    source_language: str = ""      # e.g. "ja", "en", "zh" (auto-detect if empty)
    target_language: str = "ko"    # e.g. "ko", "en", "ja"


@dataclass
class PdfChunk:
    chunk_index: int = 0
    start_page: int = 0
    end_page: int = 0
    pdf_path: Path = field(default_factory=lambda: Path())
    total_pages_in_doc: int = 0
