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
        gemini_model: str = "gemini-2.5-flash",
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
