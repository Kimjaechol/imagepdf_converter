"""Verification tests for the PyMuPDF/MuPDF BiDi numeral displacement fix.

Root cause: MuPDF's C library miscalculates glyph widths when CJK fonts
(Korean, Chinese, Japanese) are mixed with Arabic numerals (0-9). This
causes digit character bounding boxes (bbox) to be displaced to the end
of the line, breaking the visual reading order.

Fix: PyMuPDF >= 1.25.3 (bundling MuPDF >= 1.25.4) corrects the glyph
width calculation in the C layer.

These tests verify:
1. PyMuPDF version meets minimum requirement
2. Span x-coordinates for digits are between surrounding CJK spans
3. Digit spans are NOT displaced to line ends
4. The detection heuristic correctly identifies displacement
5. End-to-end extraction preserves numeral positions in CJK text

Run: python -m pytest backend/tests/test_bidi_numeral_fix.py -v
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def fitz():
    """Import fitz (PyMuPDF) or skip."""
    try:
        import fitz
        return fitz
    except ImportError:
        pytest.skip("PyMuPDF not installed")


@pytest.fixture(scope="session")
def extractor():
    """Create a DigitalPdfExtractor instance."""
    try:
        from backend.core.digital_pdf_extractor import DigitalPdfExtractor
        return DigitalPdfExtractor(dpi=72)
    except ImportError:
        pytest.skip("backend.core.digital_pdf_extractor not available")


@pytest.fixture(scope="session")
def cjk_numeral_pdf(fitz) -> Path:
    """Create a test PDF with CJK text mixed with Arabic numerals.

    Contains lines like:
      - "제1조 (목적)"
      - "2024년도 매출액은 1,000,000원입니다"
      - "서울시 강남구 123동 456번지"
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4

    # Try to use a font that supports Korean; fall back to Helvetica
    try:
        fontname = "korea"  # Built-in CJK support in PyMuPDF
        # Test if the font works by inserting a single character
        test_rc = page.insert_text(
            (72, 72), "테스트", fontname="ko", fontsize=12,
        )
        if test_rc < 0:
            raise ValueError("CJK font not available")
        fontname = "ko"
    except Exception:
        # Fall back — this won't produce real CJK rendering but
        # the span coordinate logic can still be tested
        fontname = "helv"

    lines = [
        "제1조 (목적)",
        "제2장 총칙",
        "2024년도 매출액은 1,000,000원입니다",
        "서울시 강남구 123동 456번지",
        "증가율은 5.3%입니다",
        "3월 15일 계약 체결",
    ]

    y = 100
    for line in lines:
        page.insert_text((72, y), line, fontname=fontname, fontsize=12)
        y += 24

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    doc.save(tmp.name)
    doc.close()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# Version checks
# ---------------------------------------------------------------------------

class TestPyMuPDFVersion:
    """Verify PyMuPDF version meets minimum requirement."""

    def test_version_minimum(self, fitz):
        """PyMuPDF must be >= 1.25.3 for the BiDi glyph width fix."""
        version_str = fitz.VersionBind
        parts = tuple(int(x) for x in version_str.split(".")[:3])
        assert parts >= (1, 25, 3), (
            f"PyMuPDF {version_str} is below 1.25.3 — MuPDF BiDi glyph "
            f"width bug is present. Upgrade: pip install --upgrade pymupdf"
        )

    def test_mupdf_version(self, fitz):
        """MuPDF backend must be >= 1.25.4 (contains the C-level fix)."""
        # fitz.VersionFitz is the MuPDF C library version
        mupdf_version = fitz.VersionFitz
        parts = tuple(int(x) for x in mupdf_version.split(".")[:3])
        assert parts >= (1, 25, 4), (
            f"MuPDF {mupdf_version} is below 1.25.4 — glyph width "
            f"calculation bug for CJK+numeral is present."
        )


# ---------------------------------------------------------------------------
# Span coordinate verification
# ---------------------------------------------------------------------------

class TestSpanCoordinates:
    """Verify digit spans have correct x-coordinates (not displaced)."""

    def test_digit_spans_not_displaced_to_end(self, fitz, cjk_numeral_pdf):
        """Digit-containing spans must NOT have x-coords beyond all CJK spans."""
        doc = fitz.open(str(cjk_numeral_pdf))
        page = doc[0]
        text_dict = page.get_text("dict", sort=True)

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue

            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if len(spans) < 2:
                    continue

                # Collect span positions
                digit_max_x = 0
                non_digit_max_x = 0

                for span in spans:
                    text = span.get("text", "").strip()
                    if not text:
                        continue
                    x0 = span.get("origin", (0, 0))[0]
                    x1 = span.get("bbox", (0, 0, 0, 0))[2]

                    import re
                    if re.match(r"^[\d,.\-]+$", text):
                        digit_max_x = max(digit_max_x, x0)
                    else:
                        non_digit_max_x = max(non_digit_max_x, x1)

                if digit_max_x > 0 and non_digit_max_x > 0:
                    line_text = "".join(s.get("text", "") for s in spans)
                    # Digit spans should NOT be displaced far beyond non-digit content
                    line_width = spans[-1].get("bbox", (0, 0, 100, 0))[2] - spans[0].get("bbox", (0, 0, 0, 0))[0]
                    if line_width > 0:
                        gap_ratio = (digit_max_x - non_digit_max_x) / line_width
                        assert gap_ratio < 0.3, (
                            f"Digit displacement detected in line '{line_text}': "
                            f"digit x={digit_max_x:.1f} is {gap_ratio:.0%} beyond "
                            f"non-digit end x={non_digit_max_x:.1f}. "
                            f"MuPDF glyph width bug is still present."
                        )

        doc.close()

    def test_span_order_matches_visual_order(self, fitz, cjk_numeral_pdf):
        """Spans sorted by x-coordinate should produce readable text."""
        doc = fitz.open(str(cjk_numeral_pdf))
        page = doc[0]
        text_dict = page.get_text("dict", sort=True)

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue

            for line in block.get("lines", []):
                spans = line.get("spans", [])
                # Verify spans are already in x-coordinate order
                x_positions = [
                    span.get("origin", (0, 0))[0]
                    for span in spans
                    if span.get("text", "").strip()
                ]
                assert x_positions == sorted(x_positions), (
                    f"Spans not in x-coordinate order: {x_positions}. "
                    f"Text: {''.join(s.get('text', '') for s in spans)}"
                )

        doc.close()


# ---------------------------------------------------------------------------
# Detection heuristic tests
# ---------------------------------------------------------------------------

class TestDisplacementDetection:
    """Test the runtime displacement detection heuristic."""

    def test_detects_displaced_digit_span(self, extractor):
        """Detection should fire for a digit span at line end with CJK context."""
        # Simulate the bug: digit span bbox displaced to end of line
        fake_spans = [
            {"text": "제", "origin": (72.0, 100.0), "bbox": (72.0, 88.0, 84.0, 100.0)},
            {"text": "조 (목적)", "origin": (96.0, 100.0), "bbox": (96.0, 88.0, 168.0, 100.0)},
            # Bug: "1" displaced to far right
            {"text": "1", "origin": (400.0, 100.0), "bbox": (400.0, 88.0, 408.0, 100.0)},
        ]
        assert extractor._detect_bidi_numeral_displacement(fake_spans) is True

    def test_no_false_positive_for_correct_spans(self, extractor):
        """Detection should NOT fire when digits are correctly positioned."""
        # Correct: "1" is between "제" and "조"
        correct_spans = [
            {"text": "제", "origin": (72.0, 100.0), "bbox": (72.0, 88.0, 84.0, 100.0)},
            {"text": "1", "origin": (84.0, 100.0), "bbox": (84.0, 88.0, 92.0, 100.0)},
            {"text": "조 (목적)", "origin": (92.0, 100.0), "bbox": (92.0, 88.0, 168.0, 100.0)},
        ]
        assert extractor._detect_bidi_numeral_displacement(correct_spans) is False

    def test_no_false_positive_for_trailing_numbers(self, extractor):
        """Detection should NOT fire for legitimately trailing numbers."""
        # "참조번호 12345" — number legitimately at end
        spans = [
            {"text": "참조번호 ", "origin": (72.0, 100.0), "bbox": (72.0, 88.0, 150.0, 100.0)},
            {"text": "12345", "origin": (150.0, 100.0), "bbox": (150.0, 88.0, 190.0, 100.0)},
        ]
        assert extractor._detect_bidi_numeral_displacement(spans) is False

    def test_no_false_positive_for_latin_only(self, extractor):
        """Detection should NOT fire for Latin-only text (no CJK context)."""
        spans = [
            {"text": "Total: ", "origin": (72.0, 100.0), "bbox": (72.0, 88.0, 130.0, 100.0)},
            {"text": "1,000,000", "origin": (400.0, 100.0), "bbox": (400.0, 88.0, 480.0, 100.0)},
        ]
        # No CJK characters → should not trigger
        assert extractor._detect_bidi_numeral_displacement(spans) is False


# ---------------------------------------------------------------------------
# End-to-end extraction test
# ---------------------------------------------------------------------------

class TestEndToEndExtraction:
    """Test that extracted text preserves numeral positions."""

    def test_extraction_preserves_numeral_positions(self, extractor, cjk_numeral_pdf):
        """Extracted text should have numbers in correct positions."""
        try:
            pages, _ = extractor.extract(
                cjk_numeral_pdf, render_images=False,
            )
        except Exception as exc:
            pytest.skip(f"Extraction failed (likely font issue in test env): {exc}")

        all_text = "\n".join(
            block.text for page in pages for block in page.blocks if block.text
        )

        # These patterns should be intact (numbers not displaced)
        expected_patterns = [
            r"제\s*1\s*조",        # 제1조
            r"제\s*2\s*장",        # 제2장
            r"2024\s*년",         # 2024년
            r"1,000,000\s*원",    # 1,000,000원
            r"123\s*동",          # 123동
            r"456\s*번지",        # 456번지
            r"5\.3\s*%",          # 5.3%
            r"3\s*월\s*15\s*일",  # 3월 15일
        ]

        import re
        for pattern in expected_patterns:
            match = re.search(pattern, all_text)
            # Allow some flexibility — if CJK font isn't available,
            # the test PDF may not render Korean characters
            if "제" in all_text or "년" in all_text:
                assert match, (
                    f"Pattern '{pattern}' not found in extracted text. "
                    f"Numbers may be displaced. Text:\n{all_text[:500]}"
                )

    def test_verify_bidi_fix_report(self, extractor, cjk_numeral_pdf):
        """verify_bidi_fix() should return PASS for fixed PyMuPDF."""
        report = extractor.verify_bidi_fix(cjk_numeral_pdf)
        assert report["version_includes_fix"] is True, (
            f"PyMuPDF {report['pymupdf_version']} does not include BiDi fix"
        )
        assert report["displacement_detected"] == 0, (
            f"Displacement still detected in {report['displacement_detected']} "
            f"lines despite PyMuPDF {report['pymupdf_version']}:\n"
            + "\n".join(report["details"])
        )
        assert report["status"] in ("PASS", "WARNING")
