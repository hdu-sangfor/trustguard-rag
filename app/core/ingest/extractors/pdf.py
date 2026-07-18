"""基于 PyMuPDF 的 PDF 文本抽取 + 图片区域 OCR（邻近插入 + 统一前缀）。"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import fitz

from app.core.ingest.errors import (
    CORRUPT_FILE,
    EMPTY_CONTENT,
    OCR_UNAVAILABLE,
    PDF_ENCRYPTED,
    PDF_NO_TEXT_LAYER,
    PDF_TOO_LARGE,
    IngestError,
)
from app.core.ingest.extractors._async_utils import run_sync
from app.core.ingest.extractors.ocr_merge import format_ocr_span
from app.core.ingest.models import ExtractedDocument
from app.core.ocr import get_ocr_engine
from app.core.ocr.errors import OcrError
from app.core.ocr.protocol import OcrRegionDraft
from app.settings import get_settings

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"


def _safe_ocr_error(message: str, *, limit: int = 200) -> str:
    """对外暴露的 OCR 错误摘要：截断并去掉疑似密钥片段。"""
    text = " ".join((message or "").split())
    lowered = text.lower()
    for marker in ("api_key", "authorization", "bearer ", "sk-", "password"):
        if marker in lowered:
            return "OCR provider error (details redacted)"
    return text[:limit]


def _page_image_rects(page: fitz.Page) -> list[fitz.Rect]:
    """收集页面上的图片矩形（页面坐标）。"""
    rects: list[fitz.Rect] = []
    for info in page.get_image_info(xrefs=True):
        bbox = info.get("bbox")
        if not bbox:
            continue
        rect = fitz.Rect(bbox)
        if rect.is_empty or rect.is_infinite:
            continue
        rects.append(rect)
    # 去重近似重叠框
    unique: list[fitz.Rect] = []
    for rect in rects:
        if any(abs(rect.x0 - u.x0) < 1 and abs(rect.y0 - u.y0) < 1 for u in unique):
            continue
        unique.append(rect)
    return unique


def _page_text_blocks(page: fitz.Page) -> list[tuple[float, float, str]]:
    """文本块：(y0, x0, text)，供与 OCR 邻近排序。"""
    blocks: list[tuple[float, float, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        lines: list[str] = []
        for line in block.get("lines", []):
            spans = [s.get("text", "") for s in line.get("spans", [])]
            line_text = "".join(spans).strip()
            if line_text:
                lines.append(line_text)
        text = "\n".join(lines).strip()
        if not text:
            continue
        bbox = block.get("bbox") or (0, 0, 0, 0)
        blocks.append((float(bbox[1]), float(bbox[0]), text))
    return blocks


class PdfExtractor:
    def __init__(self, ocr_engine=None) -> None:
        self._ocr = ocr_engine

    def extract(
        self,
        data: bytes,
        *,
        original_filename: str = "document.pdf",
    ) -> ExtractedDocument:
        return run_sync(self.extract_async(data, original_filename=original_filename))

    async def extract_async(
        self,
        data: bytes,
        *,
        original_filename: str = "document.pdf",
    ) -> ExtractedDocument:
        """校验 PDF，抽取文本层，并对图片区域做裁剪 OCR（邻近插入）。"""
        settings = get_settings()
        if len(data) > settings.ingest_max_pdf_bytes:
            raise IngestError(PDF_TOO_LARGE, "PDF exceeds max byte size")
        if not data.startswith(PDF_MAGIC):
            raise IngestError(CORRUPT_FILE, "Not a valid PDF file")

        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as e:
            raise IngestError(CORRUPT_FILE, f"Cannot open PDF: {e}") from e

        ocr_engine = self._ocr or get_ocr_engine()
        region_drafts: list[OcrRegionDraft] = []

        try:
            if doc.needs_pass:
                raise IngestError(PDF_ENCRYPTED, "Password-protected PDF not supported")
            page_count = doc.page_count
            if page_count > settings.ingest_max_pdf_pages:
                raise IngestError(PDF_TOO_LARGE, "PDF exceeds max page count")

            pages_with_text: list[int] = []
            pages_with_ocr: list[int] = []
            base_parts: list[str] = []
            page_bodies: list[str] = []
            min_side = settings.ocr_min_image_side_px
            dpi = settings.ocr_render_dpi
            scale = dpi / 72.0
            max_per_page = settings.ocr_max_regions_per_page
            max_doc_regions = settings.ocr_max_regions_per_document
            max_pixels = settings.ocr_max_crop_pixels
            max_crop_bytes = settings.ocr_max_crop_bytes
            image_no = 0

            for i in range(page_count):
                page_no = i + 1
                page = doc.load_page(i)
                text_blocks = _page_text_blocks(page)
                plain_text = "\n".join(t for _, _, t in text_blocks).strip()
                if plain_text:
                    base_parts.append(f"--- Page {page_no} ---\n{plain_text}")

                # events: (y0, x0, kind, payload) kind=text|ocr
                events: list[tuple[float, float, str, Any]] = [
                    (y0, x0, "text", text) for y0, x0, text in text_blocks
                ]
                page_region_count = 0

                if ocr_engine.enabled:
                    for rect in _page_image_rects(page):
                        if len(region_drafts) >= max_doc_regions:
                            logger.warning(
                                "OCR region cap reached for document (%s)", max_doc_regions
                            )
                            break
                        if page_region_count >= max_per_page:
                            break
                        clip = rect & page.rect
                        if clip.is_empty:
                            continue
                        rendered_width = max(1, int(clip.width * scale + 0.5))
                        rendered_height = max(1, int(clip.height * scale + 0.5))
                        if rendered_width < min_side or rendered_height < min_side:
                            continue
                        if rendered_width * rendered_height > max_pixels:
                            logger.warning(
                                "skip OCR crop exceeding max pixels page=%s size=%sx%s",
                                page_no,
                                rendered_width,
                                rendered_height,
                            )
                            continue
                        try:
                            pix = page.get_pixmap(
                                matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False
                            )
                            crop_png = pix.tobytes("png")
                            if len(crop_png) > max_crop_bytes:
                                logger.warning(
                                    "skip OCR crop exceeding max bytes page=%s size=%s",
                                    page_no,
                                    len(crop_png),
                                )
                                continue
                        except Exception as e:  # noqa: BLE001
                            logger.warning("failed to render PDF image crop: %s", e)
                            continue

                        image_no += 1
                        bbox = [float(clip.x0), float(clip.y0), float(clip.x1), float(clip.y1)]
                        try:
                            draft = await ocr_engine.recognize_region(
                                page_no=page_no,
                                bbox=bbox,
                                crop_png=crop_png,
                            )
                        except OcrError as e:
                            if settings.ocr_fail_open:
                                draft = OcrRegionDraft(
                                    page_no=page_no,
                                    bbox=bbox,
                                    crop_png=crop_png,
                                    ocr_text="",
                                    status="failed",
                                    provider=ocr_engine.provider_name,
                                    error_message=_safe_ocr_error(str(e)),
                                    metadata={
                                        "image_no": image_no,
                                        "source": "pdf",
                                        "sequence": len(region_drafts),
                                    },
                                )
                            else:
                                raise IngestError(OCR_UNAVAILABLE, str(e)) from e
                        draft.metadata = {
                            **(draft.metadata or {}),
                            "image_no": image_no,
                            "source": "pdf",
                            "sequence": len(region_drafts),
                        }
                        region_drafts.append(draft)
                        page_region_count += 1
                        span = format_ocr_span(image_no, draft.ocr_text, page_no=page_no)
                        if span:
                            events.append((float(clip.y0), float(clip.x0), "ocr", span))
                            pages_with_ocr.append(page_no)

                events.sort(key=lambda e: (e[0], e[1], 0 if e[2] == "text" else 1))
                page_chunks = [payload for _, _, _, payload in events if payload]
                page_body = "\n\n".join(page_chunks).strip()
                if page_body:
                    pages_with_text.append(page_no)
                    page_bodies.append(f"--- Page {page_no} ---\n{page_body}")

            base_text = "\n\n".join(base_parts).strip()
            full_text = "\n\n".join(page_bodies).strip()
            if not full_text:
                if region_drafts:
                    # OCR 尝试过但全文仍空：返回空文本 + drafts，由 pipeline 落库后报 EMPTY
                    pass
                elif not ocr_engine.enabled:
                    raise IngestError(PDF_NO_TEXT_LAYER, "PDF has no extractable text layer")
                else:
                    raise IngestError(EMPTY_CONTENT, "PDF has no text layer and OCR found nothing")

            if not full_text and not region_drafts:
                raise IngestError(EMPTY_CONTENT, "Extracted text is empty")

            content_hash = hashlib.sha256(data).hexdigest()
            meta: dict[str, Any] = {
                "page_count": page_count,
                "pages_with_text": pages_with_text,
                "pages_with_ocr": sorted(set(pages_with_ocr)),
                "original_filename": original_filename,
                "file_size": len(data),
                "ocr_region_drafts": region_drafts,
                "ocr_base_text": base_text,
                "ocr_provider": ocr_engine.provider_name if ocr_engine.enabled else "none",
            }
            return ExtractedDocument(
                text=full_text,
                content_hash=content_hash,
                source_uri=f"upload://{content_hash}",
                mime="application/pdf",
                raw_bytes=data,
                raw_filename="raw.pdf",
                metadata=meta,
            )
        finally:
            doc.close()
