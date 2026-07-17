"""OCR 引擎门面与 Provider 工厂。"""
from __future__ import annotations

import logging
from functools import lru_cache

from app.core.ocr.bailian_provider import BailianOcrProvider
from app.core.ocr.custom_http_provider import CustomHttpOcrProvider
from app.core.ocr.errors import OcrError
from app.core.ocr.none_provider import NoneOcrProvider
from app.core.ocr.openai_compatible_provider import OpenAICompatibleOcrProvider
from app.core.ocr.paddle_provider import PaddleOcrProvider
from app.core.ocr.protocol import OcrProvider, OcrRecognizeResult, OcrRegionDraft
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def normalize_ocr_provider(value: str) -> str:
    v = (value or "none").strip().lower()
    if v in {"none", "off", "disabled"}:
        return "none"
    if v in {"local", "paddle", "bge"}:
        return "local"
    if v in {"api", "remote"}:
        return "api"
    raise OcrError(f"Unsupported RAG_OCR_PROVIDER: {value}")


def normalize_ocr_api_driver(value: str) -> str:
    v = (value or "openai_compatible").strip().lower()
    if v in {"bailian", "dashscope"}:
        return "bailian"
    if v in {"openai", "openai_compatible", "compatible"}:
        return "openai_compatible"
    if v in {"custom", "http", "custom_http"}:
        return "custom"
    raise OcrError(f"Unsupported RAG_OCR_API_DRIVER: {value}")


def build_ocr_provider(settings: Settings | None = None) -> OcrProvider:
    s = settings or get_settings()
    mode = normalize_ocr_provider(s.ocr_provider)
    if mode == "none":
        return NoneOcrProvider()
    if mode == "local":
        return PaddleOcrProvider(lang=s.ocr_lang)
    driver = normalize_ocr_api_driver(s.ocr_api_driver)
    if driver == "bailian":
        return BailianOcrProvider(s)
    if driver == "custom":
        return CustomHttpOcrProvider(s)
    return OpenAICompatibleOcrProvider(s)


def _safe_error_message(exc: Exception, *, limit: int = 200) -> str:
    text = " ".join(str(exc).split())
    lowered = text.lower()
    for marker in ("api_key", "authorization", "bearer ", "sk-", "password"):
        if marker in lowered:
            return "OCR provider error (details redacted)"
    return text[:limit]


class OcrEngine:
    """统一 OCR 调用入口，支持 fail-open。"""

    def __init__(self, provider: OcrProvider | None = None, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._provider = provider or build_ocr_provider(self._settings)

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "name", "unknown")

    @property
    def enabled(self) -> bool:
        return normalize_ocr_provider(self._settings.ocr_provider) != "none"

    async def recognize(
        self,
        image_bytes: bytes,
        *,
        lang: str | None = None,
        fail_open: bool | None = None,
    ) -> OcrRecognizeResult:
        open_on_fail = (
            self._settings.ocr_fail_open if fail_open is None else fail_open
        )
        try:
            result = await self._provider.recognize(
                image_bytes, lang=lang or self._settings.ocr_lang
            )
            if not result.text.strip():
                return OcrRecognizeResult(
                    text="",
                    confidence=result.confidence,
                    raw=result.raw,
                    empty=True,
                )
            return result
        except Exception as e:  # noqa: BLE001
            if open_on_fail:
                logger.warning("OCR failed (fail-open): %s", e)
                return OcrRecognizeResult(
                    text="",
                    confidence=None,
                    raw={"error": _safe_error_message(e)},
                    empty=True,
                )
            raise

    async def recognize_region(
        self,
        *,
        page_no: int | None,
        bbox: list[float],
        crop_png: bytes,
        lang: str | None = None,
    ) -> OcrRegionDraft:
        try:
            result = await self.recognize(crop_png, lang=lang, fail_open=False)
            status = "empty" if result.empty or not result.text.strip() else "pending"
            return OcrRegionDraft(
                page_no=page_no,
                bbox=bbox,
                crop_png=crop_png,
                ocr_text=result.text,
                status=status,
                provider=self.provider_name,
                confidence=result.confidence,
            )
        except Exception as e:  # noqa: BLE001
            if self._settings.ocr_fail_open:
                logger.warning("OCR region failed (fail-open): %s", e)
                return OcrRegionDraft(
                    page_no=page_no,
                    bbox=bbox,
                    crop_png=crop_png,
                    ocr_text="",
                    status="failed",
                    provider=self.provider_name,
                    confidence=None,
                    error_message=_safe_error_message(e),
                )
            raise


@lru_cache
def get_ocr_engine() -> OcrEngine:
    return OcrEngine()


def reset_ocr_engine_cache() -> None:
    get_ocr_engine.cache_clear()
