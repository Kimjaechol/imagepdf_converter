"""OCR engine – extract text from page images per layout block."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

from backend.models.schema import BBox, BlockType, LayoutBlock, TextStyle

logger = logging.getLogger(__name__)


class OcrEngine:
    """Run OCR on image regions identified by the layout detector."""

    def __init__(
        self,
        engine: str = "surya",
        languages: list[str] | None = None,
        tesseract_config: str = "--oem 3 --psm 6",
    ):
        self.engine = engine
        self.languages = languages or ["ko", "en"]
        self.tesseract_config = tesseract_config
        self._surya_model = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def ocr_page(
        self,
        image_path: str,
        blocks: list[LayoutBlock],
        page_index: int,
    ) -> list[LayoutBlock]:
        """Run OCR on each block that has no text yet.

        For blocks that already have digital text (confidence == 1.0), skip.
        Returns blocks with ``.text`` populated.
        """
        img = Image.open(image_path).convert("RGB")

        # Separate blocks that need OCR
        needs_ocr = [b for b in blocks if not b.text and b.block_type not in (
            BlockType.FIGURE, BlockType.EQUATION,
        )]
        already_has_text = [b for b in blocks if b.text]
        image_blocks = [b for b in blocks if b.block_type in (
            BlockType.FIGURE, BlockType.EQUATION,
        ) and not b.text]

        if needs_ocr:
            if self.engine == "surya":
                self._ocr_surya(img, needs_ocr, page_index)
            else:
                self._ocr_tesseract(img, needs_ocr, page_index)

        return already_has_text + needs_ocr + image_blocks

    def ocr_full_page(self, image_path: str, page_index: int) -> list[LayoutBlock]:
        """OCR the entire page at once (when no layout blocks are available)."""
        img = Image.open(image_path).convert("RGB")
        blocks: list[LayoutBlock] = []

        if self.engine == "surya":
            blocks = self._ocr_surya_full(img, page_index)
        else:
            blocks = self._ocr_tesseract_full(img, page_index)

        return blocks

    # ------------------------------------------------------------------
    # Surya OCR
    # ------------------------------------------------------------------

    def _ocr_surya(
        self,
        img: Image.Image,
        blocks: list[LayoutBlock],
        page_index: int,
    ) -> None:
        try:
            from surya.ocr import run_ocr
            from surya.model.detection.model import load_model as load_det_model
            from surya.model.detection.model import load_processor as load_det_proc
            from surya.model.recognition.model import load_model as load_rec_model
            from surya.model.recognition.processor import load_processor as load_rec_proc

            if self._surya_model is None:
                self._surya_model = {
                    "det_model": load_det_model(),
                    "det_proc": load_det_proc(),
                    "rec_model": load_rec_model(),
                    "rec_proc": load_rec_proc(),
                }

            # Crop block regions and OCR each
            for block in blocks:
                if block.bbox is None:
                    continue
                cropped = img.crop((
                    int(block.bbox.x0),
                    int(block.bbox.y0),
                    int(block.bbox.x1),
                    int(block.bbox.y1),
                ))
                results = run_ocr(
                    [cropped],
                    [self.languages],
                    self._surya_model["det_model"],
                    self._surya_model["det_proc"],
                    self._surya_model["rec_model"],
                    self._surya_model["rec_proc"],
                )
                if results:
                    text_lines = [
                        line.text for line in results[0].text_lines
                    ]
                    block.text = "\n".join(text_lines)
        except ImportError:
            logger.warning("Surya not installed, falling back to Tesseract.")
            self._ocr_tesseract(img, blocks, page_index)
        except Exception as exc:
            logger.error("Surya OCR failed: %s", exc)
            self._ocr_tesseract(img, blocks, page_index)

    def _ocr_surya_full(self, img: Image.Image, page_index: int) -> list[LayoutBlock]:
        try:
            from surya.ocr import run_ocr
            from surya.model.detection.model import load_model as load_det_model
            from surya.model.detection.model import load_processor as load_det_proc
            from surya.model.recognition.model import load_model as load_rec_model
            from surya.model.recognition.processor import load_processor as load_rec_proc

            if self._surya_model is None:
                self._surya_model = {
                    "det_model": load_det_model(),
                    "det_proc": load_det_proc(),
                    "rec_model": load_rec_model(),
                    "rec_proc": load_rec_proc(),
                }

            results = run_ocr(
                [img],
                [self.languages],
                self._surya_model["det_model"],
                self._surya_model["det_proc"],
                self._surya_model["rec_model"],
                self._surya_model["rec_proc"],
            )

            blocks: list[LayoutBlock] = []
            if results:
                for i, line in enumerate(results[0].text_lines):
                    b = line.bbox
                    blocks.append(LayoutBlock(
                        id=f"ocr_{page_index}_{i}",
                        block_type=BlockType.PARAGRAPH,
                        bbox=BBox(x0=b[0], y0=b[1], x1=b[2], y1=b[3]),
                        text=line.text,
                        confidence=line.confidence if hasattr(line, "confidence") else 0.8,
                        page_index=page_index,
                    ))
            return blocks
        except ImportError:
            return self._ocr_tesseract_full(img, page_index)

    # ------------------------------------------------------------------
    # Tesseract fallback
    # ------------------------------------------------------------------

    def _ocr_tesseract(
        self,
        img: Image.Image,
        blocks: list[LayoutBlock],
        page_index: int,
    ) -> None:
        try:
            import pytesseract
        except ImportError:
            logger.error("pytesseract is not installed.")
            return

        lang_str = "+".join(self._tesseract_lang_codes())

        for block in blocks:
            if block.bbox is None:
                continue
            cropped = img.crop((
                int(block.bbox.x0),
                int(block.bbox.y0),
                int(block.bbox.x1),
                int(block.bbox.y1),
            ))
            text = pytesseract.image_to_string(
                cropped,
                lang=lang_str,
                config=self.tesseract_config,
            )
            block.text = text.strip()

    def _ocr_tesseract_full(self, img: Image.Image, page_index: int) -> list[LayoutBlock]:
        try:
            import pytesseract
        except ImportError:
            logger.error("pytesseract is not installed.")
            return []

        lang_str = "+".join(self._tesseract_lang_codes())
        data = pytesseract.image_to_data(
            img,
            lang=lang_str,
            config=self.tesseract_config,
            output_type=pytesseract.Output.DICT,
        )

        blocks: list[LayoutBlock] = []
        current_text_parts: list[str] = []
        current_bbox: list[float] = [9999, 9999, 0, 0]
        last_block_num = -1

        for i in range(len(data["text"])):
            block_num = data["block_num"][i]
            text = data["text"][i].strip()

            if block_num != last_block_num and last_block_num != -1:
                # Emit previous block
                if current_text_parts:
                    blocks.append(LayoutBlock(
                        id=f"tess_{page_index}_{len(blocks)}",
                        block_type=BlockType.PARAGRAPH,
                        bbox=BBox(*current_bbox),
                        text=" ".join(current_text_parts),
                        confidence=0.7,
                        page_index=page_index,
                    ))
                current_text_parts = []
                current_bbox = [9999, 9999, 0, 0]

            if text:
                current_text_parts.append(text)
                x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
                current_bbox[0] = min(current_bbox[0], x)
                current_bbox[1] = min(current_bbox[1], y)
                current_bbox[2] = max(current_bbox[2], x + w)
                current_bbox[3] = max(current_bbox[3], y + h)

            last_block_num = block_num

        if current_text_parts:
            blocks.append(LayoutBlock(
                id=f"tess_{page_index}_{len(blocks)}",
                block_type=BlockType.PARAGRAPH,
                bbox=BBox(*current_bbox),
                text=" ".join(current_text_parts),
                confidence=0.7,
                page_index=page_index,
            ))

        return blocks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _tesseract_lang_codes(self) -> list[str]:
        mapping = {"ko": "kor", "en": "eng", "zh": "chi_sim", "ja": "jpn"}
        return [mapping.get(lang, lang) for lang in self.languages]
