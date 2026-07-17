"""可配置的自定义 HTTP OCR Provider。"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import httpx

from app.core.ocr.errors import OcrError
from app.core.ocr.protocol import OcrRecognizeResult
from app.core.ocr.url_safety import assert_safe_ocr_url
from app.settings import Settings

logger = logging.getLogger(__name__)

_JSONPATH_RE = re.compile(r"^\$\.([A-Za-z0-9_\.]+)$")


def _resolve_jsonpath(data: Any, path: str) -> Any:
    """解析极简 JSONPath：仅支持 $.a.b.c。"""
    match = _JSONPATH_RE.match(path.strip())
    if not match:
        raise OcrError(f"Unsupported OCR response JSONPath: {path}")
    cur = data
    for part in match.group(1).split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise OcrError(f"OCR response missing path {path}")
        cur = cur[part]
    return cur


class CustomHttpOcrProvider:
    """按配置模板调用任意 OCR HTTP API。"""

    name = "custom"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def recognize(
        self,
        image_bytes: bytes,
        *,
        lang: str | None = None,
    ) -> OcrRecognizeResult:
        base = (self._settings.ocr_custom_base_url or "").rstrip("/")
        path = self._settings.ocr_custom_path or "/ocr"
        if not base:
            raise OcrError("RAG_OCR_CUSTOM_BASE_URL is required for custom OCR")

        template = (self._settings.ocr_custom_request_template or "multipart").lower()
        headers: dict[str, str] = {}
        if self._settings.ocr_custom_headers_json:
            try:
                headers = json.loads(self._settings.ocr_custom_headers_json)
            except json.JSONDecodeError as e:
                raise OcrError("RAG_OCR_CUSTOM_HEADERS_JSON is invalid JSON") from e
        if self._settings.ocr_custom_api_key and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self._settings.ocr_custom_api_key}"

        url = f"{base}{path if path.startswith('/') else '/' + path}"
        assert_safe_ocr_url(url, allow_private=self._settings.ocr_allow_private_urls)
        timeout = self._settings.ocr_api_timeout_seconds

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                if template == "multipart":
                    files = {"file": ("image.png", image_bytes, "image/png")}
                    data = {"lang": lang or self._settings.ocr_lang}
                    response = await client.post(url, headers=headers, files=files, data=data)
                elif template == "base64_json":
                    payload = {
                        "image_base64": base64.b64encode(image_bytes).decode("ascii"),
                        "lang": lang or self._settings.ocr_lang,
                    }
                    response = await client.post(url, headers=headers, json=payload)
                else:
                    raise OcrError(
                        "RAG_OCR_CUSTOM_REQUEST_TEMPLATE must be multipart or base64_json"
                    )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as e:
            raise OcrError(f"Custom OCR request failed: {e}") from e

        jsonpath = self._settings.ocr_custom_response_jsonpath or "$.text"
        value = _resolve_jsonpath(body, jsonpath)
        text = str(value or "").strip()
        return OcrRecognizeResult(text=text, confidence=None, raw=body, empty=not bool(text))
