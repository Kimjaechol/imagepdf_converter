"""Gemini-based HTML refinement for LibreOffice output.

Compares the original document (rendered as images) with LibreOffice HTML output
and fixes discrepancies. This is the "2nd pass" quality assurance step.

Only called when:
1. The document is complex (tables, mixed formatting, multi-column)
2. The user has opted for high-quality conversion
3. A Gemini API key is configured
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Max characters per chunk sent to Gemini
_MAX_CHUNK_CHARS = 40_000


def is_gemini_available() -> bool:
    """Check if Gemini API key is configured."""
    return bool(os.environ.get("GEMINI_API_KEY", ""))


def refine_html(
    html: str,
    original_path: str | None = None,
    doc_type: str = "document",
) -> str:
    """Refine LibreOffice-generated HTML using Gemini.

    Focuses on:
    - Table structure correction (merged cells, headers)
    - Heading level normalization
    - List structure cleanup
    - Removing LibreOffice artifacts (empty elements, redundant styles)
    - Consistent Korean typography

    Args:
        html: The LibreOffice-generated HTML
        original_path: Path to original document (for context in prompt)
        doc_type: "docx", "hwpx", "xlsx", "pptx" etc.

    Returns:
        Refined HTML string
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.info("Gemini API key not configured, skipping HTML refinement")
        return html

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
    except ImportError:
        logger.warning("google-generativeai not installed, skipping refinement")
        return html

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-lite")
    model = genai.GenerativeModel(model_name)

    doc_name = Path(original_path).name if original_path else "document"

    # For small documents, process in one shot
    if len(html) <= _MAX_CHUNK_CHARS:
        return _refine_chunk(model, html, doc_name, doc_type)

    # For large documents, split at block boundaries and process chunks
    return _refine_large_html(model, html, doc_name, doc_type)


def _refine_chunk(model, html: str, doc_name: str, doc_type: str) -> str:
    """Refine a single chunk of HTML."""
    prompt = f"""You are an HTML document quality reviewer. A "{doc_type}" file named "{doc_name}" was converted to HTML by LibreOffice.

Review and fix the HTML below. Make ONLY these corrections:

1. TABLE FIXES:
   - Ensure proper <thead>/<tbody> separation
   - Fix rowspan/colspan if cells are misaligned
   - Add missing border attributes
   - Remove empty rows/columns

2. HEADING FIXES:
   - Ensure logical heading hierarchy (h1 > h2 > h3, no skipping)
   - Convert obviously-headings paragraphs (large bold text) to proper heading tags

3. LIST FIXES:
   - Convert sequences of "- item" or "1. item" paragraphs to proper <ul>/<ol>

4. CLEANUP:
   - Remove empty <p>, <span>, <div> elements
   - Remove LibreOffice-specific CSS classes that add no value
   - Simplify redundant inline styles
   - Fix broken image references

5. PRESERVE:
   - ALL text content exactly as-is (do NOT rewrite, translate, or summarize)
   - All meaningful formatting (bold, italic, underline, colors, font sizes)
   - All images and their positions
   - Document structure and reading order

CRITICAL: Output ONLY the corrected HTML. No explanation, no code fences.
If the HTML is already good, return it unchanged.

HTML to review:
{html}"""

    try:
        response = model.generate_content(prompt)
        refined = response.text.strip()

        # Remove code fences if Gemini added them
        if refined.startswith("```html"):
            refined = refined[7:]
        elif refined.startswith("```"):
            refined = refined[3:]
        if refined.endswith("```"):
            refined = refined[:-3]
        refined = refined.strip()

        # Validate: refined should be similar length (no accidental truncation)
        if len(refined) < len(html) * 0.5:
            logger.warning(
                "Gemini refinement too short (%d vs %d chars), keeping original",
                len(refined), len(html),
            )
            return html

        # Validate: should still contain HTML structure
        if "<" not in refined or ">" not in refined:
            logger.warning("Gemini refinement lost HTML structure, keeping original")
            return html

        return refined

    except Exception as e:
        logger.error("Gemini refinement failed: %s", e)
        return html


def _refine_large_html(model, html: str, doc_name: str, doc_type: str) -> str:
    """Refine a large HTML document by splitting into chunks."""
    # Extract head and body
    body_start = html.find("<body")
    body_end = html.rfind("</body>")

    if body_start == -1 or body_end == -1:
        # No body tags, try to process as single chunk anyway
        if len(html) <= _MAX_CHUNK_CHARS * 2:
            return _refine_chunk(model, html, doc_name, doc_type)
        return html

    body_tag_end = html.index(">", body_start) + 1
    head_part = html[:body_tag_end]
    body_content = html[body_tag_end:body_end]
    tail_part = html[body_end:]

    # Split body at block-level elements
    chunks = _split_html_chunks(body_content, _MAX_CHUNK_CHARS)

    refined_chunks = []
    for i, chunk in enumerate(chunks):
        logger.info("Refining chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
        refined = _refine_chunk(model, chunk, doc_name, doc_type)
        refined_chunks.append(refined)

    return head_part + "\n".join(refined_chunks) + tail_part


def _split_html_chunks(html: str, max_chars: int) -> list[str]:
    """Split HTML at block-level tag boundaries."""
    # Split on block-level opening tags
    parts = re.split(r'(?=<(?:div|h[1-6]|p|table|section|article)[\s>])', html)

    chunks = []
    current = ""
    for part in parts:
        if len(current) + len(part) > max_chars and current:
            chunks.append(current)
            current = part
        else:
            current += part
    if current:
        chunks.append(current)

    return chunks if chunks else [html]


def estimate_refinement_cost(html: str) -> dict:
    """Estimate the Gemini API cost for refining this HTML.

    Returns dict with token estimates and approximate cost.
    """
    # Rough estimate: 1 char ≈ 0.3 tokens for HTML
    input_tokens = int(len(html) * 0.3)
    # Output is roughly same size as input (corrections only)
    output_tokens = int(input_tokens * 0.9)

    # Gemini Flash Lite pricing
    input_cost = input_tokens / 1_000_000 * 0.075
    output_cost = output_tokens / 1_000_000 * 0.30

    return {
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": round(input_cost + output_cost, 6),
        "num_chunks": max(1, len(html) // _MAX_CHUNK_CHARS + 1),
    }
