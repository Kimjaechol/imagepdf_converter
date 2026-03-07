"""Main pipeline orchestrator – PDF to HTML/Markdown conversion."""

from __future__ import annotations

import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from .block_integrity import assign_content_ids_and_seq
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
from .unified_vision import UnifiedVisionProcessor

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    pages_per_chunk: int = 10
    max_workers: int = 4
    dpi: int = 300
    output_formats: list[str] = field(default_factory=lambda: ["html", "markdown"])
    # Pipeline mode: "standard" (multi-step) | "unified_vision" (single Gemini call)
    pipeline_mode: str = "unified_vision"
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
            pipeline_mode=pipe.get("mode", "unified_vision"),
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
        self.unified_vision = UnifiedVisionProcessor(
            gemini_model=self.cfg.gemini_model,
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

        # Translation mode logging
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
        output_images_dir = job.output_dir / "images"

        if self.cfg.pipeline_mode == "unified_vision":
            # ── Adaptive processing: prescan first, then smart batching ──
            all_pages, total_pages, page_data_by_idx = (
                self._process_unified_adaptive(
                    job, images_dir, output_images_dir,
                )
            )

            # Dictionary-based correction as safety net
            self.progress_callback("Applying dictionary corrections", 0.82)
            all_blocks = [b for p in all_pages for b in p.blocks]
            all_blocks = self.correction.correct_dictionary_only(all_blocks)

            # Block integrity: validate reading order, assign sequential
            # numbers to tables/figures, link captions, assign stable IDs.
            # MUST run BEFORE image extraction so filenames include seq numbers.
            self.progress_callback("Verifying block integrity", 0.85)
            all_pages = assign_content_ids_and_seq(all_pages)

            # Now extract images with correct sequential numbers in filenames
            self.progress_callback("Extracting images", 0.88)
            image_extractor = ImageExtractor(output_dir=output_images_dir)
            for page in all_pages:
                pd = page_data_by_idx.get(page.page_index)
                if pd:
                    page.blocks = image_extractor.extract_images(
                        pd["image_path"], page.blocks, page.page_index,
                    )
        else:
            # ── Standard mode: fixed chunking + multi-step pipeline ──
            self.progress_callback("Splitting PDF", 0.05)
            chunks = self.splitter.split(job.input_path, work_dir / "chunks")
            total_pages = self.splitter.get_page_count(job.input_path)

            self.progress_callback("Processing chunks in parallel", 0.10)
            chunk_results = self._process_chunks_parallel(
                chunks, images_dir, output_images_dir, job=job,
            )

            self.progress_callback("Merging results", 0.75)
            all_pages = self.merger.merge(chunk_results)

            self.progress_callback("Classifying headings", 0.80)
            all_blocks = [b for p in all_pages for b in p.blocks]
            all_blocks = self.heading_clf.classify(all_blocks)

            self.progress_callback("Correcting text", 0.85)
            all_blocks = self.correction.correct(all_blocks)

        # Render outputs
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

        # Save output files
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
    # Adaptive unified processing (prescan-first, no fixed chunking)
    # ------------------------------------------------------------------

    def _process_unified_adaptive(
        self,
        job: PdfJob,
        images_dir: Path,
        output_images_dir: Path,
    ) -> tuple[list[PageResult], int, dict[int, dict]]:
        """Adaptive processing: prescan ALL pages first, then smart-batch.

        Instead of blindly splitting the PDF into fixed 10-page chunks,
        this method:
          1. Renders ALL pages and runs a fast local prescan (~0.1ms/page)
          2. Classifies every page as TAG=0 (text-only) or TAG=1 (complex)
          3. TAG=0 pages: run local OCR → send only TEXT to Gemini in
             large batches (up to 30 pages).  No images sent.
          4. TAG=1 pages: group consecutive runs; only split into ≤10-page
             sub-batches when a consecutive run exceeds 10 pages.
          5. Process TAG=0 and TAG=1 groups in parallel via thread pool.

        Returns (all_pages, total_page_count, page_data_by_index).
        """
        from .unified_vision import (
            TranslationContext,
            _BATCH_COMPLEX,
            _BATCH_TEXT_ONLY,
        )

        # 1. Render ALL pages from the original PDF
        self.progress_callback("Rendering all pages", 0.05)
        total_pages = self.splitter.get_page_count(job.input_path)
        all_page_data = self.renderer.render_pdf(job.input_path, images_dir)
        logger.info("Rendered %d pages from %s", len(all_page_data), job.input_path)

        # 2. Fast prescan: classify every page as TAG=0 or TAG=1
        self.progress_callback("Pre-scanning page complexity", 0.10)
        classifications = self.unified_vision.prescan_pages(all_page_data)

        tag0_pages = []
        tag1_pages = []
        for pd in all_page_data:
            cls = classifications.get(pd["page_index"])
            if cls and cls.is_complex:
                tag1_pages.append(pd)
            else:
                tag0_pages.append(pd)

        logger.info(
            "Adaptive prescan: %d TAG=0 (text-only), %d TAG=1 (complex) out of %d pages",
            len(tag0_pages), len(tag1_pages), len(all_page_data),
        )

        # 3. Gather digital text + run local OCR on TAG=0 pages without it
        self.progress_callback("Running local OCR on text-only pages", 0.15)
        ocr_blocks: dict[int, list[LayoutBlock]] = {}
        for pd in all_page_data:
            digital_blocks = pd.get("digital_blocks", [])
            if digital_blocks:
                ocr_blocks[pd["page_index"]] = digital_blocks

        for pd in tag0_pages:
            page_idx = pd["page_index"]
            if page_idx not in ocr_blocks:
                local_blocks = self.ocr.ocr_full_page(pd["image_path"], page_idx)
                if local_blocks:
                    ocr_blocks[page_idx] = local_blocks
                    logger.debug(
                        "Local OCR for TAG=0 page %d: %d blocks",
                        page_idx, len(local_blocks),
                    )

        # 4. Build smart batches
        # TAG=0: large batches (text-only, up to _BATCH_TEXT_ONLY per batch)
        # TAG=1: group consecutive pages, split only if >_BATCH_COMPLEX
        tag0_batches = []
        for i in range(0, len(tag0_pages), _BATCH_TEXT_ONLY):
            tag0_batches.append(("tag0", tag0_pages[i : i + _BATCH_TEXT_ONLY]))

        # Group consecutive TAG=1 pages, then split runs > _BATCH_COMPLEX
        tag1_batches = []
        if tag1_pages:
            consecutive_run: list[dict] = [tag1_pages[0]]
            for j in range(1, len(tag1_pages)):
                prev_idx = tag1_pages[j - 1]["page_index"]
                curr_idx = tag1_pages[j]["page_index"]
                if curr_idx == prev_idx + 1:
                    consecutive_run.append(tag1_pages[j])
                else:
                    # End of a consecutive run → split if needed
                    for k in range(0, len(consecutive_run), _BATCH_COMPLEX):
                        tag1_batches.append(("tag1", consecutive_run[k : k + _BATCH_COMPLEX]))
                    consecutive_run = [tag1_pages[j]]
            # Don't forget the last run
            for k in range(0, len(consecutive_run), _BATCH_COMPLEX):
                tag1_batches.append(("tag1", consecutive_run[k : k + _BATCH_COMPLEX]))

        all_batches = tag0_batches + tag1_batches
        logger.info(
            "Smart batching: %d TAG=0 batches, %d TAG=1 batches",
            len(tag0_batches), len(tag1_batches),
        )

        # 5. Translation context
        translation_ctx = None
        if job.translate:
            translation_ctx = TranslationContext(
                enabled=True,
                source_language=job.source_language,
                target_language=job.target_language,
            )

        # 6. Process all batches in parallel via thread pool
        self.progress_callback("Processing batches in parallel", 0.20)
        api_key = os.environ.get("GEMINI_API_KEY", "")

        all_results: list[PageResult] = []
        completed = 0

        # Dynamic worker count: scale with batch count but cap to avoid
        # memory pressure (~15MB/worker for 5-page image batches) and
        # API burst issues. Gemini Pay-as-you-go allows ~2000 RPM so
        # the real limit is local resources, not API rate.
        effective_workers = min(
            max(self.cfg.max_workers, len(all_batches)),
            20,
        )
        logger.info(
            "Parallel workers: %d (configured=%d, batches=%d)",
            effective_workers, self.cfg.max_workers, len(all_batches),
        )

        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures = {}
            for batch_tag, batch_pages in all_batches:
                if batch_tag == "tag0":
                    future = executor.submit(
                        self.unified_vision._process_text_only_batch,
                        batch_pages, ocr_blocks, api_key, classifications,
                        translation_ctx,
                    )
                else:
                    future = executor.submit(
                        self.unified_vision._process_complex_batch,
                        batch_pages, ocr_blocks, api_key, classifications,
                        translation_ctx,
                    )
                futures[future] = (batch_tag, batch_pages)

            for future in as_completed(futures):
                batch_tag, batch_pages = futures[future]
                try:
                    batch_results = future.result()
                    all_results.extend(batch_results)
                except Exception as exc:
                    page_ids = [p["page_index"] for p in batch_pages]
                    logger.error(
                        "%s batch (pages %s) failed: %s",
                        batch_tag.upper(), page_ids, exc,
                    )
                completed += 1
                pct = 0.20 + 0.50 * (completed / max(len(all_batches), 1))
                self.progress_callback(
                    f"Batch {completed}/{len(all_batches)} done ({batch_tag})",
                    pct,
                )

        # ── SAFETY LAYER 1: Detect missing & duplicate pages ──
        self.progress_callback("Verifying page integrity", 0.71)
        expected_indices = {pd["page_index"] for pd in all_page_data}
        result_index_map: dict[int, PageResult] = {}

        for pr in all_results:
            if pr.page_index in result_index_map:
                # Duplicate: keep the one with more blocks (richer content)
                existing = result_index_map[pr.page_index]
                if len(pr.blocks) > len(existing.blocks):
                    logger.warning(
                        "Duplicate page %d: replacing (%d blocks → %d blocks)",
                        pr.page_index, len(existing.blocks), len(pr.blocks),
                    )
                    result_index_map[pr.page_index] = pr
                else:
                    logger.warning(
                        "Duplicate page %d: keeping first (%d blocks), "
                        "discarding duplicate (%d blocks)",
                        pr.page_index, len(existing.blocks), len(pr.blocks),
                    )
            elif pr.page_index in expected_indices:
                result_index_map[pr.page_index] = pr
            else:
                # Page index not in our document → discard
                logger.warning(
                    "Discarding result with unexpected page_index %d "
                    "(not in document pages %s)",
                    pr.page_index, sorted(expected_indices),
                )

        # ── SAFETY LAYER 2: Create fallback for missing pages ──
        page_data_by_idx = {pd["page_index"]: pd for pd in all_page_data}
        missing_indices = expected_indices - set(result_index_map.keys())

        if missing_indices:
            logger.warning(
                "MISSING PAGES detected: %s out of %d total. "
                "Creating fallback results from OCR/digital text.",
                sorted(missing_indices), len(expected_indices),
            )
            for miss_idx in missing_indices:
                pd = page_data_by_idx[miss_idx]
                # Build fallback PageResult from available OCR/digital text
                fallback_blocks: list[LayoutBlock] = []
                if miss_idx in ocr_blocks:
                    fallback_blocks = list(ocr_blocks[miss_idx])
                    logger.info(
                        "Fallback for page %d: using %d OCR blocks",
                        miss_idx, len(fallback_blocks),
                    )
                else:
                    logger.warning(
                        "Fallback for page %d: no text available, "
                        "page will be empty in output",
                        miss_idx,
                    )

                result_index_map[miss_idx] = PageResult(
                    page_index=miss_idx,
                    width=pd["width"],
                    height=pd["height"],
                    blocks=fallback_blocks,
                )

        # NOTE: Image extraction (cropping figures/equations from pages) is
        # deferred to process() AFTER assign_content_ids_and_seq(), so that
        # image filenames include correct sequential numbers (Table 1, Figure 2).
        # Store page_data mapping for later use.

        # ── SAFETY LAYER 3: Final integrity check ──
        # Build results sorted by page_index (guaranteed document order)
        all_results_verified = [
            result_index_map[idx]
            for idx in sorted(result_index_map.keys())
        ]

        # Verify 1:1 correspondence with input pages
        final_indices = [pr.page_index for pr in all_results_verified]
        if final_indices != sorted(expected_indices):
            logger.error(
                "CRITICAL: Final page indices %s do not match expected %s. "
                "Document integrity may be compromised!",
                final_indices, sorted(expected_indices),
            )

        if len(all_results_verified) != len(expected_indices):
            logger.error(
                "CRITICAL: Final page count (%d) != expected (%d). "
                "Document may have missing or extra pages!",
                len(all_results_verified), len(expected_indices),
            )

        logger.info(
            "Page integrity verified: %d/%d pages present, %d recovered via fallback",
            len(all_results_verified), len(expected_indices), len(missing_indices),
        )

        return all_results_verified, total_pages, page_data_by_idx

    # ------------------------------------------------------------------
    # Parallel chunk processing (used by standard mode)
    # ------------------------------------------------------------------

    def _process_chunks_parallel(
        self,
        chunks: list[PdfChunk],
        images_dir: Path,
        output_images_dir: Path,
        job: PdfJob | None = None,
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
                    job,
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
        job: PdfJob | None = None,
    ) -> ChunkResult:
        """Process a single chunk through the pipeline."""
        if self.cfg.pipeline_mode == "unified_vision":
            return self._process_chunk_unified(chunk, images_dir, output_images_dir, job)
        return self._process_chunk_standard(chunk, images_dir, output_images_dir)

    def _process_chunk_unified(
        self,
        chunk: PdfChunk,
        images_dir: Path,
        output_images_dir: Path,
        job: PdfJob | None = None,
    ) -> ChunkResult:
        """Process a chunk via unified Gemini call with TAG=0/1 optimization.

        Steps:
          1. Render pages to images
          2. Fast local pre-scan → classify TAG=0 (text) or TAG=1 (complex)
          3. TAG=0 pages: run local Surya OCR first → send TEXT ONLY to Gemini
          4. TAG=1 pages: send page IMAGES to Gemini for full vision analysis
          5. Extract figure/equation images locally

        When translation is enabled (job.translate=True), ALL pages are sent
        to Gemini with translation instructions embedded in the prompt.
        TAG=0 pages still send only text (no images), keeping costs low.
        """
        page_data_list = self.renderer.render_chunk(chunk, images_dir)

        # Gather digital text (from PDF text layer) as baseline hints
        ocr_blocks: dict[int, list[LayoutBlock]] = {}
        for pd in page_data_list:
            digital_blocks = pd.get("digital_blocks", [])
            if digital_blocks:
                ocr_blocks[pd["page_index"]] = digital_blocks

        # Pre-scan to identify TAG=0 pages that need local OCR
        classifications = self.unified_vision.prescan_pages(page_data_list)

        # Run local OCR (Surya) on TAG=0 pages that lack digital text
        for pd in page_data_list:
            page_idx = pd["page_index"]
            cls = classifications.get(page_idx)
            if cls and not cls.is_complex and page_idx not in ocr_blocks:
                # TAG=0 page without digital text → run local OCR
                img_path = pd["image_path"]
                local_blocks = self.ocr.ocr_full_page(img_path, page_idx)
                if local_blocks:
                    ocr_blocks[page_idx] = local_blocks
                    logger.debug(
                        "Local OCR for TAG=0 page %d: %d blocks",
                        page_idx, len(local_blocks),
                    )

        # Translation options from job
        translate = job.translate if job else False
        source_language = job.source_language if job else ""
        target_language = job.target_language if job else "ko"

        # Unified processing: TAG=0 → text only, TAG=1 → vision
        # Translation instructions are embedded in the same prompt
        page_results = self.unified_vision.process_pages(
            page_data_list,
            ocr_blocks,
            translate=translate,
            source_language=source_language,
            target_language=target_language,
        )

        # Fallback: if unified vision failed, use standard pipeline
        if not page_results:
            logger.warning(
                "Unified vision failed for chunk %d, falling back to standard.",
                chunk.chunk_index,
            )
            return self._process_chunk_standard(chunk, images_dir, output_images_dir)

        # Extract images (figures, equations) – still done locally
        image_extractor = ImageExtractor(output_dir=output_images_dir)
        page_data_by_idx = {pd["page_index"]: pd for pd in page_data_list}
        for pr in page_results:
            pd = page_data_by_idx.get(pr.page_index)
            if pd:
                img_path = pd["image_path"]
                pr.blocks = image_extractor.extract_images(
                    img_path, pr.blocks, pr.page_index
                )

        return ChunkResult(
            chunk_index=chunk.chunk_index,
            start_page=chunk.start_page,
            end_page=chunk.end_page,
            pages=page_results,
        )

    def _process_chunk_standard(
        self,
        chunk: PdfChunk,
        images_dir: Path,
        output_images_dir: Path,
    ) -> ChunkResult:
        """Process a single chunk through the standard multi-step pipeline."""
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
