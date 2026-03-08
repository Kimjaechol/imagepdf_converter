"""FastAPI server – bridges the Electron frontend with the Python pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.core.pipeline import Pipeline, PipelineConfig
from backend.models.schema import PdfJob
from backend.services.credit_service import CreditService
from backend.services.auth_service import AuthService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PDF Converter API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_config: PipelineConfig | None = None
_jobs: dict[str, dict[str, Any]] = {}  # job_id -> status
_websockets: dict[str, WebSocket] = {}
_credit_service: CreditService | None = None
_auth_service: AuthService | None = None


def _get_credit_service() -> CreditService:
    global _credit_service
    if _credit_service is None:
        _credit_service = CreditService(data_dir="data")
    return _credit_service


def _get_auth_service() -> AuthService:
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService(data_dir="data")
    return _auth_service


async def get_current_user(authorization: str = Header(default="")) -> dict:
    """Extract and verify the user from the Authorization header."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid authorization header")
    token = authorization[7:]
    auth = _get_auth_service()
    payload = auth.verify_token(token)
    if not payload:
        raise HTTPException(401, "Invalid or expired token")
    return payload


def _get_config() -> PipelineConfig:
    global _config
    if _config is None:
        config_path = os.environ.get("PIPELINE_CONFIG", "config/pipeline_config.yaml")
        if os.path.exists(config_path):
            _config = PipelineConfig.from_yaml(config_path)
        else:
            _config = PipelineConfig()
    return _config


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ConvertRequest(BaseModel):
    input_path: str
    output_dir: str
    output_formats: list[str] = ["html", "markdown"]
    # Translation options: set translate=True and specify languages
    translate: bool = False
    source_language: str = ""      # empty = auto-detect (e.g. "ja", "en", "zh")
    target_language: str = "ko"    # e.g. "ko", "en", "ja"
    # Auth: user_id injected by the endpoint handler
    user_id: str = ""


class BatchConvertRequest(BaseModel):
    folder_path: str
    output_dir: str
    recursive: bool = False
    output_formats: list[str] = ["html", "markdown"]


class ConfigUpdate(BaseModel):
    key: str
    value: Any


class CustomTermRequest(BaseModel):
    correct: str
    confused_with: list[str]


class SetApiKeyRequest(BaseModel):
    api_key: str


class SetUpstageApiKeyRequest(BaseModel):
    api_key: str


class SetPipelineModeRequest(BaseModel):
    mode: str  # "standard" | "unified_vision" | "upstage_hybrid"


class TranslateHtmlRequest(BaseModel):
    html: str
    source_language: str = ""
    target_language: str = "ko"


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class PurchaseCreditsRequest(BaseModel):
    amount_usd: float


class CreateCheckoutRequest(BaseModel):
    amount_usd: float


class EstimateCostRequest(BaseModel):
    num_pages: int
    doc_type: str = "image_pdf"  # "image_pdf" | "digital_pdf" | "other"


class DocumentConvertRequest(BaseModel):
    input_path: str
    output_dir: str
    output_formats: list[str] = ["html", "markdown"]
    refine_with_gemini: bool = True  # Enable Gemini post-processing (PDF only)
    translate: bool = False
    source_language: str = ""
    target_language: str = "ko"


class BatchDocumentConvertRequest(BaseModel):
    input_paths: list[str]
    output_dir: str
    output_formats: list[str] = ["html", "markdown"]
    refine_with_gemini: bool = True  # For PDF files in batch


class JobStatus(BaseModel):
    job_id: str
    status: str  # pending | processing | completed | error
    progress: float  # 0.0 - 1.0
    message: str
    result: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/languages")
async def get_supported_languages():
    """Return supported translation languages."""
    return {
        "languages": [
            {"code": "", "name": "Auto-detect", "name_native": "자동 감지"},
            {"code": "ko", "name": "Korean", "name_native": "한국어"},
            {"code": "en", "name": "English", "name_native": "English"},
            {"code": "ja", "name": "Japanese", "name_native": "日本語"},
            {"code": "zh", "name": "Chinese", "name_native": "中文"},
            {"code": "de", "name": "German", "name_native": "Deutsch"},
            {"code": "fr", "name": "French", "name_native": "Français"},
            {"code": "es", "name": "Spanish", "name_native": "Español"},
            {"code": "vi", "name": "Vietnamese", "name_native": "Tiếng Việt"},
            {"code": "th", "name": "Thai", "name_native": "ไทย"},
            {"code": "ru", "name": "Russian", "name_native": "Русский"},
            {"code": "pt", "name": "Portuguese", "name_native": "Português"},
            {"code": "it", "name": "Italian", "name_native": "Italiano"},
            {"code": "ar", "name": "Arabic", "name_native": "العربية"},
            {"code": "id", "name": "Indonesian", "name_native": "Bahasa Indonesia"},
        ],
    }


@app.get("/api/config")
async def get_config():
    cfg = _get_config()
    return {
        "pages_per_chunk": cfg.pages_per_chunk,
        "max_workers": cfg.max_workers,
        "dpi": cfg.dpi,
        "output_formats": cfg.output_formats,
        "pipeline_mode": cfg.pipeline_mode,
        "layout_engine": cfg.layout_engine,
        "ocr_engine": cfg.ocr_engine,
        "reading_order_mode": cfg.reading_order_mode,
        "heading_mode": cfg.heading_mode,
        "correction_mode": cfg.correction_mode,
        "correction_aggressiveness": cfg.correction_aggressiveness,
        "upstage_mode": cfg.upstage_mode,
        "gemini_visual_batch_size": cfg.gemini_visual_batch_size,
    }


@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    cfg = _get_config()
    if hasattr(cfg, update.key):
        setattr(cfg, update.key, update.value)
        return {"status": "ok", "key": update.key, "value": update.value}
    raise HTTPException(400, f"Unknown config key: {update.key}")


@app.post("/api/convert", response_model=JobStatus)
async def convert_single(req: ConvertRequest, user: dict = Depends(get_current_user)):
    """Start a single PDF conversion job (authenticated, credit-checked)."""
    req.user_id = user["user_id"]

    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {
        "status": "pending",
        "progress": 0.0,
        "message": "Queued",
        "result": None,
    }

    # Run in background
    asyncio.get_event_loop().run_in_executor(
        None, _run_conversion, job_id, req
    )

    return JobStatus(
        job_id=job_id,
        status="pending",
        progress=0.0,
        message="Job queued",
    )


@app.post("/api/convert/batch", response_model=JobStatus)
async def convert_batch(req: BatchConvertRequest):
    """Start a batch conversion job for an entire folder."""
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {
        "status": "pending",
        "progress": 0.0,
        "message": "Queued",
        "result": None,
    }

    asyncio.get_event_loop().run_in_executor(
        None, _run_batch_conversion, job_id, req
    )

    return JobStatus(
        job_id=job_id,
        status="pending",
        progress=0.0,
        message="Batch job queued",
    )


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    j = _jobs[job_id]
    return JobStatus(
        job_id=job_id,
        status=j["status"],
        progress=j["progress"],
        message=j["message"],
        result=j.get("result"),
    )


@app.get("/api/jobs")
async def list_jobs():
    return {
        jid: {
            "status": j["status"],
            "progress": j["progress"],
            "message": j["message"],
        }
        for jid, j in _jobs.items()
    }


@app.post("/api/dictionary/add")
async def add_dictionary_term(req: CustomTermRequest):
    """Add a custom correction term to the dictionary."""
    cfg = _get_config()
    pipeline = Pipeline(config=cfg)
    pipeline.correction.add_custom_term(req.correct, req.confused_with)
    pipeline.correction.save_dictionary(cfg.correction_dict_path)
    return {"status": "ok", "term": req.correct}


# ---------------------------------------------------------------------------
# API Key Management (Operator)
# ---------------------------------------------------------------------------

def _api_key_file() -> Path:
    """Return the path to the persisted API key file."""
    p = Path("data")
    p.mkdir(parents=True, exist_ok=True)
    return p / "api_key.txt"


def _load_persisted_api_key() -> None:
    """Load the API key from disk into os.environ on startup."""
    f = _api_key_file()
    if f.exists():
        key = f.read_text(encoding="utf-8").strip()
        if key:
            os.environ["GEMINI_API_KEY"] = key
            logger.info("Loaded persisted Gemini API key (%s...)", key[:4])


# Load on module import so the key is available immediately
_load_persisted_api_key()


@app.post("/api/settings/api-key")
async def set_api_key(req: SetApiKeyRequest):
    """Set the Gemini API key (operator only). Persisted to disk."""
    os.environ["GEMINI_API_KEY"] = req.api_key
    # Persist to file so it survives restarts
    _api_key_file().write_text(req.api_key, encoding="utf-8")
    logger.info("API key saved and persisted")
    return {"status": "ok", "message": "API key configured"}


@app.get("/api/settings/api-key/status")
async def get_api_key_status():
    """Check if a Gemini API key is configured."""
    key = os.environ.get("GEMINI_API_KEY", "")
    return {
        "configured": bool(key),
        "masked": f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "",
    }


# ---------------------------------------------------------------------------
# Upstage API Key Management
# ---------------------------------------------------------------------------

def _upstage_api_key_file() -> Path:
    """Return the path to the persisted Upstage API key file."""
    p = Path("data")
    p.mkdir(parents=True, exist_ok=True)
    return p / "upstage_api_key.txt"


def _load_persisted_upstage_api_key() -> None:
    """Load the Upstage API key from disk into os.environ on startup."""
    f = _upstage_api_key_file()
    if f.exists():
        key = f.read_text(encoding="utf-8").strip()
        if key:
            os.environ["UPSTAGE_API_KEY"] = key
            logger.info("Loaded persisted Upstage API key (%s...)", key[:4])


_load_persisted_upstage_api_key()


@app.post("/api/settings/upstage-api-key")
async def set_upstage_api_key(req: SetUpstageApiKeyRequest):
    """Set the Upstage API key. Persisted to disk."""
    os.environ["UPSTAGE_API_KEY"] = req.api_key
    _upstage_api_key_file().write_text(req.api_key, encoding="utf-8")
    logger.info("Upstage API key saved and persisted")
    return {"status": "ok", "message": "Upstage API key configured"}


@app.get("/api/settings/upstage-api-key/status")
async def get_upstage_api_key_status():
    """Check if an Upstage API key is configured."""
    key = os.environ.get("UPSTAGE_API_KEY", "")
    return {
        "configured": bool(key),
        "masked": f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "",
    }


# ---------------------------------------------------------------------------
# Pipeline Mode Management
# ---------------------------------------------------------------------------

@app.post("/api/settings/pipeline-mode")
async def set_pipeline_mode(req: SetPipelineModeRequest):
    """Set the conversion pipeline mode.

    Available modes:
    - "standard": Multi-step local pipeline (layout→OCR→table→heading→correction)
    - "unified_vision": Single Gemini call with TAG=0/1 optimization (default)
    - "upstage_hybrid": Upstage Document Parse + Gemini visual comparison (highest accuracy)
    """
    valid_modes = {"standard", "unified_vision", "upstage_hybrid"}
    if req.mode not in valid_modes:
        raise HTTPException(400, f"Invalid mode. Must be one of: {valid_modes}")

    cfg = _get_config()
    cfg.pipeline_mode = req.mode
    logger.info("Pipeline mode set to: %s", req.mode)
    return {"status": "ok", "mode": req.mode}


@app.get("/api/settings/pipeline-mode")
async def get_pipeline_mode():
    """Get the current pipeline mode and available modes."""
    cfg = _get_config()
    upstage_key = os.environ.get("UPSTAGE_API_KEY", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    return {
        "current_mode": cfg.pipeline_mode,
        "available_modes": [
            {
                "id": "standard",
                "name": "Standard (Local)",
                "description": "Multi-step local pipeline. No API key needed.",
                "available": True,
            },
            {
                "id": "unified_vision",
                "name": "Gemini Vision",
                "description": "Single Gemini call with smart batching. Requires Gemini API key.",
                "available": bool(gemini_key),
            },
            {
                "id": "upstage_hybrid",
                "name": "Upstage + Gemini Hybrid (Highest Accuracy)",
                "description": (
                    "Upstage Document Parse for OCR/layout + Gemini for visual comparison. "
                    "Requires both Upstage and Gemini API keys for scanned PDFs. "
                    "Digital PDFs need only Gemini API key."
                ),
                "available": bool(gemini_key),
                "upstage_configured": bool(upstage_key),
            },
        ],
    }


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class BidiCheckRequest(BaseModel):
    pdf_path: str


@app.post("/api/diagnostics/bidi-check")
async def bidi_check(req: BidiCheckRequest):
    """Run BiDi numeral displacement diagnostic on a PDF.

    Verifies that PyMuPDF's MuPDF glyph width fix is working correctly
    for CJK + Arabic numeral mixed text.
    """
    pdf_path = Path(req.pdf_path)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")

    try:
        from backend.core.digital_pdf_extractor import DigitalPdfExtractor
        extractor = DigitalPdfExtractor()
        report = await asyncio.get_event_loop().run_in_executor(
            None, extractor.verify_bidi_fix, pdf_path,
        )
        return report
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"PyMuPDF not installed: {exc}",
        )
    except Exception as exc:
        logger.error("BiDi diagnostic failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/diagnostics/pymupdf-version")
async def pymupdf_version():
    """Check PyMuPDF version and whether it includes the BiDi fix."""
    try:
        from backend.core.digital_pdf_extractor import DigitalPdfExtractor
        extractor = DigitalPdfExtractor()
        is_fixed, version_str = extractor._check_pymupdf_version()
        return {
            "pymupdf_version": version_str,
            "bidi_fix_included": is_fixed,
            "minimum_required": "1.25.3",
            "recommendation": (
                "Version OK" if is_fixed
                else "Upgrade required: pip install --upgrade pymupdf"
            ),
        }
    except ImportError:
        return {
            "pymupdf_version": "not installed",
            "bidi_fix_included": False,
            "minimum_required": "1.25.3",
            "recommendation": "Install PyMuPDF: pip install pymupdf>=1.25.3",
        }


# ---------------------------------------------------------------------------
# HTML Translation (for non-PDF documents)
# ---------------------------------------------------------------------------

@app.post("/api/translate-html")
async def translate_html(req: TranslateHtmlRequest):
    """Translate HTML content via Gemini while preserving HTML tags."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="Gemini API key not configured")

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _translate_html_sync, req.html, req.source_language,
            req.target_language, api_key,
        )
        return {"translated_html": result}
    except Exception as exc:
        logger.error("Translation failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


def _translate_html_sync(
    html: str, source_language: str, target_language: str, api_key: str,
) -> str:
    """Translate HTML using Gemini, preserving all HTML structure."""
    import google.generativeai as genai

    genai.configure(api_key=api_key)

    cfg = _get_config()
    model = genai.GenerativeModel(cfg.gemini_model)

    src = source_language or "auto-detected"
    lang_names = {
        "ko": "Korean", "en": "English", "ja": "Japanese",
        "zh": "Chinese", "de": "German", "fr": "French",
        "es": "Spanish", "vi": "Vietnamese", "th": "Thai",
        "ru": "Russian", "pt": "Portuguese",
    }
    tgt_name = lang_names.get(target_language, target_language)

    # Split HTML into chunks if too large (Gemini has token limits)
    # Process <body> content only, preserving head/style
    body_start = html.find("<body")
    body_end = html.rfind("</body>")

    if body_start == -1 or body_end == -1:
        # No body tags, translate the whole thing
        head_part = ""
        body_content = html
        tail_part = ""
    else:
        body_tag_end = html.index(">", body_start) + 1
        head_part = html[:body_tag_end]
        body_content = html[body_tag_end:body_end]
        tail_part = html[body_end:]

    # Chunk body content for large documents
    max_chunk = 30000  # characters per chunk
    chunks = []
    if len(body_content) <= max_chunk:
        chunks = [body_content]
    else:
        # Split on block-level tags to avoid breaking mid-tag
        import re
        parts = re.split(r'(?=<(?:div|h[1-6]|p|table|section)[\s>])', body_content)
        current = ""
        for part in parts:
            if len(current) + len(part) > max_chunk and current:
                chunks.append(current)
                current = part
            else:
                current += part
        if current:
            chunks.append(current)

    translated_chunks = []
    for chunk in chunks:
        prompt = f"""Translate the following HTML content from {src} to {tgt_name}.

CRITICAL RULES:
1. Preserve ALL HTML tags, attributes, and structure EXACTLY as they are
2. Only translate the visible text content between tags
3. Do NOT translate tag names, attribute names, attribute values, CSS, or JavaScript
4. Do NOT add, remove, or modify any HTML tags
5. Do NOT wrap the output in code fences or add any explanation
6. Preserve all whitespace and line breaks in the original
7. If text is already in {tgt_name}, keep it unchanged

HTML to translate:
{chunk}"""

        response = model.generate_content(prompt)
        translated = response.text.strip()

        # Remove markdown code fences if Gemini added them
        if translated.startswith("```html"):
            translated = translated[7:]
        elif translated.startswith("```"):
            translated = translated[3:]
        if translated.endswith("```"):
            translated = translated[:-3]

        translated_chunks.append(translated.strip())

    translated_body = "\n".join(translated_chunks)
    return head_part + translated_body + tail_part


# ---------------------------------------------------------------------------
# Document Conversion (한컴 DocsConverter for non-PDF)
# ---------------------------------------------------------------------------

@app.get("/api/hancom/status")
async def hancom_status():
    """Check if Hancom DocsConverter server is reachable."""
    try:
        from backend.core.hancom_converter import (
            is_hancom_available,
            _hancom_base_url,
            SUPPORTED_EXTENSIONS,
        )
        available = is_hancom_available()
        return {
            "available": available,
            "server_url": _hancom_base_url(),
            "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        }
    except Exception:
        return {"available": False, "server_url": None, "supported_extensions": []}


@app.post("/api/convert/document/batch")
async def convert_document_batch(req: BatchDocumentConvertRequest):
    """Batch-convert multiple documents using Hancom DocsConverter (non-PDF).

    Files are uploaded to the remote Hancom server and converted via REST API.
    """
    if not req.input_paths:
        return {"results": [], "total": 0}

    # Validate all files exist and have supported extensions
    from backend.core.hancom_converter import SUPPORTED_EXTENSIONS
    for p in req.input_paths:
        fp = Path(p)
        if not fp.exists():
            raise HTTPException(404, f"File not found: {p}")
        ext = fp.suffix.lower().lstrip(".")
        if ext not in SUPPORTED_EXTENSIONS:
            raise HTTPException(
                400, f"Unsupported format: {fp.name} (.{ext})"
            )

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            _batch_convert_sync,
            req.input_paths,
            req.output_dir,
            req.output_formats,
        )
        return result
    except Exception as e:
        logger.error("Batch document conversion failed: %s", e)
        raise HTTPException(500, f"Batch conversion failed: {e}")


def _batch_convert_sync(
    input_paths: list[str],
    output_dir: str,
    output_formats: list[str],
) -> dict:
    """Run Hancom batch conversion, then generate markdown for each."""
    from backend.core.hancom_converter import convert_batch, clean_hancom_html

    start = time.time()

    # Step 1: Hancom DocsConverter conversion
    hancom_results = convert_batch(input_paths, output_dir)

    # Step 2: Post-process each result (cleanup + markdown)
    final_results = []
    want_html = "html" in output_formats
    want_md = "markdown" in output_formats or "md" in output_formats

    for i, hc in enumerate(hancom_results):
        path = input_paths[i]
        stem = Path(path).stem
        file_out = Path(output_dir) / stem

        if hc.get("error"):
            final_results.append({
                "input_path": path,
                "error": hc["error"],
                "output_files": [],
            })
            continue

        html = clean_hancom_html(hc["html"])

        output_files = []
        if want_html:
            file_out.mkdir(parents=True, exist_ok=True)
            html_path = file_out / f"{stem}.html"
            html_path.write_text(html, encoding="utf-8")
            output_files.append(str(html_path))

        if want_md:
            file_out.mkdir(parents=True, exist_ok=True)
            md = _basic_html_to_markdown(html)
            md_path = file_out / f"{stem}.md"
            md_path.write_text(md, encoding="utf-8")
            output_files.append(str(md_path))

        final_results.append({
            "input_path": path,
            "output_files": output_files,
            "engine": "hancom",
            "elapsed_seconds": hc.get("elapsed_seconds", 0),
        })

    elapsed = round(time.time() - start, 2)
    succeeded = sum(1 for r in final_results if "error" not in r)
    failed = len(final_results) - succeeded

    return {
        "results": final_results,
        "total": len(input_paths),
        "succeeded": succeeded,
        "failed": failed,
        "elapsed_seconds": elapsed,
        "files_per_second": round(len(input_paths) / elapsed, 2) if elapsed > 0 else 0,
    }


@app.post("/api/convert/document")
async def convert_document(req: DocumentConvertRequest):
    """Convert a document to HTML.

    Non-PDF (HWP/HWPX/DOC/DOCX/XLS/XLSX/PPT/PPTX): uses 한컴 DocsConverter.
    """
    input_path = Path(req.input_path)
    if not input_path.exists():
        raise HTTPException(404, f"File not found: {req.input_path}")

    ext = input_path.suffix.lower().lstrip(".")

    from backend.core.hancom_converter import SUPPORTED_EXTENSIONS
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported format: .{ext}. Use /api/convert for PDFs.")

    output_dir = Path(req.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            _convert_document_sync,
            str(input_path),
            str(output_dir),
            req.output_formats,
            req.translate,
            req.source_language,
            req.target_language,
        )
        return result
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except TimeoutError as e:
        raise HTTPException(504, str(e))
    except Exception as e:
        logger.error("Document conversion failed: %s", e)
        raise HTTPException(500, f"Conversion failed: {e}")


def _convert_document_sync(
    input_path: str,
    output_dir: str,
    output_formats: list[str],
    translate: bool,
    source_language: str,
    target_language: str,
) -> dict:
    """Synchronous document conversion via Hancom DocsConverter."""
    from backend.core.hancom_converter import convert_to_html, clean_hancom_html

    stem = Path(input_path).stem

    # Step 1: Hancom DocsConverter conversion
    hc_result = convert_to_html(input_path, output_dir)
    html = hc_result["html"]
    images = hc_result["images"]
    elapsed = hc_result["elapsed_seconds"]

    # Step 2: Basic cleanup
    html = clean_hancom_html(html)

    # Step 3: Translation (optional)
    translated = False
    if translate and os.environ.get("GEMINI_API_KEY"):
        try:
            api_key = os.environ["GEMINI_API_KEY"]
            html = _translate_html_sync(html, source_language, target_language, api_key)
            translated = True
        except Exception as e:
            logger.warning("Translation failed: %s", e)

    # Step 4: Save final HTML
    output_files = []
    want_html = "html" in output_formats
    want_md = "markdown" in output_formats or "md" in output_formats

    if want_html:
        html_path = Path(output_dir) / f"{stem}.html"
        html_path.write_text(html, encoding="utf-8")
        output_files.append(str(html_path))

    # Step 5: Generate Markdown
    markdown = None
    if want_md:
        from backend.core.md_renderer import html_to_markdown
        try:
            markdown = html_to_markdown(html)
        except Exception:
            markdown = _basic_html_to_markdown(html)
        md_path = Path(output_dir) / f"{stem}.md"
        md_path.write_text(markdown, encoding="utf-8")
        output_files.append(str(md_path))

    # Collect image paths
    image_paths = []
    img_dir = Path(output_dir) / "images"
    if img_dir.exists():
        image_paths = [str(p) for p in img_dir.iterdir() if p.is_file()]

    return {
        "html": html if want_html else None,
        "markdown": markdown,
        "output_files": output_files,
        "images": image_paths,
        "page_count": None,
        "title": stem,
        "author": None,
        "engine": "hancom",
        "translated": translated,
        "elapsed_seconds": elapsed,
    }


def _basic_html_to_markdown(html: str) -> str:
    """Minimal HTML to Markdown fallback if md_renderer is unavailable."""
    import re
    text = html
    # Remove head/style
    text = re.sub(r'<head>.*?</head>', '', text, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    # Headings
    for i in range(1, 7):
        text = re.sub(rf'<h{i}[^>]*>(.*?)</h{i}>', rf'\n{"#" * i} \1\n', text, flags=re.DOTALL)
    # Bold/italic
    text = re.sub(r'<(?:strong|b)>(.*?)</(?:strong|b)>', r'**\1**', text)
    text = re.sub(r'<(?:em|i)>(.*?)</(?:em|i)>', r'*\1*', text)
    # Paragraphs
    text = re.sub(r'<p[^>]*>(.*?)</p>', r'\n\1\n', text, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text)
    # Strip remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Clean entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&nbsp;', ' ').replace('&quot;', '"')
    # Clean whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    """Register a new user account."""
    auth = _get_auth_service()
    try:
        result = auth.register(req.email, req.password, req.display_name)
        # Auto-create credit account
        _get_credit_service().get_or_create_account(result["user_id"])
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    """Log in and get a token."""
    auth = _get_auth_service()
    try:
        return auth.login(req.email, req.password)
    except ValueError as e:
        raise HTTPException(401, str(e))


@app.get("/api/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    """Get current user info."""
    auth = _get_auth_service()
    info = auth.get_user(user["user_id"])
    if not info:
        raise HTTPException(404, "User not found")
    return info


# ---------------------------------------------------------------------------
# Credit System (authenticated)
# ---------------------------------------------------------------------------

@app.get("/api/credits")
async def get_credits(user: dict = Depends(get_current_user)):
    """Get the current user's credit balance."""
    svc = _get_credit_service()
    acct = svc.get_or_create_account(user["user_id"])
    return {
        "user_id": acct.user_id,
        "balance_usd": round(acct.balance_usd, 4),
        "total_purchased_usd": round(acct.total_purchased_usd, 4),
        "total_consumed_usd": round(acct.total_consumed_usd, 4),
    }


@app.post("/api/credits/purchase")
async def purchase_credits(req: PurchaseCreditsRequest, user: dict = Depends(get_current_user)):
    """Add credits to the current user's balance (manual top-up for testing)."""
    if req.amount_usd <= 0:
        raise HTTPException(400, "Amount must be positive")
    svc = _get_credit_service()
    new_balance = svc.purchase_credits(user["user_id"], req.amount_usd)
    return {
        "amount_usd": req.amount_usd,
        "new_balance_usd": round(new_balance, 4),
    }


@app.post("/api/credits/estimate")
async def estimate_cost(req: EstimateCostRequest):
    """Estimate the credit cost for converting N pages (public, no auth needed)."""
    svc = _get_credit_service()
    return svc.estimate_cost(req.num_pages, doc_type=req.doc_type)


@app.get("/api/credits/pricing")
async def get_pricing():
    """Return the public pricing table (no raw costs exposed)."""
    from backend.services.credit_service import (
        PRICE_IMAGE_PDF_PER_PAGE,
        PRICE_DIGITAL_PDF_PER_PAGE,
        PRICE_OTHER_PER_PAGE,
    )
    return {
        "pricing": [
            {"doc_type": "image_pdf", "label": "Image PDF (scanned)", "per_page_usd": PRICE_IMAGE_PDF_PER_PAGE},
            {"doc_type": "digital_pdf", "label": "Digital PDF", "per_page_usd": PRICE_DIGITAL_PDF_PER_PAGE},
            {"doc_type": "other", "label": "HWP / HWPX / DOC / DOCX / XLS / XLSX / PPT / PPTX", "per_page_usd": PRICE_OTHER_PER_PAGE},
        ]
    }


@app.get("/api/credits/history")
async def get_credit_history(user: dict = Depends(get_current_user), limit: int = 50):
    """Get the current user's recent credit usage history."""
    svc = _get_credit_service()
    acct = svc.get_or_create_account(user["user_id"])
    history = acct.usage_history[-limit:]
    history.reverse()
    return {"history": history}


# ---------------------------------------------------------------------------
# Stripe Checkout (payment integration)
# ---------------------------------------------------------------------------

@app.post("/api/payments/create-checkout")
async def create_checkout(req: CreateCheckoutRequest, user: dict = Depends(get_current_user)):
    """Create a Stripe Checkout session for credit purchase."""
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        raise HTTPException(500, "Payment system not configured")

    try:
        import stripe
        stripe.api_key = stripe_key

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "MoA Converter Credits"},
                    "unit_amount": int(req.amount_usd * 100),
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=os.environ.get("STRIPE_SUCCESS_URL", "http://localhost:3000/payment-success"),
            cancel_url=os.environ.get("STRIPE_CANCEL_URL", "http://localhost:3000/payment-cancel"),
            metadata={
                "user_id": user["user_id"],
                "amount_usd": str(req.amount_usd),
            },
        )
        return {"checkout_url": session.url, "session_id": session.id}
    except ImportError:
        raise HTTPException(500, "stripe package not installed")
    except Exception as e:
        raise HTTPException(500, f"Payment error: {e}")


@app.post("/api/payments/webhook")
async def stripe_webhook(request_body: dict):
    """Handle Stripe webhook events (payment confirmation)."""
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        raise HTTPException(500, "Payment system not configured")

    event_type = request_body.get("type", "")
    if event_type == "checkout.session.completed":
        session = request_body.get("data", {}).get("object", {})
        metadata = session.get("metadata", {})
        user_id = metadata.get("user_id")
        amount = float(metadata.get("amount_usd", "0"))
        if user_id and amount > 0:
            svc = _get_credit_service()
            svc.purchase_credits(user_id, amount)
            logger.info("Payment confirmed: user=%s amount=$%.2f", user_id, amount)
    return {"received": True}


# ---------------------------------------------------------------------------
# WebSocket for real-time progress
# ---------------------------------------------------------------------------

@app.websocket("/ws/progress/{job_id}")
async def websocket_progress(websocket: WebSocket, job_id: str):
    await websocket.accept()
    _websockets[job_id] = websocket
    try:
        while True:
            # Keep connection alive, send progress updates
            if job_id in _jobs:
                j = _jobs[job_id]
                await websocket.send_json({
                    "job_id": job_id,
                    "status": j["status"],
                    "progress": j["progress"],
                    "message": j["message"],
                })
                if j["status"] in ("completed", "error"):
                    break
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    finally:
        _websockets.pop(job_id, None)


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

def _detect_pdf_type(input_path: str) -> str:
    """Detect whether a PDF is image-based (scanned) or digital."""
    try:
        from backend.core.digital_pdf_extractor import DigitalPdfExtractor
        extractor = DigitalPdfExtractor()
        if extractor.is_digital_pdf(input_path):
            return "digital_pdf"
        return "image_pdf"
    except Exception:
        return "image_pdf"  # default to more expensive type


def _run_conversion(job_id: str, req: ConvertRequest) -> None:
    """Run single file conversion in background thread."""
    try:
        _jobs[job_id]["status"] = "processing"
        cfg = _get_config()

        # Detect document type for pricing
        ext = Path(req.input_path).suffix.lower()
        if ext == ".pdf":
            doc_type = _detect_pdf_type(req.input_path)
        else:
            doc_type = "other"

        # Count pages for credit check (PDF only)
        num_pages = 0
        if ext == ".pdf":
            try:
                import fitz
                with fitz.open(req.input_path) as doc:
                    num_pages = len(doc)
            except Exception:
                num_pages = 1

        # Credit check and deduction for PDF files
        if doc_type != "other" and req.user_id:
            svc = _get_credit_service()
            if not svc.check_sufficient_balance(req.user_id, num_pages, doc_type):
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["message"] = "Insufficient credits"
                return
            # Debit upfront
            svc.debit_usage(
                req.user_id, num_pages, doc_type,
                description=f"{Path(req.input_path).name} ({num_pages}p, {doc_type})",
            )

        def progress_cb(msg: str, pct: float):
            _jobs[job_id]["progress"] = pct
            _jobs[job_id]["message"] = msg

        pipeline = Pipeline(config=cfg, progress_callback=progress_cb)

        job = PdfJob(
            input_path=Path(req.input_path),
            output_dir=Path(req.output_dir),
            filename=Path(req.input_path).stem,
            output_formats=req.output_formats,
            translate=req.translate,
            source_language=req.source_language,
            target_language=req.target_language,
        )

        result = pipeline.process(job)

        # Collect output files
        output_files = []
        output_dir = Path(req.output_dir)
        filename = Path(req.input_path).stem
        if result.html:
            output_files.append(str(output_dir / f"{filename}.html"))
        if result.markdown:
            output_files.append(str(output_dir / f"{filename}.md"))

        _jobs[job_id]["status"] = "completed"
        _jobs[job_id]["progress"] = 1.0
        _jobs[job_id]["message"] = "Conversion complete"
        _jobs[job_id]["result"] = {
            "total_pages": result.total_pages,
            "output_dir": req.output_dir,
            "output_files": output_files,
            "elapsed_seconds": result.metadata.get("elapsed_seconds", 0),
        }

    except Exception as exc:
        logger.error("Job %s failed: %s", job_id, exc)
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["message"] = str(exc)


def _run_batch_conversion(job_id: str, req: BatchConvertRequest) -> None:
    """Run batch conversion in background thread."""
    try:
        _jobs[job_id]["status"] = "processing"
        cfg = _get_config()
        cfg.output_formats = req.output_formats

        def progress_cb(msg: str, pct: float):
            _jobs[job_id]["progress"] = pct
            _jobs[job_id]["message"] = msg

        pipeline = Pipeline(config=cfg, progress_callback=progress_cb)

        results = pipeline.process_folder(
            folder=Path(req.folder_path),
            output_dir=Path(req.output_dir),
            recursive=req.recursive,
        )

        _jobs[job_id]["status"] = "completed"
        _jobs[job_id]["progress"] = 1.0
        _jobs[job_id]["message"] = f"Batch complete: {len(results)} files"
        _jobs[job_id]["result"] = {
            "total_files": len(results),
            "output_dir": req.output_dir,
        }

    except Exception as exc:
        logger.error("Batch job %s failed: %s", job_id, exc)
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["message"] = str(exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import uvicorn
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
