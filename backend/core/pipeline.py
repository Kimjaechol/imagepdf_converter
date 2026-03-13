"""Main pipeline orchestrator – PDF to HTML/Markdown conversion.

Two workflows based on PDF type (auto-detected):
  - Image/Scanned PDF: Upstage Document Parse (OCR) → Gemini visual correction
  - Digital PDF:       PyMuPDF text/style extraction → Gemini visual correction
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from backend.models.schema import (
    BlockType,
    DocumentResult,
    LayoutBlock,
    PageResult,
    PdfJob,
)
from .block_integrity import assign_content_ids_and_seq
from .correction import CorrectionEngine
from .html_renderer import HtmlRenderer
from .image_extractor import ImageExtractor
from .md_renderer import MarkdownRenderer

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    dpi: int = 300
    output_formats: list[str] = field(default_factory=lambda: ["html", "markdown"])
    # Gemini model for visual comparison / correction
    gemini_model: str = "gemini-3.1-flash-lite-preview"
    # Correction
    correction_mode: str = "hybrid"
    correction_llm: str = "gemini"
    correction_dict_path: str = "config/correction_dict.json"
    correction_aggressiveness: str = "conservative"
    ollama_model: str = "qwen2.5:7b"
    ollama_base_url: str = "http://localhost:11434"
    # HTML / Markdown output
    html_simplified: bool = True
    html_inline_css: bool = True
    md_table_format: str = "pipe"
    md_footnote_style: str = "reference"
    # Upstage Document Parse
    upstage_mode: str = "auto"
    upstage_max_workers: int = 4
    upstage_force_enhanced_scanned: bool = True
    # Gemini visual comparison
    gemini_visual_batch_size: int = 3
    gemini_max_structure_change: float = 0.3

    # ── User-provided LLM correction (optional, runs locally) ──
    # If user provides their own API key, LLM correction runs on their machine
    # provider: "gemini" | "openai" | "claude" | "" (disabled)
    user_llm_provider: str = ""
    user_llm_api_key: str = ""
    user_llm_model: str = ""  # empty = use default for provider

    # ── kept for backward-compat with server.py / YAML ──
    pipeline_mode: str = "upstage_hybrid"
    pages_per_chunk: int = 10
    max_workers: int = 10
    layout_engine: str = "surya"
    layout_confidence: float = 0.5
    ocr_engine: str = "surya"
    ocr_languages: list[str] = field(default_factory=lambda: ["ko", "en"])
    table_engine: str = "table_transformer"
    merge_multipage_tables: bool = True
    reading_order_mode: str = "hybrid"
    reading_order_vlm: str = "gemini"
    heading_mode: str = "hybrid"
    heading_llm: str = "gemini"
    heading_ollama_model: str = "qwen2.5:0.5b-instruct"

    @classmethod
    def from_yaml(cls, path: str) -> "PipelineConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        pipe = data.get("pipeline", {})
        corr = data.get("correction", {})
        out = data.get("output", {})
        models = data.get("models", {})
        upstage = data.get("upstage", {})
        ro = data.get("reading_order", {})
        head = data.get("heading", {})
        layout = data.get("layout", {})
        ocr = data.get("ocr", {})
        table = data.get("table", {})
        user_llm = data.get("user_llm", {})

        return cls(
            dpi=pipe.get("dpi", 300),
            output_formats=pipe.get("output_formats", ["html", "markdown"]),
            gemini_model=ro.get("gemini_model", "gemini-3.1-flash-lite-preview"),
            correction_mode=corr.get("mode", "hybrid"),
            correction_llm=corr.get("llm_provider", "gemini"),
            correction_dict_path=corr.get("dictionary_path", "config/correction_dict.json"),
            correction_aggressiveness=corr.get("aggressiveness", "conservative"),
            ollama_model=ro.get("ollama_model", "qwen2.5:7b"),
            ollama_base_url=models.get("ollama_base_url", "http://localhost:11434"),
            html_simplified=out.get("html", {}).get("simplified", True),
            html_inline_css=out.get("html", {}).get("inline_css", True),
            md_table_format=out.get("markdown", {}).get("table_format", "pipe"),
            md_footnote_style=out.get("markdown", {}).get("footnote_style", "reference"),
            upstage_mode=upstage.get("mode", "auto"),
            upstage_max_workers=upstage.get("max_workers", 4),
            upstage_force_enhanced_scanned=upstage.get("force_enhanced_scanned", True),
            gemini_visual_batch_size=upstage.get("visual_batch_size", 3),
            gemini_max_structure_change=upstage.get("max_structure_change", 0.3),
            # backward-compat fields
            pipeline_mode=pipe.get("mode", "upstage_hybrid"),
            pages_per_chunk=pipe.get("pages_per_chunk", 10),
            max_workers=pipe.get("max_workers", 4),
            layout_engine=layout.get("engine", "surya"),
            layout_confidence=layout.get("confidence_threshold", 0.5),
            ocr_engine=ocr.get("engine", "surya"),
            ocr_languages=ocr.get("languages", ["ko", "en"]),
            table_engine=table.get("engine", "table_transformer"),
            merge_multipage_tables=table.get("merge_multipage", True),
            reading_order_mode=ro.get("mode", "hybrid"),
            reading_order_vlm=ro.get("vlm_provider", "gemini"),
            heading_mode=head.get("mode", "hybrid"),
            heading_llm=head.get("llm_provider", "gemini"),
            heading_ollama_model=head.get("ollama_model", "qwen2.5:0.5b-instruct"),
            # User LLM correction
            user_llm_provider=user_llm.get("provider", ""),
            user_llm_api_key=user_llm.get("api_key", ""),
            user_llm_model=user_llm.get("model", ""),
        )


class Pipeline:
    """PDF → HTML/Markdown conversion.

    Auto-detects PDF type and routes to the appropriate workflow:
      - Image/Scanned PDF → Upstage Document Parse + Gemini correction
      - Digital PDF        → PyMuPDF extraction + Gemini correction
    """

    def __init__(
        self,
        config: PipelineConfig | None = None,
        progress_callback: Callable[[str, float], None] | None = None,
    ):
        self.cfg = config or PipelineConfig()
        self.progress_callback = progress_callback or (lambda msg, pct: None)

        # Correction engine (dictionary + LLM)
        self.correction = CorrectionEngine(
            dictionary_path=self.cfg.correction_dict_path,
            mode=self.cfg.correction_mode,
            llm_provider=self.cfg.correction_llm,
            gemini_model=self.cfg.gemini_model,
            ollama_model=self.cfg.ollama_model,
            ollama_base_url=self.cfg.ollama_base_url,
            aggressiveness=self.cfg.correction_aggressiveness,
        )

        # Renderers
        self.html_renderer = HtmlRenderer(
            simplified=self.cfg.html_simplified,
            inline_css=self.cfg.html_inline_css,
        )
        self.md_renderer = MarkdownRenderer(
            table_format=self.cfg.md_table_format,
            footnote_style=self.cfg.md_footnote_style,
        )

        # Lazy-initialized components
        self._upstage_parser = None
        self._digital_extractor = None
        self._gemini_refiner = None
        self._user_llm_corrector = None

    # ------------------------------------------------------------------
    # Lazy properties
    # ------------------------------------------------------------------

    @property
    def upstage_parser(self):
        if self._upstage_parser is None:
            from .upstage_parser import UpstageDocumentParser, UpstageParseConfig
            self._upstage_parser = UpstageDocumentParser(
                config=UpstageParseConfig(
                    mode=self.cfg.upstage_mode,
                    force_enhanced_for_scanned=self.cfg.upstage_force_enhanced_scanned,
                    max_workers=self.cfg.upstage_max_workers,
                ),
            )
        return self._upstage_parser

    @property
    def digital_extractor(self):
        if self._digital_extractor is None:
            from .digital_pdf_extractor import DigitalPdfExtractor
            self._digital_extractor = DigitalPdfExtractor(dpi=self.cfg.dpi)
        return self._digital_extractor

    @property
    def gemini_refiner(self):
        if self._gemini_refiner is None:
            from .upstage_gemini_refiner import RefinementConfig, UpstageGeminiRefiner
            self._gemini_refiner = UpstageGeminiRefiner(
                config=RefinementConfig(
                    gemini_model=self.cfg.gemini_model,
                    visual_batch_size=self.cfg.gemini_visual_batch_size,
                    max_structure_change_ratio=self.cfg.gemini_max_structure_change,
                ),
            )
        return self._gemini_refiner

    @property
    def user_llm_corrector(self):
        """User-provided LLM corrector (optional, runs locally)."""
        if self._user_llm_corrector is None and self.cfg.user_llm_provider:
            from .llm_corrector import create_corrector
            self._user_llm_corrector = create_corrector(
                provider=self.cfg.user_llm_provider,
                api_key=self.cfg.user_llm_api_key,
                model=self.cfg.user_llm_model,
            )
        return self._user_llm_corrector

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def process(self, job: PdfJob) -> DocumentResult:
        """Process a single PDF end-to-end.

        1. Detect PDF type (digital vs image/scanned)
        2. Extract text + structure
           - Digital  → PyMuPDF (preserves fonts, styles, exact text)
           - Scanned  → Upstage Document Parse (OCR + layout)
        3. Gemini visual comparison & correction
        4. Dictionary-based correction safety net
        5. Render HTML / Markdown
        """
        start = time.time()

        if job.translate:
            src = job.source_language or "auto-detect"
            self.progress_callback(
                f"Starting conversion + translation ({src} → {job.target_language})",
                0.0,
            )
        else:
            self.progress_callback("Starting conversion", 0.0)

        work_dir = Path(tempfile.mkdtemp(prefix="pdfconv_"))
        images_dir = work_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        output_images_dir = job.output_dir / "images"

        # ── Step 1: Detect PDF type ──
        self.progress_callback("Detecting PDF type", 0.02)
        is_digital = self.digital_extractor.is_digital_pdf(job.input_path)

        # ── Step 2: Extract ──
        if is_digital:
            all_pages, page_images, total_pages = self._extract_digital(
                job, images_dir,
            )
        else:
            all_pages, page_images, total_pages = self._extract_scanned(
                job, images_dir,
            )

        # ── Step 3: Gemini visual comparison (operator key, optional) ──
        # Only runs if GEMINI_API_KEY is set on the server (operator's key).
        # If not set, skip this step – user can optionally use their own
        # LLM key for correction in Step 3b.
        all_pages = self._refine_with_gemini(
            all_pages, page_images, job,
        )

        # ── Step 3b: User LLM correction (user's own API key, optional) ──
        # If the user provided their own LLM API key (Gemini/OpenAI/Claude),
        # apply additional correction locally on their machine.
        if self.user_llm_corrector:
            self.progress_callback("사용자 LLM 교정 중", 0.75)
            all_pages = self._correct_with_user_llm(all_pages, is_digital)

        # ── Step 4: Dictionary-based correction ──
        self.progress_callback("Applying dictionary corrections", 0.80)
        all_blocks = [b for p in all_pages for b in p.blocks]
        self.correction.correct_dictionary_only(all_blocks)

        # ── Step 5: Block integrity ──
        self.progress_callback("Verifying block integrity", 0.85)
        all_pages = assign_content_ids_and_seq(all_pages)

        # ── Step 6: Extract images (figures/equations) ──
        self.progress_callback("Extracting images", 0.88)
        image_extractor = ImageExtractor(output_dir=output_images_dir)
        for page in all_pages:
            img_path = page_images.get(page.page_index)
            if img_path:
                page.blocks = image_extractor.extract_images(
                    img_path, page.blocks, page.page_index,
                )

        # ── Step 7: Render output ──
        self.progress_callback("Rendering output", 0.90)
        total_pages = total_pages or len(all_pages)
        result = DocumentResult(
            source_path=str(job.input_path),
            total_pages=total_pages,
            pages=all_pages,
        )

        if "html" in job.output_formats:
            result.html = self.html_renderer.render(all_pages)
        if "markdown" in job.output_formats:
            result.markdown = self.md_renderer.render(all_pages)

        # ── Step 7b: Generate viewer HTML (pdf2htmlEX or fallback) ──
        # Only for digital PDFs – produces a high-fidelity layout-preserving
        # HTML for the read-only viewer layer.
        if is_digital and "html" in job.output_formats:
            self.progress_callback("Generating viewer HTML", 0.92)
            result.viewer_html = self._generate_viewer_html(job)

        # ── Step 8: Save ──
        self.progress_callback("Saving files", 0.95)
        self._save_outputs(result, job, is_digital=is_digital)

        elapsed = time.time() - start
        self.progress_callback(f"Done in {elapsed:.1f}s", 1.0)
        result.metadata["elapsed_seconds"] = elapsed

        return result

    # ------------------------------------------------------------------
    # Digital PDF workflow
    # ------------------------------------------------------------------

    def _extract_digital(
        self,
        job: PdfJob,
        images_dir: Path,
    ) -> tuple[list[PageResult], dict[int, str], int]:
        """Digital PDF → PyMuPDF extraction + page images for Gemini."""
        logger.info("Digital PDF detected – using PyMuPDF extraction")
        self.progress_callback("Extracting text from digital PDF", 0.05)

        all_pages, page_images = self.digital_extractor.extract(
            job.input_path,
            render_images=True,
            images_dir=images_dir,
        )

        total_pages = len(all_pages)
        self.progress_callback(
            f"Extracted {total_pages} pages with PyMuPDF", 0.30,
        )
        return all_pages, page_images, total_pages

    # ------------------------------------------------------------------
    # Image/Scanned PDF workflow
    # ------------------------------------------------------------------

    def _extract_scanned(
        self,
        job: PdfJob,
        images_dir: Path,
    ) -> tuple[list[PageResult], dict[int, str], int]:
        """Scanned/Image PDF → Upstage Document Parse OCR."""
        logger.info("Scanned/image PDF detected – using Upstage Document Parse")
        self.progress_callback("Sending to Upstage Document Parse", 0.05)

        upstage_api_key = os.environ.get("UPSTAGE_API_KEY", "")
        if not upstage_api_key:
            raise RuntimeError(
                "UPSTAGE_API_KEY not set. "
                "Scanned/image PDFs require Upstage Document Parse for OCR. "
                "Please set the UPSTAGE_API_KEY environment variable."
            )

        all_pages = self.upstage_parser.parse_pdf(
            job.input_path,
            progress_callback=self.progress_callback,
        )

        if not all_pages:
            raise RuntimeError(
                "Upstage Document Parse returned no results. "
                "The PDF may be corrupted or unsupported."
            )

        # Render page images for Gemini visual comparison
        self.progress_callback("Rendering page images for comparison", 0.45)
        page_images = self._render_all_page_images(
            job.input_path, images_dir,
        )

        total_pages = len(all_pages)
        self.progress_callback(
            f"Upstage parsed {total_pages} pages", 0.50,
        )
        return all_pages, page_images, total_pages

    # ------------------------------------------------------------------
    # Gemini visual comparison (shared by both workflows)
    # ------------------------------------------------------------------

    def _refine_with_gemini(
        self,
        all_pages: list[PageResult],
        page_images: dict[int, str],
        job: PdfJob,
    ) -> list[PageResult]:
        """Send extracted pages + original images to Gemini for correction."""
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        if not gemini_key:
            logger.warning(
                "GEMINI_API_KEY not set – skipping visual comparison. "
                "Output may contain uncorrected OCR errors."
            )
            return all_pages

        self.progress_callback("Starting Gemini visual comparison", 0.50)

        if job.translate:
            self.gemini_refiner.config.translate = True
            self.gemini_refiner.config.source_language = job.source_language
            self.gemini_refiner.config.target_language = job.target_language

        refinement_result = self.gemini_refiner.refine_with_visual_comparison(
            pages=all_pages,
            page_images=page_images,
            progress_callback=self.progress_callback,
        )

        logger.info(
            "Gemini visual comparison: %d corrections, %d rejected",
            refinement_result.corrections_applied,
            refinement_result.pages_rejected,
        )
        return refinement_result.pages

    # ------------------------------------------------------------------
    # User LLM correction (optional, local)
    # ------------------------------------------------------------------

    def _correct_with_user_llm(
        self,
        all_pages: list[PageResult],
        is_digital: bool,
    ) -> list[PageResult]:
        """Apply user's own LLM for text correction on extracted blocks."""
        corrector = self.user_llm_corrector
        if not corrector:
            return all_pages

        source_type = "digital_pdf" if is_digital else "image_pdf"

        for page in all_pages:
            for block in page.blocks:
                if not block.text or not block.text.strip():
                    continue
                # Only correct text-bearing blocks
                if block.block_type in (
                    BlockType.PARAGRAPH, BlockType.HEADING,
                    BlockType.LIST, BlockType.CAPTION,
                    BlockType.FOOTNOTE,
                ):
                    try:
                        corrected = corrector.correct_html(
                            block.text, source_type=source_type,
                        )
                        if corrected and corrected.strip():
                            block.text = corrected
                    except Exception as exc:
                        logger.warning(
                            "User LLM correction failed for block %s: %s",
                            block.id, exc,
                        )

        return all_pages

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _render_all_page_images(
        self,
        pdf_path: Path,
        images_dir: Path,
    ) -> dict[int, str]:
        """Render all PDF pages to images for Gemini visual comparison."""
        import fitz

        page_images: dict[int, str] = {}
        doc = fitz.open(str(pdf_path))
        zoom = self.cfg.dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)

        images_dir.mkdir(parents=True, exist_ok=True)

        for page_idx in range(len(doc)):
            try:
                pix = doc[page_idx].get_pixmap(matrix=mat)
                img_path = images_dir / f"page_{page_idx:04d}.png"
                pix.save(str(img_path))
                page_images[page_idx] = str(img_path)
            except Exception as exc:
                logger.warning("Failed to render page %d: %s", page_idx, exc)

        doc.close()
        return page_images

    def _generate_viewer_html(self, job: PdfJob) -> str:
        """Generate high-fidelity viewer HTML using pdf2htmlEX (or fallback).

        The viewer HTML preserves the original PDF layout as closely as possible
        using absolute positioning. It is read-only – editing happens in the
        Tiptap editor which works with the structured Markdown/HTML.
        """
        from .pdf2html_renderer import (
            is_pdf2htmlex_available,
            render_pdf_to_viewer_html,
            render_pdf_to_viewer_html_fallback,
        )

        viewer_dir = job.output_dir / "viewer"
        viewer_dir.mkdir(parents=True, exist_ok=True)

        viewer_path = None

        if is_pdf2htmlex_available():
            viewer_path = render_pdf_to_viewer_html(
                job.input_path,
                viewer_dir,
                output_filename="viewer.html",
            )

        if viewer_path is None:
            # Fallback to PyMuPDF HTML rendering
            viewer_path = render_pdf_to_viewer_html_fallback(
                job.input_path,
                viewer_dir,
                output_filename="viewer.html",
            )

        if viewer_path and viewer_path.exists():
            return viewer_path.read_text(encoding="utf-8")

        return ""

    def _save_outputs(
        self, result: DocumentResult, job: PdfJob, *, is_digital: bool = False,
    ) -> None:
        """Save HTML, Markdown, and viewer HTML files to disk."""
        job.output_dir.mkdir(parents=True, exist_ok=True)

        output_files = []

        if result.html:
            html_path = job.output_dir / f"{job.filename or 'output'}.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(result.html)
            output_files.append(str(html_path))
            logger.info("Saved HTML: %s", html_path)

        if result.markdown:
            md_path = job.output_dir / f"{job.filename or 'output'}.md"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(result.markdown)
            output_files.append(str(md_path))
            logger.info("Saved Markdown: %s", md_path)

        if result.viewer_html:
            viewer_path = job.output_dir / "viewer" / "viewer.html"
            # viewer.html is already saved by _generate_viewer_html,
            # but ensure it's tracked
            if viewer_path.exists():
                output_files.append(str(viewer_path))
                logger.info("Viewer HTML: %s", viewer_path)

        result.metadata["output_files"] = output_files
        result.metadata["is_digital"] = is_digital
        result.metadata["has_viewer"] = bool(result.viewer_html)

    def process_folder(
        self,
        folder: Path,
        output_dir: Path,
        recursive: bool = False,
    ) -> list[DocumentResult]:
        """Batch process all PDFs in a folder."""
        pattern = "**/*.pdf" if recursive else "*.pdf"
        pdf_files = sorted(folder.glob(pattern))

        results: list[DocumentResult] = []
        for i, pdf_path in enumerate(pdf_files):
            self.progress_callback(
                f"Processing file {i+1}/{len(pdf_files)}: {pdf_path.name}",
                i / len(pdf_files),
            )
            job = PdfJob(
                input_path=pdf_path,
                output_dir=output_dir / pdf_path.stem,
                filename=pdf_path.stem,
                output_formats=self.cfg.output_formats,
            )
            try:
                result = self.process(job)
                results.append(result)
            except Exception as exc:
                logger.error("Failed to process %s: %s", pdf_path, exc)

        return results
