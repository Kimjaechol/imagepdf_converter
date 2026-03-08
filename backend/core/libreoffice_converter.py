"""LibreOffice headless document converter.

Converts DOCX, HWPX, XLSX, PPTX to HTML using LibreOffice in headless mode.
Optionally applies Gemini-based post-processing to clean up the output.

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
from pathlib import Path

logger = logging.getLogger(__name__)


def _find_soffice() -> str | None:
    """Locate the soffice binary."""
    # Check PATH first
    found = shutil.which("soffice")
    if found:
        return found

    # Common locations
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


def convert_to_html(
    input_path: str,
    output_dir: str | None = None,
    timeout: int = 120,
) -> dict:
    """Convert a document to HTML using LibreOffice headless.

    Args:
        input_path: Path to the input document
        output_dir: Output directory (defaults to temp dir)
        timeout: Maximum seconds to wait for conversion

    Returns:
        dict with keys: html, images, output_path, elapsed_seconds
    """
    soffice = get_soffice_path()
    input_path = Path(input_path).resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    ext = input_path.suffix.lower()
    stem = input_path.stem

    # Use a temporary directory for conversion output to avoid conflicts
    with tempfile.TemporaryDirectory(prefix="lo_convert_") as tmpdir:
        # Build the soffice command
        # Use a unique user profile to allow parallel conversions
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
                f"LibreOffice conversion failed (exit {result.returncode}): {result.stderr[:500]}"
            )

        # Find the output HTML file
        html_file = Path(tmpdir) / f"{stem}.html"
        if not html_file.exists():
            # LibreOffice may produce a slightly different name
            html_files = list(Path(tmpdir).glob("*.html"))
            if html_files:
                html_file = html_files[0]
            else:
                raise RuntimeError(
                    f"LibreOffice produced no HTML output. stdout: {result.stdout[:300]}"
                )

        html_content = html_file.read_text(encoding="utf-8", errors="replace")

        # Collect any generated images (LibreOffice puts them alongside the HTML)
        images: list[tuple[str, bytes]] = []
        for img_file in Path(tmpdir).iterdir():
            if img_file.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp"):
                images.append((img_file.name, img_file.read_bytes()))

        # If output_dir specified, copy files there
        final_output_path = None
        if output_dir:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)

            # Save HTML
            final_html = out / f"{stem}.html"
            final_html.write_text(html_content, encoding="utf-8")
            final_output_path = str(final_html)

            # Save images
            if images:
                img_dir = out / "images"
                img_dir.mkdir(exist_ok=True)
                for img_name, img_data in images:
                    (img_dir / img_name).write_bytes(img_data)
                # Update image references in HTML
                html_content = _rewrite_image_paths(html_content, "images")
                final_html.write_text(html_content, encoding="utf-8")

        logger.info(
            "LibreOffice conversion complete: %s → HTML (%.1fs, %d images)",
            input_path.name, elapsed, len(images),
        )

        return {
            "html": html_content,
            "images": images,
            "output_path": final_output_path,
            "elapsed_seconds": round(elapsed, 2),
        }


def _rewrite_image_paths(html: str, img_dir: str) -> str:
    """Rewrite image src paths in LibreOffice-generated HTML to point to img_dir/."""
    def _replace(match):
        src = match.group(1)
        # Only rewrite local file references (not URLs)
        if src.startswith("http://") or src.startswith("https://") or src.startswith("data:"):
            return match.group(0)
        filename = Path(src).name
        return f'src="{img_dir}/{filename}"'

    return re.sub(r'src="([^"]*)"', _replace, html)


def clean_libreoffice_html(html: str) -> str:
    """Clean up LibreOffice's verbose HTML output.

    LibreOffice generates very verbose HTML with inline styles.
    This function simplifies it while preserving structure and formatting.
    """
    # Remove XML declaration and DOCTYPE
    html = re.sub(r'<\?xml[^?]*\?>\s*', '', html)

    # Remove LibreOffice-specific meta tags (but keep charset)
    html = re.sub(r'<meta\s+name="generator"[^>]*>', '', html)
    html = re.sub(r'<meta\s+name="created"[^>]*>', '', html)
    html = re.sub(r'<meta\s+name="changed"[^>]*>', '', html)

    # Remove empty paragraphs (common LO artifact)
    html = re.sub(r'<p[^>]*>\s*</p>', '', html)

    # Remove empty spans
    html = re.sub(r'<span[^>]*>\s*</span>', '', html)

    # Simplify excessive whitespace in tags
    html = re.sub(r'\s+>', '>', html)

    return html
