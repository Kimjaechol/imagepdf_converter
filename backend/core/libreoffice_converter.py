"""LibreOffice headless document converter with parallel processing.

Converts DOCX, HWPX, XLSX, PPTX to HTML using LibreOffice in headless mode.
Multiple instances run in parallel with isolated user profiles.

Requirements:
  - LibreOffice installed (soffice command available)
  - On Linux: apt install libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress
  - On macOS: brew install --cask libreoffice
  - On Windows: standard LibreOffice installation
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# soffice binary detection
# ---------------------------------------------------------------------------

def _find_soffice() -> str | None:
    """Locate the soffice binary."""
    found = shutil.which("soffice")
    if found:
        return found

    candidates = []
    system = platform.system()
    if system == "Darwin":
        candidates = [
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
    elif system == "Windows":
        for base in [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        ]:
            if base:
                candidates.append(os.path.join(base, "LibreOffice", "program", "soffice.exe"))
    else:  # Linux
        candidates = [
            "/usr/bin/soffice",
            "/usr/lib/libreoffice/program/soffice",
            "/snap/bin/libreoffice",
        ]

    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


_soffice_path: str | None = None


def get_soffice_path() -> str:
    """Get cached soffice path, raising if not found."""
    global _soffice_path
    if _soffice_path is None:
        _soffice_path = _find_soffice()
    if _soffice_path is None:
        raise RuntimeError(
            "LibreOffice not found. Install it:\n"
            "  Linux: sudo apt install libreoffice\n"
            "  macOS: brew install --cask libreoffice\n"
            "  Windows: https://www.libreoffice.org/download/"
        )
    return _soffice_path


def is_libreoffice_available() -> bool:
    """Check if LibreOffice is installed."""
    try:
        get_soffice_path()
        return True
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# Optimal worker count
# ---------------------------------------------------------------------------

def _detect_max_workers() -> int:
    """Detect optimal number of parallel LibreOffice instances.

    Heuristic: min(CPU cores, RAM_GB // 1, 8).
    Each LibreOffice instance uses ~200-500MB RAM.
    """
    cpu_count = os.cpu_count() or 4

    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        # Reserve 2GB for OS + app, allow ~0.5GB per LO instance
        ram_workers = max(1, int((ram_gb - 2) / 0.5))
    except ImportError:
        # No psutil — estimate conservatively
        ram_workers = cpu_count

    workers = min(cpu_count, ram_workers, 8)
    return max(1, workers)


# ---------------------------------------------------------------------------
# Single file conversion
# ---------------------------------------------------------------------------

def convert_to_html(
    input_path: str,
    output_dir: str | None = None,
    timeout: int = 120,
) -> dict:
    """Convert a single document to HTML using LibreOffice headless.

    Each call gets its own user profile directory so multiple instances
    can run in parallel without conflicts.

    Args:
        input_path: Path to the input document.
        output_dir: Where to save the final HTML + images. If None, only
                    returns the HTML string without saving.
        timeout: Maximum seconds to wait for soffice.

    Returns:
        dict with keys: html, images, output_path, elapsed_seconds
    """
    soffice = get_soffice_path()
    input_path = Path(input_path).resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    stem = input_path.stem

    with tempfile.TemporaryDirectory(prefix="lo_convert_") as tmpdir:
        user_profile = os.path.join(tmpdir, "profile")
        cmd = [
            soffice,
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation=file://{user_profile}",
            "--convert-to", "html",
            "--outdir", tmpdir,
            str(input_path),
        ]

        logger.info("LibreOffice converting: %s", input_path.name)
        start_time = time.time()

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tmpdir,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"LibreOffice conversion timed out after {timeout}s: {input_path.name}"
            )

        elapsed = time.time() - start_time

        if result.returncode != 0:
            logger.error("soffice stderr: %s", result.stderr)
            raise RuntimeError(
                f"LibreOffice conversion failed (exit {result.returncode}): "
                f"{result.stderr[:500]}"
            )

        # Find the output HTML file
        html_file = Path(tmpdir) / f"{stem}.html"
        if not html_file.exists():
            html_files = list(Path(tmpdir).glob("*.html"))
            if html_files:
                html_file = html_files[0]
            else:
                raise RuntimeError(
                    f"LibreOffice produced no HTML output. "
                    f"stdout: {result.stdout[:300]}"
                )

        html_content = html_file.read_text(encoding="utf-8", errors="replace")

        # Collect generated images
        images: list[tuple[str, bytes]] = []
        for img_file in Path(tmpdir).iterdir():
            if img_file.suffix.lower() in (
                ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp",
            ):
                images.append((img_file.name, img_file.read_bytes()))

        # Persist to output_dir if requested
        final_output_path = None
        if output_dir:
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
                html_content = _rewrite_image_paths(html_content, "images")
                final_html.write_text(html_content, encoding="utf-8")

        logger.info(
            "LibreOffice done: %s (%.1fs, %d images)",
            input_path.name, elapsed, len(images),
        )

        return {
            "html": html_content,
            "images": images,
            "output_path": final_output_path,
            "elapsed_seconds": round(elapsed, 2),
        }


# ---------------------------------------------------------------------------
# Batch / parallel conversion
# ---------------------------------------------------------------------------

def convert_batch(
    input_paths: list[str],
    output_dir: str,
    max_workers: int | None = None,
    timeout_per_file: int = 120,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> list[dict]:
    """Convert multiple documents in parallel using a LibreOffice worker pool.

    Each worker spawns its own soffice process with an isolated user profile,
    so there is zero contention between instances.

    Args:
        input_paths: List of document file paths.
        output_dir: Root output directory. Each file gets a subdirectory
                    named after its stem to avoid collisions.
        max_workers: Number of parallel LibreOffice instances.
                     Auto-detected if None.
        timeout_per_file: Seconds before a single conversion is killed.
        progress_callback: Optional ``fn(filename, completed, total)``
                           called after each file finishes.

    Returns:
        List of result dicts (one per file), in the same order as
        *input_paths*.  Failed files have an ``"error"`` key.
    """
    if not input_paths:
        return []

    if max_workers is None:
        max_workers = _detect_max_workers()
    # Never more workers than files
    max_workers = min(max_workers, len(input_paths))

    logger.info(
        "Batch converting %d documents with %d parallel workers",
        len(input_paths), max_workers,
    )

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    total = len(input_paths)
    completed = 0
    results: dict[int, dict] = {}

    def _do_one(idx: int, path: str) -> tuple[int, dict]:
        stem = Path(path).stem
        # Per-file output subdirectory
        file_out = out_root / stem
        file_out.mkdir(parents=True, exist_ok=True)
        try:
            return idx, convert_to_html(path, str(file_out), timeout_per_file)
        except Exception as exc:
            logger.error("Failed to convert %s: %s", path, exc)
            return idx, {
                "error": str(exc),
                "input_path": path,
                "html": None,
                "images": [],
                "output_path": None,
                "elapsed_seconds": 0,
            }

    start_all = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_do_one, i, p): i
            for i, p in enumerate(input_paths)
        }
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result
            completed += 1
            fname = Path(input_paths[idx]).name
            if progress_callback:
                progress_callback(fname, completed, total)
            logger.info(
                "Batch progress: %d/%d – %s", completed, total, fname,
            )

    total_elapsed = time.time() - start_all
    logger.info(
        "Batch complete: %d files in %.1fs (%.1f files/sec)",
        total, total_elapsed, total / total_elapsed if total_elapsed > 0 else 0,
    )

    # Return in original order
    return [results[i] for i in range(total)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rewrite_image_paths(html: str, img_dir: str) -> str:
    """Rewrite image src paths to point to *img_dir*/."""
    def _replace(match):
        src = match.group(1)
        if src.startswith(("http://", "https://", "data:")):
            return match.group(0)
        filename = Path(src).name
        return f'src="{img_dir}/{filename}"'

    return re.sub(r'src="([^"]*)"', _replace, html)


def clean_libreoffice_html(html: str) -> str:
    """Strip LibreOffice-specific noise from HTML while keeping structure."""
    # XML declaration / DOCTYPE
    html = re.sub(r'<\?xml[^?]*\?>\s*', '', html)

    # LO meta tags
    html = re.sub(r'<meta\s+name="generator"[^>]*>', '', html)
    html = re.sub(r'<meta\s+name="created"[^>]*>', '', html)
    html = re.sub(r'<meta\s+name="changed"[^>]*>', '', html)

    # Empty paragraphs / spans
    html = re.sub(r'<p[^>]*>\s*</p>', '', html)
    html = re.sub(r'<span[^>]*>\s*</span>', '', html)

    # Excessive whitespace before >
    html = re.sub(r'\s+>', '>', html)

    return html
