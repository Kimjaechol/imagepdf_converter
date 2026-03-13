"""Multi-LLM HTML correction module – user-provided API keys.

Supports Gemini, OpenAI, and Anthropic Claude for optional post-processing
of converted HTML. The user provides their own API key in the app settings;
if no key is provided, correction is skipped entirely.

Each provider uses its latest fast model for cost-effective correction:
  - Gemini:    gemini-2.5-flash-lite (latest)
  - OpenAI:    gpt-4.1-mini (latest)
  - Claude:    claude-sonnet-4-5-20250514 (latest)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Default models – latest fast models per provider
DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash-lite",
    "openai": "gpt-4.1-mini",
    "claude": "claude-sonnet-4-5-20250514",
}

# Max characters per correction chunk (to stay within token limits)
_MAX_CHUNK_CHARS = 30_000


@dataclass
class LLMCorrectorConfig:
    """Configuration for LLM-based HTML correction."""
    # Provider: "gemini" | "openai" | "claude" | "" (disabled)
    provider: str = ""
    # API key (user-provided, stored locally only)
    api_key: str = ""
    # Model override (empty = use default for provider)
    model: str = ""
    # Max retries
    max_retries: int = 2
    # Temperature (low for correction tasks)
    temperature: float = 0.1

    @property
    def effective_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.provider, "")

    @property
    def is_enabled(self) -> bool:
        return bool(self.provider and self.api_key)


class LLMCorrector:
    """Correct and refine HTML output using user-provided LLM API keys.

    This runs locally on the user's machine. The API key is never sent
    to the Railway server – it goes directly from the user's machine
    to the LLM provider.
    """

    def __init__(self, config: LLMCorrectorConfig):
        self.config = config

    def correct_html(
        self,
        html: str,
        source_type: str = "pdf",
        progress_callback: Any = None,
    ) -> str:
        """Correct HTML content using the configured LLM provider.

        Args:
            html: The HTML content to correct.
            source_type: "image_pdf" | "digital_pdf" | "document"
            progress_callback: Optional fn(message, progress_pct)

        Returns:
            Corrected HTML string. Returns original if correction fails.
        """
        if not self.config.is_enabled:
            logger.info("LLM correction disabled (no API key configured)")
            return html

        if not html or not html.strip():
            return html

        provider = self.config.provider.lower()

        # Split HTML into manageable chunks
        chunks = self._split_html_for_correction(html)
        total_chunks = len(chunks)

        if progress_callback:
            progress_callback("LLM 교정 시작", 0.0)

        corrected_chunks = []
        for i, chunk in enumerate(chunks):
            if progress_callback:
                pct = (i / total_chunks)
                progress_callback(
                    f"LLM 교정 중 ({i+1}/{total_chunks})", pct,
                )

            try:
                if provider == "gemini":
                    result = self._correct_with_gemini(chunk, source_type)
                elif provider == "openai":
                    result = self._correct_with_openai(chunk, source_type)
                elif provider == "claude":
                    result = self._correct_with_claude(chunk, source_type)
                else:
                    logger.warning("Unknown LLM provider: %s", provider)
                    result = chunk

                corrected_chunks.append(result)

            except Exception as exc:
                logger.error(
                    "LLM correction failed for chunk %d/%d: %s",
                    i + 1, total_chunks, exc,
                )
                corrected_chunks.append(chunk)  # Keep original on failure

        if progress_callback:
            progress_callback("LLM 교정 완료", 1.0)

        return "\n".join(corrected_chunks)

    # ------------------------------------------------------------------
    # Provider implementations
    # ------------------------------------------------------------------

    def _correct_with_gemini(self, html_chunk: str, source_type: str) -> str:
        """Correct HTML using Google Gemini API."""
        import google.generativeai as genai

        genai.configure(api_key=self.config.api_key)
        model = genai.GenerativeModel(self.config.effective_model)

        prompt = self._build_correction_prompt(html_chunk, source_type)

        for attempt in range(self.config.max_retries + 1):
            try:
                response = model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=self.config.temperature,
                        max_output_tokens=8192,
                    ),
                )
                result = response.text.strip()
                return self._clean_llm_output(result)
            except Exception as exc:
                if attempt < self.config.max_retries:
                    logger.warning(
                        "Gemini attempt %d failed: %s", attempt + 1, exc,
                    )
                else:
                    raise

        return html_chunk

    def _correct_with_openai(self, html_chunk: str, source_type: str) -> str:
        """Correct HTML using OpenAI API."""
        import openai

        client = openai.OpenAI(api_key=self.config.api_key)
        prompt = self._build_correction_prompt(html_chunk, source_type)

        for attempt in range(self.config.max_retries + 1):
            try:
                response = client.chat.completions.create(
                    model=self.config.effective_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are an expert document correction assistant. "
                                "Fix OCR/conversion errors in HTML while preserving "
                                "all HTML structure, tags, and formatting exactly."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=self.config.temperature,
                    max_tokens=8192,
                )
                result = response.choices[0].message.content.strip()
                return self._clean_llm_output(result)
            except Exception as exc:
                if attempt < self.config.max_retries:
                    logger.warning(
                        "OpenAI attempt %d failed: %s", attempt + 1, exc,
                    )
                else:
                    raise

        return html_chunk

    def _correct_with_claude(self, html_chunk: str, source_type: str) -> str:
        """Correct HTML using Anthropic Claude API."""
        import anthropic

        client = anthropic.Anthropic(api_key=self.config.api_key)
        prompt = self._build_correction_prompt(html_chunk, source_type)

        for attempt in range(self.config.max_retries + 1):
            try:
                response = client.messages.create(
                    model=self.config.effective_model,
                    max_tokens=8192,
                    temperature=self.config.temperature,
                    system=(
                        "You are an expert document correction assistant. "
                        "Fix OCR/conversion errors in HTML while preserving "
                        "all HTML structure, tags, and formatting exactly."
                    ),
                    messages=[{"role": "user", "content": prompt}],
                )
                result = response.content[0].text.strip()
                return self._clean_llm_output(result)
            except Exception as exc:
                if attempt < self.config.max_retries:
                    logger.warning(
                        "Claude attempt %d failed: %s", attempt + 1, exc,
                    )
                else:
                    raise

        return html_chunk

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_correction_prompt(
        self, html_chunk: str, source_type: str,
    ) -> str:
        """Build the correction prompt for LLM."""
        source_desc = {
            "image_pdf": "scanned/image PDF (OCR-extracted)",
            "digital_pdf": "digital PDF (text-extracted)",
            "document": "office document (converted from HWP/DOCX/etc.)",
        }.get(source_type, "document")

        return f"""다음은 {source_desc}에서 변환된 HTML입니다.
텍스트의 오류를 교정해주세요.

교정 규칙:
1. HTML 태그, 속성, CSS 스타일은 절대 수정하지 마세요
2. 텍스트 내용만 교정하세요 (오타, OCR 오류, 깨진 문자)
3. 표(table)의 구조는 유지하세요
4. 문맥에 맞지 않는 글자/단어를 올바르게 수정하세요
5. 한국어 맞춤법/띄어쓰기 오류를 교정하세요
6. 숫자/날짜/고유명사는 원본을 존중하세요
7. 코드 펜스나 설명 없이 교정된 HTML만 출력하세요
8. 원본과 동일한 줄바꿈/공백 패턴을 유지하세요

HTML:
{html_chunk}"""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _split_html_for_correction(self, html: str) -> list[str]:
        """Split HTML into chunks for LLM processing.

        Splits on block-level elements to avoid breaking tags.
        """
        if len(html) <= _MAX_CHUNK_CHARS:
            return [html]

        # Find body content if present
        body_start = html.find("<body")
        body_end = html.rfind("</body>")

        if body_start == -1 or body_end == -1:
            head_part = ""
            body_content = html
            tail_part = ""
        else:
            body_close = html.find(">", body_start)
            if body_close == -1:
                head_part = ""
                body_content = html
                tail_part = ""
            else:
                body_tag_end = body_close + 1
                head_part = html[:body_tag_end]
                body_content = html[body_tag_end:body_end]
                tail_part = html[body_end:]

        # Split body content on block-level tags
        parts = re.split(
            r'(?=<(?:div|h[1-6]|p|table|section|article)[\s>])',
            body_content,
        )

        chunks = []
        current = ""
        for part in parts:
            if len(current) + len(part) > _MAX_CHUNK_CHARS and current:
                chunks.append(current)
                current = part
            else:
                current += part
        if current:
            chunks.append(current)

        # Re-add head/tail to first/last chunks
        if head_part and chunks:
            chunks[0] = head_part + chunks[0]
        if tail_part and chunks:
            chunks[-1] = chunks[-1] + tail_part

        return chunks

    @staticmethod
    def _clean_llm_output(text: str) -> str:
        """Remove markdown code fences that LLMs sometimes add."""
        if text.startswith("```html"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()


# ------------------------------------------------------------------
# Convenience factory
# ------------------------------------------------------------------

def create_corrector(
    provider: str = "",
    api_key: str = "",
    model: str = "",
) -> LLMCorrector | None:
    """Create an LLM corrector if provider and key are given.

    Returns None if correction is not configured.
    """
    if not provider or not api_key:
        return None

    config = LLMCorrectorConfig(
        provider=provider.lower(),
        api_key=api_key,
        model=model,
    )

    if not config.is_enabled:
        return None

    return LLMCorrector(config)
