"""한컴 DocsConverter client – converts non-PDF documents via remote REST API.

Converts HWP, HWPX, DOC, DOCX, XLS, XLSX, PPT, PPTX to HTML using
Hancom DocsConverter installed on a remote AWS server.

Flow:
  1. Upload the file to the remote server via HTTP multipart POST.
  2. Call the DocsConverter REST API with the uploaded file path.
  3. Retrieve the HTML result.
  4. (Optional) download generated images/resources.

Server: http://{HANCOM_HOST}:{HANCOM_PORT}
API pattern: /{module_code}/{convert_api}?file_path={relative_path}&show_type=0
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default Hancom server address (can be overridden by env vars)
HANCOM_HOST = os.environ.get("HANCOM_HOST", "3.35.4.24")
HANCOM_PORT = os.environ.get("HANCOM_PORT", "8101")

# Base URL for the Hancom DocsConverter API
def _hancom_base_url() -> str:
    host = os.environ.get("HANCOM_HOST", HANCOM_HOST)
    port = os.environ.get("HANCOM_PORT", HANCOM_PORT)
    return f"http://{host}:{port}"

# Upload endpoint on the server (file receiver)
# Adjust this to match your server's file upload path.
HANCOM_UPLOAD_PATH = os.environ.get("HANCOM_UPLOAD_PATH", "/upload")

# The source file prefix on the server where uploaded files are stored.
# The DocsConverter expects file_path relative to this prefix.
SOURCE_FILE_PATH_PREFIX = os.environ.get(
    "HANCOM_SOURCE_FILE_PREFIX", "/opt/hancom/upload"
)

# Timeouts
UPLOAD_TIMEOUT = 120  # seconds for file upload
CONVERT_TIMEOUT = 300  # seconds for conversion


# ---------------------------------------------------------------------------
# Extension → Module mapping
# ---------------------------------------------------------------------------

# Hancom DocsConverter module codes by file extension
_EXTENSION_MODULE_MAP: dict[str, str] = {
    # 한글 (Hangul word processor)
    "hwp": "hwp",
    "hwpx": "hwp",
    # MS Word
    "doc": "word",
    "docx": "word",
    # MS Excel
    "xls": "cell",
    "xlsx": "cell",
    # MS PowerPoint
    "ppt": "show",
    "pptx": "show",
}

# All supported extensions
SUPPORTED_EXTENSIONS = set(_EXTENSION_MODULE_MAP.keys())


def get_module_code(extension: str) -> str:
    """Get the Hancom module code for a file extension.

    Raises ValueError if extension is not supported.
    """
    ext = extension.lower().lstrip(".")
    module = _EXTENSION_MODULE_MAP.get(ext)
    if module is None:
        raise ValueError(
            f"Unsupported extension for Hancom DocsConverter: .{ext}. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    return module


def is_hancom_supported(extension: str) -> bool:
    """Check if a file extension is supported by Hancom DocsConverter."""
    return extension.lower().lstrip(".") in SUPPORTED_EXTENSIONS


# ---------------------------------------------------------------------------
# File upload to server
# ---------------------------------------------------------------------------

def upload_file(file_path: str) -> str:
    """Upload a file to the Hancom server.

    Args:
        file_path: Local path to the file to upload.

    Returns:
        The relative file path on the server (for use in API calls).

    Raises:
        FileNotFoundError: If the local file doesn't exist.
        RuntimeError: If the upload fails.
    """
    local_path = Path(file_path)
    if not local_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    base_url = _hancom_base_url()
    upload_url = f"{base_url}{HANCOM_UPLOAD_PATH}"

    logger.info("Uploading %s to Hancom server: %s", local_path.name, upload_url)
    start = time.time()

    try:
        with open(local_path, "rb") as f:
            response = httpx.post(
                upload_url,
                files={"file": (local_path.name, f)},
                timeout=UPLOAD_TIMEOUT,
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"File upload failed (HTTP {response.status_code}): "
                f"{response.text[:500]}"
            )

        data = response.json()
        # The server should return the relative path where the file was saved
        # Expected response: {"file_path": "relative/path/to/file.docx"}
        # or {"path": "..."} or {"filename": "..."}
        remote_path = (
            data.get("file_path")
            or data.get("path")
            or data.get("filename")
            or local_path.name
        )

        elapsed = time.time() - start
        logger.info(
            "Upload complete: %s → %s (%.1fs)",
            local_path.name, remote_path, elapsed,
        )
        return remote_path

    except httpx.TimeoutException:
        raise RuntimeError(
            f"File upload timed out after {UPLOAD_TIMEOUT}s: {local_path.name}"
        )
    except httpx.ConnectError:
        raise RuntimeError(
            f"Cannot connect to Hancom server at {base_url}. "
            f"Check HANCOM_HOST and HANCOM_PORT environment variables."
        )


# ---------------------------------------------------------------------------
# Document conversion
# ---------------------------------------------------------------------------

def convert_to_html(
    file_path: str,
    output_dir: str | None = None,
    show_type: int = 0,
    sync: bool = True,
) -> dict[str, Any]:
    """Convert a document to HTML using Hancom DocsConverter.

    Steps:
      1. Upload file to the remote server.
      2. Call DocsConverter API: /{module}/doc2htm?file_path=...&show_type=...
      3. Parse response and save HTML + images locally.

    Args:
        file_path: Local path to the document.
        output_dir: Where to save HTML output. If None, only returns HTML string.
        show_type: 0 = return HTML directly, 2 = return JSON with file paths.
        sync: True for synchronous conversion (function=sync).

    Returns:
        dict with keys: html, images, output_path, elapsed_seconds, engine
    """
    local_path = Path(file_path)
    ext = local_path.suffix.lower().lstrip(".")
    module = get_module_code(ext)
    stem = local_path.stem
    base_url = _hancom_base_url()

    start = time.time()

    # Step 1: Upload file to server
    remote_file_path = upload_file(file_path)

    # Step 2: Call DocsConverter API
    func_param = "sync" if sync else "async"
    convert_url = (
        f"{base_url}/{module}/doc2htm"
        f"?file_path={remote_file_path}"
        f"&show_type={show_type}"
        f"&function={func_param}"
    )

    logger.info("Hancom converting: %s (module=%s)", local_path.name, module)

    try:
        response = httpx.get(convert_url, timeout=CONVERT_TIMEOUT)
    except httpx.TimeoutException:
        raise TimeoutError(
            f"Hancom conversion timed out after {CONVERT_TIMEOUT}s: {local_path.name}"
        )
    except httpx.ConnectError:
        raise RuntimeError(
            f"Cannot connect to Hancom server at {base_url}"
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Hancom conversion failed (HTTP {response.status_code}): "
            f"{response.text[:500]}"
        )

    # Step 3: Parse response
    html_content = ""
    images: list[tuple[str, bytes]] = []

    if show_type == 0:
        # Direct HTML response
        html_content = response.text
    else:
        # JSON response with file paths
        data = response.json()
        # Expected: {"html_path": "...", "image_paths": [...]}
        html_path_remote = data.get("html_path", "")
        image_paths_remote = data.get("image_paths", [])

        # Download HTML file from server
        if html_path_remote:
            html_url = f"{base_url}/download?path={html_path_remote}"
            html_resp = httpx.get(html_url, timeout=60)
            if html_resp.status_code == 200:
                html_content = html_resp.text

        # Download images
        for img_path in image_paths_remote:
            img_url = f"{base_url}/download?path={img_path}"
            img_resp = httpx.get(img_url, timeout=60)
            if img_resp.status_code == 200:
                img_name = Path(img_path).name
                images.append((img_name, img_resp.content))

    elapsed = time.time() - start

    # Step 4: Save to output_dir if requested
    final_output_path = None
    if output_dir and html_content:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        final_html = out / f"{stem}.html"
        final_html.write_text(html_content, encoding="utf-8")
        final_output_path = str(final_html)

        if images:
            img_dir = out / "images"
            img_dir.mkdir(exist_ok=True)
            for img_name, img_data in images:
                (img_dir / img_name).write_bytes(img_data)
            # Rewrite image paths in HTML
            html_content = _rewrite_image_paths(html_content, "images")
            final_html.write_text(html_content, encoding="utf-8")

    logger.info(
        "Hancom done: %s (%.1fs, %d images, module=%s)",
        local_path.name, elapsed, len(images), module,
    )

    return {
        "html": html_content,
        "images": images,
        "output_path": final_output_path,
        "elapsed_seconds": round(elapsed, 2),
        "engine": "hancom",
        "module": module,
    }


# ---------------------------------------------------------------------------
# Batch conversion
# ---------------------------------------------------------------------------

def convert_batch(
    input_paths: list[str],
    output_dir: str,
    progress_callback: Any = None,
) -> list[dict]:
    """Convert multiple documents sequentially via Hancom DocsConverter.

    Each file is uploaded and converted one at a time (the remote server
    handles the heavy lifting, so local parallelism isn't needed).

    Args:
        input_paths: List of local document file paths.
        output_dir: Root output directory.
        progress_callback: Optional fn(filename, completed, total).

    Returns:
        List of result dicts in same order as input_paths.
    """
    if not input_paths:
        return []

    total = len(input_paths)
    results: list[dict] = []

    for i, path in enumerate(input_paths):
        stem = Path(path).stem
        file_out = str(Path(output_dir) / stem)

        try:
            result = convert_to_html(path, file_out)
            results.append(result)
        except Exception as exc:
            logger.error("Failed to convert %s: %s", path, exc)
            results.append({
                "error": str(exc),
                "input_path": path,
                "html": None,
                "images": [],
                "output_path": None,
                "elapsed_seconds": 0,
                "engine": "hancom",
            })

        if progress_callback:
            progress_callback(Path(path).name, i + 1, total)

    return results


# ---------------------------------------------------------------------------
# Server health check
# ---------------------------------------------------------------------------

def is_hancom_available() -> bool:
    """Check if the Hancom DocsConverter server is reachable."""
    try:
        base_url = _hancom_base_url()
        response = httpx.get(f"{base_url}/health", timeout=5)
        return response.status_code == 200
    except Exception:
        # Try a simple connection check
        try:
            response = httpx.get(_hancom_base_url(), timeout=5)
            return response.status_code < 500
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rewrite_image_paths(html: str, img_dir: str) -> str:
    """Rewrite image src paths to point to img_dir/."""
    def _replace(match: re.Match) -> str:
        src = match.group(1)
        if src.startswith(("http://", "https://", "data:")):
            return match.group(0)
        filename = Path(src).name
        return f'src="{img_dir}/{filename}"'

    return re.sub(r'src="([^"]*)"', _replace, html)


def clean_hancom_html(html: str) -> str:
    """Clean up Hancom DocsConverter HTML output.

    Removes server-specific artifacts while preserving document structure.
    """
    if not html:
        return html

    # Remove XML declaration
    html = re.sub(r'<\?xml[^?]*\?>\s*', '', html)

    # Remove Hancom-specific meta tags
    html = re.sub(r'<meta\s+name="generator"[^>]*>', '', html)

    # Remove empty paragraphs / spans (only truly empty ones)
    html = re.sub(r'<p[^>]*>\s*</p>', '', html)
    html = re.sub(r'<span[^>]*>\s*</span>', '', html)

    # Collapse multiple blank lines into one
    html = re.sub(r'\n{3,}', '\n\n', html)

    return html
