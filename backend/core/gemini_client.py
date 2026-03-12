"""Lightweight Gemini API client using direct REST calls (no google SDK).

Replaces google.generativeai SDK which has compatibility issues in some
environments due to pyo3/cryptography module conflicts.

API: https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Default model
DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"


def get_api_key() -> str:
    """Get Gemini API key from environment."""
    return os.environ.get("GEMINI_API_KEY", "")


def generate_content(
    prompt: str | list[dict],
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    temperature: float = 0.1,
    max_output_tokens: int = 8192,
    timeout: float = 120,
) -> str:
    """Call Gemini generateContent API and return the text response.

    Args:
        prompt: Either a text string or a list of content parts
               (for multimodal). Each part is a dict like:
               {"text": "..."} or {"inline_data": {"mime_type": "image/png", "data": "<base64>"}}
        model: Gemini model name.
        api_key: API key (defaults to GEMINI_API_KEY env var).
        temperature: Sampling temperature.
        max_output_tokens: Max response tokens.
        timeout: Request timeout in seconds.

    Returns:
        The text content from the first candidate response.
    """
    key = api_key or get_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")

    url = f"{_GEMINI_API_BASE}/models/{model}:generateContent?key={key}"

    # Build request body
    if isinstance(prompt, str):
        contents = [{"parts": [{"text": prompt}]}]
    else:
        # prompt is already a list of parts
        contents = [{"parts": prompt}]

    body = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        },
    }

    response = httpx.post(
        url,
        json=body,
        timeout=timeout,
        headers={"Content-Type": "application/json"},
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Gemini API error (HTTP {response.status_code}): {response.text[:500]}"
        )

    data = response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        # Check for blocked content
        block_reason = data.get("promptFeedback", {}).get("blockReason", "")
        if block_reason:
            raise RuntimeError(f"Gemini blocked content: {block_reason}")
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(data)[:500]}")

    parts = candidates[0].get("content", {}).get("parts", [])
    text_parts = [p.get("text", "") for p in parts if "text" in p]
    return "".join(text_parts)


def generate_with_image(
    text_prompt: str,
    image_data: bytes,
    mime_type: str = "image/png",
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    temperature: float = 0.1,
    max_output_tokens: int = 8192,
    timeout: float = 120,
) -> str:
    """Call Gemini with text + image (multimodal).

    Args:
        text_prompt: The text instruction.
        image_data: Raw image bytes.
        mime_type: Image MIME type.
        model: Gemini model name.

    Returns:
        The text content from the response.
    """
    b64 = base64.b64encode(image_data).decode("utf-8")
    parts = [
        {"text": text_prompt},
        {"inline_data": {"mime_type": mime_type, "data": b64}},
    ]
    return generate_content(
        parts, model=model, api_key=api_key,
        temperature=temperature, max_output_tokens=max_output_tokens,
        timeout=timeout,
    )


def generate_with_images(
    text_prompt: str,
    images: list[tuple[bytes, str]],
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    temperature: float = 0.1,
    max_output_tokens: int = 8192,
    timeout: float = 180,
) -> str:
    """Call Gemini with text + multiple images.

    Args:
        text_prompt: The text instruction.
        images: List of (image_bytes, mime_type) tuples.
        model: Gemini model name.

    Returns:
        The text content from the response.
    """
    parts = [{"text": text_prompt}]
    for img_data, mime in images:
        b64 = base64.b64encode(img_data).decode("utf-8")
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})

    return generate_content(
        parts, model=model, api_key=api_key,
        temperature=temperature, max_output_tokens=max_output_tokens,
        timeout=timeout,
    )


def is_available() -> bool:
    """Check if Gemini API key is configured."""
    return bool(get_api_key())
