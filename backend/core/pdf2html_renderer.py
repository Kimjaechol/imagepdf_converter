"""pdf2htmlEX integration – high-fidelity PDF → HTML rendering for the viewer.

pdf2htmlEX produces layout-preserving HTML with absolute positioning, embedded
fonts, and CSS transforms that closely match the original PDF appearance.
This is used for the *viewer* layer only (read-only); the *editor* layer uses
PyMuPDF-extracted structured content via Markdown/Tiptap.

If pdf2htmlEX is not installed on the system, we fall back to PyMuPDF's built-in
``page.get_text("html")`` which is less accurate but always available.

Installation:
  - Ubuntu/Debian: ``apt-get install pdf2htmlex`` or build from source
  - macOS: ``brew install pdf2htmlex`` (community tap)
  - Windows: Pre-built binaries from https://github.com/nicedoc/pdf2htmlEX/releases
  - The binary must be on PATH or its location set via PDF2HTMLEX_PATH env var.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Possible binary names (varies by platform/package)
_BINARY_NAMES = ["pdf2htmlEX", "pdf2htmlex", "pdf2htmlEX.exe"]


def _find_pdf2htmlex() -> str | None:
    """Locate the pdf2htmlEX binary on the system."""
    # 1. Check explicit env var
    env_path = os.environ.get("PDF2HTMLEX_PATH", "")
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. Search PATH
    for name in _BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return found

    # 3. Check common install locations
    common_paths = [
        "/usr/local/bin/pdf2htmlEX",
        "/usr/bin/pdf2htmlEX",
        "/opt/pdf2htmlEX/bin/pdf2htmlEX",
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p

    return None


def is_pdf2htmlex_available() -> bool:
    """Check if pdf2htmlEX is installed and accessible."""
    return _find_pdf2htmlex() is not None


def get_pdf2htmlex_version() -> str | None:
    """Get the pdf2htmlEX version string, or None if not installed."""
    binary = _find_pdf2htmlex()
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # pdf2htmlEX prints version to stderr
        output = (result.stdout + result.stderr).strip()
        for line in output.splitlines():
            if "pdf2htmlEX" in line or "version" in line.lower():
                return line.strip()
        return output.splitlines()[0] if output else "unknown"
    except Exception:
        return None


def render_pdf_to_viewer_html(
    pdf_path: Path | str,
    output_dir: Path | str,
    *,
    output_filename: str = "viewer.html",
    zoom: float = 1.0,
    optimize_text: bool = True,
    embed_font: bool = True,
    embed_image: bool = True,
    split_pages: bool = False,
    timeout_seconds: int = 300,
) -> Path | None:
    """Render a PDF to high-fidelity viewer HTML using pdf2htmlEX.

    Args:
        pdf_path: Input PDF file.
        output_dir: Directory to write the output HTML (and assets).
        output_filename: Name of the output HTML file.
        zoom: Zoom factor (1.0 = 100%).
        optimize_text: Optimize text rendering.
        embed_font: Embed fonts in the HTML.
        embed_image: Embed images as data URIs.
        split_pages: Split into per-page HTML files.
        timeout_seconds: Max execution time.

    Returns:
        Path to the generated HTML file, or None if pdf2htmlEX is not available.
    """
    binary = _find_pdf2htmlex()
    if not binary:
        logger.warning(
            "pdf2htmlEX not found. Install it for high-fidelity viewer HTML. "
            "Falling back to PyMuPDF HTML rendering."
        )
        return None

    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        binary,
        "--dest-dir", str(output_dir),
        "--zoom", str(zoom),
    ]

    if optimize_text:
        cmd.extend(["--optimize-text", "1"])

    if embed_font:
        cmd.extend(["--embed-font", "1"])

    if embed_image:
        cmd.extend(["--embed-image", "1"])

    if split_pages:
        cmd.extend(["--split-pages", "1"])

    # Output filename
    cmd.append(str(pdf_path))

    logger.info("Running pdf2htmlEX: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(output_dir),
        )

        if result.returncode != 0:
            logger.error(
                "pdf2htmlEX failed (exit %d): %s",
                result.returncode,
                result.stderr[:500],
            )
            return None

        # pdf2htmlEX names output after input file by default
        default_output = output_dir / (pdf_path.stem + ".html")
        target_output = output_dir / output_filename

        if default_output.exists() and default_output != target_output:
            default_output.rename(target_output)
        elif not target_output.exists() and not default_output.exists():
            # Check for any generated HTML
            html_files = list(output_dir.glob("*.html"))
            if html_files:
                html_files[0].rename(target_output)
            else:
                logger.error("pdf2htmlEX produced no output HTML")
                return None

        logger.info("pdf2htmlEX viewer HTML generated: %s", target_output)
        return target_output

    except subprocess.TimeoutExpired:
        logger.error(
            "pdf2htmlEX timed out after %ds for %s",
            timeout_seconds, pdf_path,
        )
        return None
    except Exception as exc:
        logger.error("pdf2htmlEX execution failed: %s", exc)
        return None


def render_pdf_to_viewer_html_fallback(
    pdf_path: Path | str,
    output_dir: Path | str,
    *,
    output_filename: str = "viewer.html",
    dpi: int = 150,
) -> Path:
    """Fallback: Render PDF to viewer HTML using PyMuPDF when pdf2htmlEX is unavailable.

    Generates a self-contained HTML with each page rendered via PyMuPDF's
    built-in HTML output. Less accurate than pdf2htmlEX but always works.
    """
    import fitz

    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    pages_html = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        # Use PyMuPDF's HTML output (includes inline styling)
        page_html = page.get_text("html")
        pages_html.append(
            f'<div class="pdf-page" data-page="{page_idx + 1}"'
            f' style="margin-bottom:20px; padding:20px; '
            f'background:white; box-shadow:0 1px 4px rgba(0,0,0,0.15);">'
            f'{page_html}</div>'
        )

    doc.close()

    full_html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{pdf_path.stem} - Viewer</title>
<style>
  body {{
    margin: 0;
    padding: 20px;
    background: #f5f5f5;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .pdf-page {{
    max-width: 900px;
    margin: 0 auto 20px;
    overflow: hidden;
  }}
  img {{ max-width: 100%; height: auto; }}
  table {{ border-collapse: collapse; }}
  td, th {{ border: 1px solid #ccc; padding: 4px 8px; }}
</style>
</head>
<body>
{"".join(pages_html)}
</body>
</html>"""

    output_path = output_dir / output_filename
    output_path.write_text(full_html, encoding="utf-8")

    logger.info("PyMuPDF fallback viewer HTML generated: %s", output_path)
    return output_path
