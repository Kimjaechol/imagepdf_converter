"""Main pipeline orchestrator – PDF to HTML/Markdown conversion."""

from __future__ import annotations

import logging
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from backend.models.schema import (
    ChunkResult,
    DocumentResult,
    LayoutBlock,
    PageResult,
    PdfChunk,
    PdfJob,
)
from .correction import CorrectionEngine
from .heading_classifier import HeadingClassifier
from .html_renderer import HtmlRenderer
from .image_extractor import ImageExtractor
from .layout_detector import LayoutDetector
from .md_renderer import MarkdownRenderer
from .merger import ChunkMerger
from .ocr_engine import OcrEngine
from .page_renderer import PageRenderer
from .pdf_splitter import PdfSplitter
from .reading_order import ReadingOrderRefiner
from .table_recognizer import TableRecognizer

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    pages_per_chunk: int = 10
    max_workers: int = 4
    dpi: int = 300
    output_formats: list[str] = field(default_factory=lambda: ["html", "markdown"])
    # Sub-configs
    layout_engine: str = "surya"
    layout_confidence: float = 0.5
    ocr_engine: str = "surya"
    ocr_languages: list[str] = field(default_factory=lambda: ["ko", "en"])
    table_engine: str = "table_transformer"
    merge_multipage_tables: bool = True
    reading_order_mode: str = "hybrid"
    reading_order_vlm: str = "gemini"
    gemini_model: str = "gemini-3.1-flash-lite-preview"
    ollama_model: str = "qwen2.5:7b"
    ollama_base_url: str = "http://localhost:11434"
    heading_mode: str = "hybrid"
    heading_llm: str = "gemini"
    heading_ollama_model: str = "qwen2.5:0.5b-instruct"
    correction_mode: str = "hybrid"
    correction_llm: str = "gemini"
    correction_dict_path: str = "config/correction_dict.json"
    correction_aggressiveness: str = "conservative"
    html_simplified: bool = True
    html_inline_css: bool = True
    md_table_format: str = "pipe"
    md_footnote_style: str = "reference"

    @classmethod
    def from_yaml(cls, path: str) -> "PipelineConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        pipe = data.get("pipeline", {})
        layout = data.get("layout", {})
        ocr = data.get("ocr", {})
        table = data.get("table", {})
        ro = data.get("reading_order", {})
        head = data.get("heading", {})
        corr = data.get("correction", {})
        out = data.get("output", {})
        models = data.get("models", {})

        return cls(
            pages_per_chunk=pipe.get("pages_per_chunk", 10),
            max_workers=pipe.get("max_workers", 4),
            dpi=pipe.get("dpi", 300),
            output_formats=pipe.get("output_formats", ["html", "markdown"]),
            layout_engine=layout.get("engine", "surya"),
            layout_confidence=layout.get("confidence_threshold", 0.5),
            ocr_engine=ocr.get("engine", "surya"),
            ocr_languages=ocr.get("languages", ["ko", "en"]),
            table_engine=table.get("engine", "table_transformer"),
            merge_multipage_tables=table.get("merge_multipage", True),
            reading_order_mode=ro.get("mode", "hybrid"),
            reading_order_vlm=ro.get("vlm_provider", "gemini"),
            gemini_model=ro.get("gemini_model", "gemini-3.1-flash-lite-preview"),
            ollama_model=ro.get("ollama_model", "qwen2.5:7b"),
            ollama_base_url=models.get("ollama_base_url", "http://localhost:11434"),
            heading_mode=head.get("mode", "hybrid"),
            heading_llm=head.get("llm_provider", "gemini"),
            heading_ollama_model=head.get("ollama_model", "qwen2.5:0.5b-instruct"),
            correction_mode=corr.get("mode", "hybrid"),
            correction_llm=corr.get("llm_provider", "gemini"),
            correction_dict_path=corr.get("dictionary_path", "config/correction_dict.json"),
            correction_aggressiveness=corr.get("aggressiveness", "conservative"),
            html_simplified=out.get("html", {}).get("simplified", True),
            html_inline_css=out.get("html", {}).get("inline_css", True),
            md_table_format=out.get("markdown", {}).get("table_format", "pipe"),
            md_footnote_style=out.get("markdown", {}).get("footnote_style", "reference"),
        )


class Pipeline:
    """End-to-end PDF → HTML/Markdown conversion pipeline."""

    def __init__(
        self,
        config: PipelineConfig | None = None,
        progress_callback: Callable[[str, float], None] | None = None,
    ):
        self.cfg = config or PipelineConfig()
        self.progress_callback = progress_callback or (lambda msg, pct: None)

        # Initialize components
        self.splitter = PdfSplitter(self.cfg.pages_per_chunk)
        self.renderer = PageRenderer(dpi=self.cfg.dpi)
        self.layout = LayoutDetector(
            engine=self.cfg.layout_engine,
            confidence_threshold=self.cfg.layout_confidence,
        )
        self.ocr = OcrEngine(
            engine=self.cfg.ocr_engine,
            languages=self.cfg.ocr_languages,
        )
        self.table_rec = TableRecognizer(engine=self.cfg.table_engine)
        self.reading_order = ReadingOrderRefiner(
            mode=self.cfg.reading_order_mode,
            vlm_provider=self.cfg.reading_order_vlm,
            gemini_model=self.cfg.gemini_model,
            ollama_model=self.cfg.ollama_model,
            ollama_base_url=self.cfg.ollama_base_url,
        )
        self.heading_clf = HeadingClassifier(
            mode=self.cfg.heading_mode,
            llm_provider=self.cfg.heading_llm,
            ollama_model=self.cfg.heading_ollama_model,
            ollama_base_url=self.cfg.ollama_base_url,
            gemini_model=self.cfg.gemini_model,
        )
        self.correction = CorrectionEngine(
            dictionary_path=self.cfg.correction_dict_path,
            mode=self.cfg.correction_mode,
            llm_provider=self.cfg.correction_llm,
            gemini_model=self.cfg.gemini_model,
            ollama_model=self.cfg.ollama_model,
            ollama_base_url=self.cfg.ollama_base_url,
            aggressiveness=self.cfg.correction_aggressiveness,
        )
        self.merger = ChunkMerger(merge_multipage_tables=self.cfg.merge_multipage_tables)
        self.html_renderer = HtmlRenderer(
            simplified=self.cfg.html_simplified,
            inline_css=self.cfg.html_inline_css,
        )
        self.md_renderer = MarkdownRenderer(
            table_format=self.cfg.md_table_format,
            footnote_style=self.cfg.md_footnote_style,
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def process(self, job: PdfJob) -> DocumentResult:
        """Process a single PDF file end-to-end."""
        start = time.time()
        self.progress_callback("Starting conversion", 0.0)

        work_dir = Path(tempfile.mkdtemp(prefix="pdfconv_"))
        images_dir = work_dir / "images"
        output_images_dir = job.output_dir / "images"

        # 1. Split PDF into chunks
        self.progress_callback("Splitting PDF", 0.05)
        chunks = self.splitter.split(job.input_path, work_dir / "chunks")
        total_pages = self.splitter.get_page_count(job.input_path)

        # 2. Process chunks in parallel
        self.progress_callback("Processing chunks in parallel", 0.10)
        chunk_results = self._process_chunks_parallel(chunks, images_dir, output_images_dir)

        # 3. Merge chunks
        self.progress_callback("Merging results", 0.75)
        all_pages = self.merger.merge(chunk_results)

        # 4. Document-level heading classification
        self.progress_callback("Classifying headings", 0.80)
        all_blocks = [b for p in all_pages for b in p.blocks]
        all_blocks = self.heading_clf.classify(all_blocks)

        # 5. Language correction (after structure is finalized)
        self.progress_callback("Correcting text", 0.85)
        all_blocks = self.correction.correct(all_blocks)

        # 6. Render outputs
        self.progress_callback("Rendering output", 0.90)
        result = DocumentResult(
            source_path=str(job.input_path),
            total_pages=total_pages,
            pages=all_pages,
        )

        if "html" in job.output_formats:
            result.html = self.html_renderer.render(all_pages)
        if "markdown" in job.output_formats:
            result.markdown = self.md_renderer.render(all_pages)

        # 7. Save output files
        self.progress_callback("Saving files", 0.95)
        self._save_outputs(result, job)

        elapsed = time.time() - start
        self.progress_callback(f"Done in {elapsed:.1f}s", 1.0)
        result.metadata["elapsed_seconds"] = elapsed

        return result

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

    # ------------------------------------------------------------------
    # Parallel chunk processing
    # ------------------------------------------------------------------

    def _process_chunks_parallel(
        self,
        chunks: list[PdfChunk],
        images_dir: Path,
        output_images_dir: Path,
    ) -> list[ChunkResult]:
        """Process all chunks using thread pool."""
        results: list[ChunkResult] = []

        with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as executor:
            futures = {}
            for chunk in chunks:
                chunk_img_dir = images_dir / f"chunk_{chunk.chunk_index:04d}"
                future = executor.submit(
                    self._process_single_chunk,
                    chunk,
                    chunk_img_dir,
                    output_images_dir,
                )
                futures[future] = chunk

            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    chunk_result = future.result()
                    results.append(chunk_result)
                    pct = 0.10 + 0.65 * (len(results) / len(chunks))
                    self.progress_callback(
                        f"Chunk {chunk.chunk_index + 1}/{len(chunks)} done",
                        pct,
                    )
                except Exception as exc:
                    logger.error(
                        "Chunk %d failed: %s", chunk.chunk_index, exc
                    )

        return results

    def _process_single_chunk(
        self,
        chunk: PdfChunk,
        images_dir: Path,
        output_images_dir: Path,
    ) -> ChunkResult:
        """Process a single chunk through the full pipeline."""
        # 1. Render pages to images
        page_data_list = self.renderer.render_chunk(chunk, images_dir)

        image_extractor = ImageExtractor(output_dir=output_images_dir)
        page_results: list[PageResult] = []

        for page_data in page_data_list:
            page_idx = page_data["page_index"]
            img_path = page_data["image_path"]
            width = page_data["width"]
            height = page_data["height"]
            digital_blocks = page_data["digital_blocks"]
            lines = page_data["lines"]

            # -- Hybrid digital PDF strategy --
            # If the page has digital text AND structural lines (tables /
            # boxed regions), we must NOT blindly extract text from those
            # regions because the reading order inside tables gets scrambled
            # when borders are stripped away.
            #
            # Instead:
            #   a) Detect which regions contain structural lines (tables,
            #      boxed layouts) → process those via the image-based AI
            #      pipeline (layout detect → OCR → table recognition).
            #   b) For the remaining text-only regions, use the already-
            #      extracted digital text blocks directly.
            #   c) Merge both sets together.

            has_digital_text = bool(digital_blocks)
            structural_regions = self.layout.detect_line_regions(
                lines, page_idx, width, height,
            )
            has_structural = bool(structural_regions)

            if has_digital_text and has_structural:
                # Split digital blocks into "inside structural region" and
                # "outside structural region"
                outside_blocks: list[LayoutBlock] = []
                for db in digital_blocks:
                    if db.bbox and any(
                        db.bbox.overlap_ratio(sr.bbox) > 0.5
                        for sr in structural_regions if sr.bbox
                    ):
                        pass  # skip – will be handled by image pipeline
                    else:
                        outside_blocks.append(db)

                # Image-based pipeline for structural regions only
                layout_blocks = self.layout.detect(
                    img_path, page_idx, None, lines,
                )
                layout_blocks = self.ocr.ocr_page(
                    img_path, layout_blocks, page_idx,
                )
                # Table recognition for image-detected table blocks
                layout_blocks = self.table_rec.recognize_all(
                    img_path, layout_blocks,
                )

                # Merge: keep image-pipeline blocks that overlap structural
                # regions, add digital text blocks for everything else
                merged: list[LayoutBlock] = []
                for lb in layout_blocks:
                    if lb.bbox and any(
                        lb.bbox.overlap_ratio(sr.bbox) > 0.3
                        for sr in structural_regions if sr.bbox
                    ):
                        merged.append(lb)
                merged.extend(outside_blocks)
                layout_blocks = merged
            else:
                # 2. Layout detection (standard path)
                layout_blocks = self.layout.detect(
                    img_path, page_idx, digital_blocks, lines,
                )

                # 3. OCR (for blocks without digital text)
                layout_blocks = self.ocr.ocr_page(
                    img_path, layout_blocks, page_idx,
                )

                # If no blocks detected, try full-page OCR
                if not layout_blocks:
                    layout_blocks = self.ocr.ocr_full_page(img_path, page_idx)

                # 4. Table structure recognition
                layout_blocks = self.table_rec.recognize_all(
                    img_path, layout_blocks,
                )

            # 5. Extract images (figures, equations)
            layout_blocks = image_extractor.extract_images(
                img_path, layout_blocks, page_idx
            )

            # 6. Reading order refinement (line-based zones + fallback)
            layout_blocks = self.reading_order.refine(
                layout_blocks, width, height, img_path, page_idx,
                vector_lines=lines,
            )

            page_results.append(PageResult(
                page_index=page_idx,
                width=width,
                height=height,
                blocks=layout_blocks,
            ))

        return ChunkResult(
            chunk_index=chunk.chunk_index,
            start_page=chunk.start_page,
            end_page=chunk.end_page,
            pages=page_results,
        )

    # ------------------------------------------------------------------
    # Output saving
    # ------------------------------------------------------------------

    def _save_outputs(self, result: DocumentResult, job: PdfJob) -> None:
        """Save HTML and Markdown files to disk."""
        job.output_dir.mkdir(parents=True, exist_ok=True)

        if result.html:
            html_path = job.output_dir / f"{job.filename or 'output'}.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(result.html)
            logger.info("Saved HTML: %s", html_path)

        if result.markdown:
            md_path = job.output_dir / f"{job.filename or 'output'}.md"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(result.markdown)
            logger.info("Saved Markdown: %s", md_path)
