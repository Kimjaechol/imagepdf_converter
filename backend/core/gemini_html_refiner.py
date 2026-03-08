"""Gemini-based HTML refinement for PDF conversion output (2nd pass).

After the 1st pass (Upstage Document Parse) produces HTML from a PDF,
this module sends the HTML to Gemini 3.1 Flash-Lite for quality refinement.

Focuses on:
  1. Heading font sizes and bold/italic accuracy vs original
  2. Table structure and cell content correctness
  3. Image placement verification (correct position in document flow)
  4. Text and number correction (OCR errors, displaced digits)

Only called for PDF files (non-PDF uses Hancom DocsConverter which is already accurate).
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
    from backend.core.gemini_client import is_available
    return is_available()


def refine_html(
    html: str,
    original_path: str | None = None,
    doc_type: str = "pdf",
) -> str:
    """Refine Upstage-extracted HTML using Gemini (PDF 2nd pass).

    Focuses on:
    - Heading font sizes and bold/italic accuracy
    - Table structure and cell content correctness
    - Image placement verification
    - Text and number position correction (OCR errors, displaced digits)

    Args:
        html: The Upstage Document Parse output HTML
        original_path: Path to original PDF (for context in prompt)
        doc_type: typically "pdf" (image_pdf or digital_pdf)

    Returns:
        Refined HTML string
    """
    from backend.core.gemini_client import get_api_key

    api_key = get_api_key()
    if not api_key:
        logger.info("Gemini API key not configured, skipping HTML refinement")
        return html

    model_name = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")

    doc_name = Path(original_path).name if original_path else "document"

    # For small documents, process in one shot
    if len(html) <= _MAX_CHUNK_CHARS:
        return _refine_chunk(model_name, api_key, html, doc_name, doc_type)

    # For large documents, split at block boundaries and process chunks
    return _refine_large_html(model_name, api_key, html, doc_name, doc_type)


def _refine_chunk(model_name: str, api_key: str, html: str, doc_name: str, doc_type: str) -> str:
    """Refine a single chunk of PDF-extracted HTML."""
    prompt = f"""You are an HTML document quality reviewer. A PDF file named "{doc_name}" was converted to HTML by Upstage Document Parse (1st pass).

You are performing the 2nd pass quality refinement. Review and fix the HTML below.
Focus on these FOUR critical areas:

1. HEADING FONT SIZE AND BOLD VERIFICATION:
   - Ensure headings (h1-h6) have appropriate font sizes in their style attributes
   - Main titles should be h1 with larger font-size (≥20px) and font-weight: bold
   - Section headings should be h2/h3 with medium font-size (14-18px)
   - If a paragraph looks like a heading (bold, larger text), convert it to proper <hN> tag
   - Ensure heading hierarchy is logical (h1 > h2 > h3, no level skipping)
   - Korean heading patterns: "제1장"/"제1편" = h2, "제1절"/"제1관" = h3, "제1조" = h4

2. TABLE STRUCTURE AND CONTENT:
   - Ensure proper <thead>/<tbody> separation
   - Fix rowspan/colspan if cells appear misaligned
   - Verify cell content is in the correct row/column position
   - Fix merged cells that were incorrectly split
   - Remove empty rows/columns that don't exist in the original
   - Ensure numeric data in cells is correct (no displaced digits)

3. IMAGE PLACEMENT:
   - Verify <img> tags are positioned correctly in the document flow
   - Images should appear at the same relative position as in the original PDF
   - Fix broken image src references
   - Ensure alt text is meaningful

4. TEXT AND NUMBER CORRECTION:
   - Fix OCR errors in text (especially CJK characters)
   - CRITICAL: Fix displaced numbers/digits caused by MuPDF glyph width bugs
     Common patterns:
     - Numbers pushed to end of line: "년도 매출액은 원입니다 2024 1,000,000" → "2024년도 매출액은 1,000,000원입니다"
     - Article numbers detached: "제 조 (목적) 1" → "제1조 (목적)"
     - Dates split: "월 일 3 15" → "3월 15일"
     - Percentage/units displaced: "증가율은 %입니다 5.3" → "증가율은 5.3%입니다"
   - Fix Korean spacing (띄어쓰기) errors
   - Do NOT change the VALUE of any number — only fix its POSITION

PRESERVE:
   - ALL text content meaning (do NOT rewrite, translate, or summarize)
   - All meaningful formatting (bold, italic, underline, colors)
   - All images and their src attributes
   - Document structure and reading order

CRITICAL: Output ONLY the corrected HTML. No explanation, no code fences.
If the HTML is already good, return it unchanged.

HTML to review:
{html}"""

    try:
        from backend.core.gemini_client import generate_content
        refined = generate_content(
            prompt, model=model_name, api_key=api_key,
        ).strip()

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


def _refine_large_html(model_name: str, api_key: str, html: str, doc_name: str, doc_type: str) -> str:
    """Refine a large HTML document by splitting into chunks."""
    # Extract head and body
    body_start = html.find("<body")
    body_end = html.rfind("</body>")

    if body_start == -1 or body_end == -1:
        # No body tags, try to process as single chunk anyway
        if len(html) <= _MAX_CHUNK_CHARS * 2:
            return _refine_chunk(model_name, api_key, html, doc_name, doc_type)
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
        refined = _refine_chunk(model_name, api_key, chunk, doc_name, doc_type)
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
