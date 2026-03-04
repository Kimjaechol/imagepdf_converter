"""Extract and save images (figures, charts, equations) from PDF pages."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from PIL import Image

from backend.models.schema import BlockType, LayoutBlock

logger = logging.getLogger(__name__)


class ImageExtractor:
    """Crop and save images/figures/equations from page images."""

    def __init__(self, output_dir: Path | None = None):
        self.output_dir = output_dir or Path("output/images")

    def extract_images(
        self,
        image_path: str,
        blocks: list[LayoutBlock],
        page_index: int,
    ) -> list[LayoutBlock]:
        """For each FIGURE/EQUATION block, crop the region and save as image file."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        img = Image.open(image_path).convert("RGB")

        for block in blocks:
            if block.block_type not in (BlockType.FIGURE, BlockType.EQUATION):
                continue
            if block.bbox is None:
                continue

            # Crop region
            cropped = img.crop((
                max(0, int(block.bbox.x0)),
                max(0, int(block.bbox.y0)),
                min(img.width, int(block.bbox.x1)),
                min(img.height, int(block.bbox.y1)),
            ))

            # Save
            ext = "png"
            filename = f"{block.block_type.value}_{page_index:04d}_{uuid.uuid4().hex[:6]}.{ext}"
            save_path = self.output_dir / filename
            cropped.save(str(save_path))
            block.image_path = str(save_path)

            logger.info(
                "Extracted %s from page %d → %s",
                block.block_type.value, page_index, save_path,
            )

        return blocks
