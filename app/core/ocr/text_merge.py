"""Deterministically combine document text layers with reviewable OCR regions."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

from app.core.ingest.extractors.ocr_merge import format_ocr_span

_PAGE_MARKER = re.compile(r"(?:^|\n)--- Page (\d+) ---\n")


def _region_fields(region: Any) -> tuple[str, Any, dict[str, Any], list[float] | None]:
    if isinstance(region, dict):
        text = str(region.get("text") or "").strip()
        page_no = region.get("page_no")
        metadata = region.get("metadata") or {}
        bbox = region.get("bbox")
    else:
        text = str(getattr(region, "ocr_text", "") or "").strip()
        page_no = getattr(region, "page_no", None)
        metadata = getattr(region, "metadata", {}) or {}
        bbox = getattr(region, "bbox", None)
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 2:
        bbox_list = [float(bbox[0]), float(bbox[1])]
    else:
        bbox_list = None
    return text, page_no, metadata, bbox_list


def merge_ocr_text(base_text: str, regions: Iterable[Any]) -> str:
    """Return page-aware text rebuilt from an immutable text-layer baseline.

    OCR spans use the shared `[OCR image N]` / `[OCR image N p{page}]` prefix.
    Within a page, regions are ordered by bbox y0 then sequence when available.
    """
    page_bodies: dict[int, str] = {}
    page_order: list[int] = []
    matches = list(_PAGE_MARKER.finditer(base_text or ""))
    if matches:
        for index, match in enumerate(matches):
            page_no = int(match.group(1))
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(base_text)
            page_bodies[page_no] = base_text[start:end].strip()
            page_order.append(page_no)

    ocr_by_page: dict[int, list[tuple[float, int, str]]] = defaultdict(list)
    unpaged: list[tuple[float, int, str]] = []
    for fallback_sequence, region in enumerate(regions):
        text, page_no, metadata, bbox = _region_fields(region)
        if not text:
            continue
        sequence = int(metadata.get("sequence", fallback_sequence))
        image_no = int(metadata.get("image_no", sequence + 1))
        y0 = float(bbox[1]) if bbox is not None else float(sequence)
        if isinstance(page_no, int) and page_no > 0:
            span = format_ocr_span(image_no, text, page_no=page_no)
            ocr_by_page[page_no].append((y0, sequence, span))
            if page_no not in page_bodies:
                page_bodies[page_no] = ""
                page_order.append(page_no)
        else:
            span = format_ocr_span(image_no, text)
            unpaged.append((y0, sequence, span))

    if not matches and not page_bodies:
        pieces = [base_text.strip()] if (base_text or "").strip() else []
        pieces.extend(span for _, _, span in sorted(unpaged) if span)
        return "\n\n".join(pieces).strip()

    rendered: list[str] = []
    for page_no in sorted(set(page_order)):
        page_text = page_bodies.get(page_no, "").strip()
        ocr_spans = [span for _, _, span in sorted(ocr_by_page.get(page_no, [])) if span]
        # 复核路径无法还原文本块 bbox：按 y0 把 OCR 插到页首/页中近似位置较难，
        # 仍采用「页内文本 + 按 y0 排序的 OCR」；初次抽取侧做真正的邻近交织。
        pieces = [page_text, *ocr_spans]
        body = "\n\n".join(piece for piece in pieces if piece)
        if body:
            rendered.append(f"--- Page {page_no} ---\n{body}")
    rendered.extend(span for _, _, span in sorted(unpaged) if span)
    return "\n\n".join(rendered).strip()
