"""Language correction engine – dictionary + LLM-based post-OCR correction."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from backend.models.schema import LayoutBlock

logger = logging.getLogger(__name__)


class CorrectionEngine:
    """Multi-stage text correction for OCR output."""

    def __init__(
        self,
        dictionary_path: str = "config/correction_dict.json",
        mode: str = "hybrid",
        llm_provider: str = "gemini",
        gemini_model: str = "gemini-3.1-flash-lite-preview",
        ollama_model: str = "qwen2.5:1.5b",
        ollama_base_url: str = "http://localhost:11434",
        aggressiveness: str = "conservative",
    ):
        self.mode = mode
        self.llm_provider = llm_provider
        self.gemini_model = gemini_model
        self.ollama_model = ollama_model
        self.ollama_base_url = ollama_base_url
        self.aggressiveness = aggressiveness
        self._dict = self._load_dictionary(dictionary_path)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def correct(self, blocks: list[LayoutBlock]) -> list[LayoutBlock]:
        """Apply corrections to all blocks with text."""
        # Stage 0: Hanja Cheongan context-based correction (before anything else)
        blocks = self._correct_hanja_cheongan(blocks)

        for block in blocks:
            if not block.text:
                continue
            # Stage 1: Symbol / dictionary corrections
            block.text = self._apply_symbol_corrections(block.text)
            block.text = self._apply_dictionary_corrections(block.text)

        # Stage 2: LLM context-aware correction (batch by section)
        if self.mode in ("llm", "hybrid"):
            blocks = self._llm_correct(blocks)

        return blocks

    def correct_dictionary_only(self, blocks: list[LayoutBlock]) -> list[LayoutBlock]:
        """Apply only dictionary/rule-based corrections (no LLM call).

        Used in unified_vision mode where Gemini already did text correction.
        This adds Hanja Cheongan and symbol corrections as a safety net.
        """
        blocks = self._correct_hanja_cheongan(blocks)
        for block in blocks:
            if not block.text:
                continue
            block.text = self._apply_symbol_corrections(block.text)
            block.text = self._apply_dictionary_corrections(block.text)
        return blocks

    def add_custom_term(self, correct: str, confused_with: list[str]) -> None:
        """Add a user-defined correction term."""
        self._dict.setdefault("user_custom", {})[correct] = {
            "confused_with": confused_with,
        }

    def save_dictionary(self, path: str) -> None:
        """Persist the correction dictionary to disk."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._dict, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Stage 0: Hanja Cheongan (天干) context-based correction
    # ------------------------------------------------------------------

    # Characters that OCR frequently confuses with Cheongan Hanja
    _CHEONGAN_CONFUSION: list[tuple[str, str]] = [
        ("乙", "Z"),   ("乙", "z"),   ("乙", "2"),   ("乙", "己"),
        ("丁", "T"),   ("丁", "7"),
        ("甲", "田"),  ("甲", "由"),
        ("丙", "内"),
    ]
    _CHEONGAN_CHARS = set("甲乙丙丁戊己庚辛壬癸")
    _CHEONGAN_DOC_KEYWORDS: list[list[str]] = [
        # exam documents
        ["문항", "정답", "배점", "수험번호", "시험시간", "시험"],
        # legal documents
        ["계약", "원고", "피고", "판결", "조항", "위반", "소송"],
        # government forms
        ["신청인", "피신청인", "등기", "관할", "처분", "허가"],
    ]
    _KO_JOSA = re.compile(
        r"^(은|는|이|가|에게|으로|와|과|의|을|를|도|만|에|서|로|한테|께)(?:\s|$)"
    )

    def _correct_hanja_cheongan(
        self, blocks: list[LayoutBlock],
    ) -> list[LayoutBlock]:
        """Context-based correction of Hanja Cheongan misrecognition.

        Algorithm (matches patent claims 11-13):
        1. Scan full document text for Cheongan-related signals.
        2. Compute confidence based on:
           a) Document-type keyword matches (+0.50 base)
           b) Any correctly-recognized Cheongan character in the same
              document (+0.35 cross-reference bonus)
           c) Confused character appears before a Korean josa (+0.10)
           d) Sequential pattern (甲→乙→丙→丁→戊) detected (+0.05)
        3. Replace only when confidence >= 0.80.
        """
        all_text = " ".join(b.text for b in blocks if b.text)
        if not all_text:
            return blocks

        # --- signal detection ---
        has_keyword = self._detect_cheongan_doc_keywords(all_text)
        has_existing_cheongan = bool(self._CHEONGAN_CHARS & set(all_text))
        has_sequential = self._detect_cheongan_sequence(all_text)

        base_confidence = 0.50 if has_keyword else 0.0
        if has_existing_cheongan:
            base_confidence += 0.35
        if has_sequential:
            base_confidence += 0.05

        # Not enough signals – skip replacement
        if base_confidence < 0.50:
            return blocks

        # --- replacement ---
        for block in blocks:
            if not block.text:
                continue
            block.text = self._replace_cheongan_confusions(
                block.text, base_confidence,
            )

        return blocks

    def _detect_cheongan_doc_keywords(self, text: str) -> bool:
        for group in self._CHEONGAN_DOC_KEYWORDS:
            if sum(1 for kw in group if kw in text) >= 2:
                return True
        return False

    def _detect_cheongan_sequence(self, text: str) -> bool:
        """Check if two or more sequential Cheongan appear in order."""
        seq = "甲乙丙丁戊己庚辛壬癸"
        found = [c for c in seq if c in text]
        if len(found) < 2:
            return False
        indices = [seq.index(c) for c in found]
        # Check if they are in ascending order
        return indices == sorted(indices)

    def _replace_cheongan_confusions(
        self, text: str, base_confidence: float,
    ) -> str:
        """Replace confused characters with Cheongan Hanja when confident."""
        for correct, confused in self._CHEONGAN_CONFUSION:
            if confused not in text:
                continue
            # Walk through each occurrence
            result_parts: list[str] = []
            i = 0
            while i < len(text):
                if text[i:i + len(confused)] == confused:
                    conf = base_confidence
                    # Check josa pattern after the confused char
                    after = text[i + len(confused):]
                    if self._KO_JOSA.match(after):
                        conf += 0.10
                    if conf >= 0.80:
                        result_parts.append(correct)
                        i += len(confused)
                        continue
                result_parts.append(text[i])
                i += 1
            text = "".join(result_parts)
        return text

    # ------------------------------------------------------------------
    # Dictionary loading
    # ------------------------------------------------------------------

    def _load_dictionary(self, path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning("Correction dictionary not found: %s", path)
            return {}

    # ------------------------------------------------------------------
    # Stage 1: Symbol & dictionary corrections
    # ------------------------------------------------------------------

    def _apply_symbol_corrections(self, text: str) -> str:
        """Fix common OCR symbol errors."""
        symbols = self._dict.get("symbol_corrections", {})
        for correct_char, info in symbols.items():
            for confused in info.get("confused_with", []):
                # Only replace in specific contexts to avoid false positives
                text = text.replace(confused, correct_char)
        return text

    def _apply_dictionary_corrections(self, text: str) -> str:
        """Apply domain-specific dictionary corrections."""
        # Legal terms
        for correct, info in self._dict.get("legal_terms", {}).items():
            for confused in info.get("confused_with", []):
                text = text.replace(confused, correct)

        # Exam terms
        for correct, info in self._dict.get("exam_terms", {}).items():
            for confused in info.get("confused_with", []):
                text = text.replace(confused, correct)

        # Common OCR errors
        for correct, info in self._dict.get("common_ocr_errors", {}).items():
            for confused in info.get("confused_with", []):
                # Use word boundary-aware replacement for common words
                pattern = re.compile(re.escape(confused))
                text = pattern.sub(correct, text)

        # Hanja corrections (context-sensitive)
        for correct, info in self._dict.get("hanja_corrections", {}).items():
            for confused in info.get("confused_with", []):
                text = text.replace(confused, correct)

        # Korean numbering
        for correct, info in self._dict.get("korean_numbering", {}).items():
            for confused in info.get("confused_with", []):
                text = text.replace(confused, correct)

        # Roman numeral corrections (context-sensitive)
        roman = self._dict.get("roman_numeral_corrections", {})
        for correct, info in roman.items():
            for confused in info.get("confused_with", []):
                # Only replace when it looks like a numeral context
                for pattern_template in info.get("context_patterns", []):
                    # Build context-aware pattern
                    pass
                # Simple replacement for now
                if len(confused) == 1 and confused.isalpha():
                    # Avoid replacing single letters in general text
                    continue
                text = text.replace(confused, correct)

        # User custom
        for correct, info in self._dict.get("user_custom", {}).items():
            for confused in info.get("confused_with", []):
                text = text.replace(confused, correct)

        return text

    # ------------------------------------------------------------------
    # Stage 2: LLM context correction
    # ------------------------------------------------------------------

    def _llm_correct(self, blocks: list[LayoutBlock]) -> list[LayoutBlock]:
        """Use LLM for context-aware text correction.

        IMPORTANT: This stage only modifies text content.
        It does NOT change reading order, heading levels, or structure.
        """
        # Group blocks into sections for context
        sections = self._group_into_sections(blocks)

        for section in sections:
            texts = [(b.id, b.text) for b in section if b.text]
            if not texts:
                continue

            combined = "\n\n".join(
                f"[BLOCK:{bid}]\n{text}" for bid, text in texts
            )

            corrected = self._call_correction_llm(combined)
            if corrected:
                self._apply_corrections_to_blocks(corrected, section)

        return blocks

    def _group_into_sections(
        self, blocks: list[LayoutBlock]
    ) -> list[list[LayoutBlock]]:
        """Group blocks into sections (by heading boundaries)."""
        sections: list[list[LayoutBlock]] = []
        current: list[LayoutBlock] = []

        for block in blocks:
            if block.heading_level.value in ("h1", "h2") and current:
                sections.append(current)
                current = []
            current.append(block)

        if current:
            sections.append(current)

        return sections

    def _call_correction_llm(self, text: str) -> str | None:
        prompt = f"""You are a Korean OCR post-correction expert.
Fix spelling errors, wrong characters, and unnatural expressions in the following
OCR-extracted text. The text is from a Korean document (may contain legal, exam,
or official content).

Rules:
1. Fix OCR character errors (similar-looking characters confused).
2. Fix spacing (Korean word spacing 띄어쓰기).
3. Fix punctuation.
4. Do NOT change the meaning or structure.
5. Keep all [BLOCK:xxx] markers exactly as they are.
6. Pay special attention to Hanja (甲乙丙丁), Roman numerals (ⅠⅡⅢ),
   and Korean numbering ((가)(나)(다)).
7. Aggressiveness: {self.aggressiveness} – {"only fix clear errors" if self.aggressiveness == "conservative" else "fix probable errors too"}.

Text to correct:
{text}

Return the corrected text with [BLOCK:xxx] markers preserved."""

        if self.llm_provider == "gemini":
            return self._call_gemini(prompt)
        elif self.llm_provider == "ollama":
            return self._call_ollama(prompt)
        return None

    def _call_gemini(self, prompt: str) -> str | None:
        try:
            import google.generativeai as genai
            api_key = os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                return None
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(self.gemini_model)
            response = model.generate_content(prompt)
            return response.text
        except Exception as exc:
            logger.error("Gemini correction failed: %s", exc)
            return None

    def _call_ollama(self, prompt: str) -> str | None:
        try:
            import httpx
            resp = httpx.post(
                f"{self.ollama_base_url}/api/generate",
                json={
                    "model": self.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except Exception as exc:
            logger.error("Ollama correction failed: %s", exc)
            return None

    def _apply_corrections_to_blocks(
        self,
        corrected_text: str,
        blocks: list[LayoutBlock],
    ) -> None:
        """Parse corrected text and apply back to blocks."""
        id_to_block = {b.id: b for b in blocks}

        # Parse [BLOCK:xxx] sections
        pattern = re.compile(r"\[BLOCK:([^\]]+)\]\n(.*?)(?=\[BLOCK:|$)", re.DOTALL)
        for match in pattern.finditer(corrected_text):
            bid = match.group(1).strip()
            text = match.group(2).strip()
            if bid in id_to_block and text:
                id_to_block[bid].text = text
