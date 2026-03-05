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

            # Crop region with padding to avoid cutting edges
            pad = 4  # pixels of padding
            cropped = img.crop((
                max(0, int(block.bbox.x0) - pad),
                max(0, int(block.bbox.y0) - pad),
                min(img.width, int(block.bbox.x1) + pad),
                min(img.height, int(block.bbox.y1) + pad),
            ))

            # Use JPEG for large photo-like images, PNG for equations/diagrams
            is_photo = (cropped.width * cropped.height > 200_000 and
                        block.block_type == BlockType.FIGURE)
            ext = "jpg" if is_photo else "png"
            filename = f"{block.block_type.value}_{page_index:04d}_{uuid.uuid4().hex[:6]}.{ext}"
            save_path = self.output_dir / filename
            if ext == "jpg":
                cropped.save(str(save_path), "JPEG", quality=90)
            else:
                cropped.save(str(save_path))
            block.image_path = str(save_path)

            logger.info(
                "Extracted %s from page %d → %s",
                block.block_type.value, page_index, save_path,
            )

        return blocks
