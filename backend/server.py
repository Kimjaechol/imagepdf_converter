"""FastAPI server – bridges the Electron frontend with the Python pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.core.pipeline import Pipeline, PipelineConfig
from backend.models.schema import PdfJob
from backend.services.credit_service import CreditService

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


def _get_credit_service() -> CreditService:
    global _credit_service
    if _credit_service is None:
        _credit_service = CreditService(data_dir="data")
    return _credit_service


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


class TranslateHtmlRequest(BaseModel):
    html: str
    source_language: str = ""
    target_language: str = "ko"


class PurchaseCreditsRequest(BaseModel):
    user_id: str
    amount_usd: float


class EstimateCostRequest(BaseModel):
    num_pages: int
    translate: bool = False


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
        "layout_engine": cfg.layout_engine,
        "ocr_engine": cfg.ocr_engine,
        "reading_order_mode": cfg.reading_order_mode,
        "heading_mode": cfg.heading_mode,
        "correction_mode": cfg.correction_mode,
        "correction_aggressiveness": cfg.correction_aggressiveness,
    }


@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    cfg = _get_config()
    if hasattr(cfg, update.key):
        setattr(cfg, update.key, update.value)
        return {"status": "ok", "key": update.key, "value": update.value}
    raise HTTPException(400, f"Unknown config key: {update.key}")


@app.post("/api/convert", response_model=JobStatus)
async def convert_single(req: ConvertRequest):
    """Start a single PDF conversion job."""
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
# Credit System
# ---------------------------------------------------------------------------

@app.get("/api/credits/{user_id}")
async def get_credits(user_id: str):
    """Get a user's credit balance."""
    svc = _get_credit_service()
    acct = svc.get_or_create_account(user_id)
    return {
        "user_id": acct.user_id,
        "balance_usd": round(acct.balance_usd, 4),
        "total_purchased_usd": round(acct.total_purchased_usd, 4),
        "total_consumed_usd": round(acct.total_consumed_usd, 4),
    }


@app.post("/api/credits/purchase")
async def purchase_credits(req: PurchaseCreditsRequest):
    """Add credits to a user's balance."""
    if req.amount_usd <= 0:
        raise HTTPException(400, "Amount must be positive")
    svc = _get_credit_service()
    new_balance = svc.purchase_credits(req.user_id, req.amount_usd)
    return {
        "user_id": req.user_id,
        "amount_usd": req.amount_usd,
        "new_balance_usd": round(new_balance, 4),
    }


@app.post("/api/credits/estimate")
async def estimate_cost(req: EstimateCostRequest):
    """Estimate the credit cost for converting N pages."""
    svc = _get_credit_service()
    return svc.estimate_cost(req.num_pages, translate=req.translate)


@app.get("/api/credits/{user_id}/history")
async def get_credit_history(user_id: str, limit: int = 50):
    """Get a user's recent credit usage history."""
    svc = _get_credit_service()
    acct = svc.get_or_create_account(user_id)
    history = acct.usage_history[-limit:]
    history.reverse()
    return {"user_id": user_id, "history": history}


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

def _run_conversion(job_id: str, req: ConvertRequest) -> None:
    """Run single file conversion in background thread."""
    try:
        _jobs[job_id]["status"] = "processing"
        cfg = _get_config()

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
