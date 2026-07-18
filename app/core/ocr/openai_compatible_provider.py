"""OpenAI-compatible / 百炼视觉 OCR Provider。"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx

from app.core.ocr.errors import OcrError
from app.core.ocr.protocol import OcrRecognizeResult
from app.core.ocr.url_safety import assert_safe_ocr_url
from app.settings import Settings

logger = logging.getLogger(__name__)


class OpenAICompatibleOcrProvider:
    """通过 chat/completions 多模态接口做 OCR。"""

    name = "openai_compatible"

    def __init__(self, settings: Settings, *, driver_name: str = "openai_compatible") -> None:
        self._settings = settings
        self.name = driver_name

    async def recognize(
        self,
        image_bytes: bytes,
        *,
        lang: str | None = None,
    ) -> OcrRecognizeResult:
        base_url = (self._settings.ocr_api_base_url or "").rstrip("/")
        if not base_url:
            raise OcrError("RAG_OCR_API_BASE_URL is required for API OCR")
        api_key = (
            self._settings.ocr_api_key
            or os.getenv("DASHSCOPE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        if not api_key:
            raise OcrError("RAG_OCR_API_KEY (or DASHSCOPE_API_KEY) is required")

        b64 = base64.b64encode(image_bytes).decode("ascii")
        mime = "image/png"
        prompt = self._settings.ocr_api_prompt or (
            "Extract all readable text from this image. "
            "Return plain text only, no markdown."
        )
        if lang:
            prompt = f"{prompt} Preferred language hint: {lang}."

        payload: dict[str, Any] = {
            "model": self._settings.ocr_api_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                }
            ],
            "temperature": 0,
        }
        url = f"{base_url}/chat/completions"
        assert_safe_ocr_url(url, allow_private=self._settings.ocr_allow_private_urls)
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.ocr_api_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as e:
            raise OcrError(f"OCR API request failed: {e}") from e

        try:
            text = str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise OcrError("OCR API returned invalid response") from e

        return OcrRecognizeResult(text=text, confidence=None, raw=data, empty=not bool(text))


class BailianOcrProvider(OpenAICompatibleOcrProvider):
    """百炼/DashScope OpenAI-compatible 视觉接口。"""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, driver_name="bailian")
