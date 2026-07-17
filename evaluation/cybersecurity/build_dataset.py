#!/usr/bin/env python3
"""生成网络安全评测语料 PDF、证据清单和 JSONL 数据集。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = Path(__file__).with_name("source_data.json")
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "pdf" / "cybersecurity-eval-corpus"
DEFAULT_WORK = PROJECT_ROOT / "tmp" / "pdfs" / "cybersecurity-eval-corpus"
DEFAULT_DATASET_DIR = Path(__file__).with_name("datasets")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    parser.add_argument("--libreoffice", type=Path)
    parser.add_argument("--pdftotext", type=Path)
    parser.add_argument("--pdfinfo", type=Path)
    parser.add_argument(
        "--reuse-pdf",
        action="store_true",
        help="复用输出目录中已有 PDF，只重建页码清单和 JSONL 标注",
    )
    return parser.parse_args()


def find_executable(explicit: Path | None, names: list[str], known: list[Path]) -> Path:
    if explicit:
        candidate = explicit.expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"找不到指定程序: {candidate}")

    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved)
    for candidate in known:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"找不到所需程序，已尝试: {', '.join(names)}")


def validate_source(data: dict[str, Any]) -> None:
    documents = data.get("documents", [])
    questions = data.get("questions", [])
    if not documents or not questions:
        raise ValueError("source_data.json 必须包含 documents 和 questions")

    evidence_ids: list[str] = [item["evidence_id"] for item in data.get("external_evidence", [])]
    filenames: list[str] = []
    for document in documents:
        filenames.append(document["filename"])
        for section in document["sections"]:
            evidence_ids.append(section["evidence_id"])

    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("evidence_id 必须全局唯一")
    if len(filenames) != len(set(filenames)):
        raise ValueError("PDF 文件名必须唯一")

    valid_evidence = set(evidence_ids)
    query_ids: list[str] = []
    for question in questions:
        query_ids.append(question["query_id"])
        unknown = set(question["evidence_ids"]) - valid_evidence
        if unknown:
            raise ValueError(f"{question['query_id']} 引用了不存在的证据: {sorted(unknown)}")
        if question["answerable"] != bool(question["evidence_ids"]):
            raise ValueError(f"{question['query_id']} 的 answerable 与 evidence_ids 不一致")
        if question["split"] not in {"dev", "test"}:
            raise ValueError(f"{question['query_id']} 的 split 只能是 dev 或 test")
    if len(query_ids) != len(set(query_ids)):
        raise ValueError("query_id 必须唯一")


def render_fact_rows(facts: list[list[str]]) -> str:
    return "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
        for label, value in facts
    )


def render_list(items: list[str]) -> str:
    return "".join(f"<li>{html.escape(item)}</li>" for item in items)


def render_sources(sources: list[list[str]]) -> str:
    rows = []
    for index, (label, url) in enumerate(sources, start=1):
        escaped_url = html.escape(url, quote=True)
        rows.append(
            "<li>"
            f"<span class='source-label'>[{index}] {html.escape(label)}</span>"
            f"<a href='{escaped_url}'>{escaped_url}</a>"
            "</li>"
        )
    return "".join(rows)


def build_html(document: dict[str, Any], metadata: dict[str, Any]) -> str:
    sections = document["sections"]
    section_html = []
    for ordinal, section in enumerate(sections, start=1):
        detail_html = "".join(f"<p>{html.escape(p)}</p>" for p in section["details"])
        section_html.append(
            f"""
            <section class="evidence">
              <p class="hard-page-break">&nbsp;</p>
              <div class="section-head">
                <div class="eyebrow">TRUSTGUARD · CYBERSECURITY EVALUATION CORPUS</div>
                <div class="marker">证据ID: {html.escape(section['evidence_id'])}</div>
                <h2>{ordinal}. {html.escape(section['title'])}</h2>
              </div>
              <p class="lead">{html.escape(section['summary'])}</p>
              <h3>关键事实</h3>
              <table class="facts">{render_fact_rows(section['facts'])}</table>
              <h3>解释与边界</h3>
              {detail_html}
              <div class="defense-box">
                <h3>防御侧行动</h3>
                <ul>{render_list(section['defensive_actions'])}</ul>
              </div>
              <h3>官方来源</h3>
              <ol class="sources">{render_sources(section['sources'])}</ol>
            </section>
            """
        )

    dataset_name = metadata["name"]
    snapshot = metadata["snapshot_date"]
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(document['title'])}</title>
  <style>
    @page {{ size: A4; margin: 16mm 16mm 17mm; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; color: #142033; background: #ffffff;
      font-family: "Microsoft YaHei", "Noto Sans CJK SC", "SimSun", sans-serif;
      font-size: 9.5pt; line-height: 1.42;
    }}
    .cover {{ page-break-after: always; break-after: page; padding: 20mm 12mm 8mm; }}
    .evidence {{ margin: 0; padding: 0; }}
    .hard-page-break {{ page-break-before: always; break-before: page; height: 0; line-height: 0; margin: 0; padding: 0; }}
    .section-head {{ page-break-inside: avoid; break-inside: avoid; }}
    .cover-band {{ width: 27mm; height: 3mm; background: #0b6bcb; margin-bottom: 18mm; }}
    h1 {{ font-size: 27pt; line-height: 1.24; color: #102a43; margin: 0 0 8mm; }}
    .subtitle {{ font-size: 15pt; color: #486581; margin-bottom: 20mm; }}
    .meta-grid {{ display: table; width: 100%; border-collapse: separate; border-spacing: 4mm; margin-left: -4mm; }}
    .meta-card {{ display: table-cell; width: 33%; padding: 5mm; background: #edf6ff; border-left: 1.5mm solid #0b6bcb; }}
    .meta-label {{ color: #627d98; font-size: 8.5pt; }}
    .meta-value {{ display: block; margin-top: 1.5mm; color: #102a43; font-weight: 700; }}
    .scope {{ margin-top: 20mm; padding: 7mm; border: 0.4mm solid #bcccdc; background: #f8fafc; }}
    .scope strong {{ color: #0b6bcb; }}
    .eyebrow {{ color: #627d98; font-size: 7.8pt; letter-spacing: 0.8pt; margin: 0 0 3mm; }}
    .marker {{ display: inline-block; color: #075985; background: #e0f2fe; border: 0.3mm solid #7dd3fc;
      border-radius: 2mm; padding: 1.5mm 3mm; font-size: 8.5pt; font-weight: 700; margin-bottom: 4mm; }}
    h2 {{ color: #102a43; font-size: 16.5pt; line-height: 1.25; margin: 1mm 0 3mm; padding-bottom: 2mm; border-bottom: 0.7mm solid #0b6bcb; }}
    h3 {{ color: #184e77; font-size: 10.5pt; margin: 3.2mm 0 1.2mm; }}
    p {{ margin: 0 0 1.8mm; text-align: justify; }}
    .lead {{ color: #243b53; background: #f0f7ff; padding: 3mm 4mm; border-left: 1.2mm solid #0b6bcb; }}
    .facts {{ width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 8.8pt; }}
    .facts th, .facts td {{ border: 0.25mm solid #bcccdc; padding: 1.3mm 2mm; vertical-align: top; }}
    .facts th {{ width: 29%; color: #334e68; background: #f0f4f8; text-align: left; }}
    .defense-box {{ margin-top: 2.5mm; padding: 0.5mm 3.5mm 1.8mm; border: 0.35mm solid #9ad8b3; background: #effaf3; }}
    ul {{ margin: 0; padding-left: 6mm; }}
    li {{ margin: 0.7mm 0; }}
    .sources {{ margin: 0; padding-left: 6mm; font-size: 7.1pt; line-height: 1.25; }}
    .sources li {{ margin-bottom: 1mm; }}
    .sources a {{ display: block; color: #486581; text-decoration: none; overflow-wrap: anywhere; word-break: break-all; }}
    .source-label {{ display: block; color: #243b53; font-weight: 600; }}
    .safety {{ margin-top: 12mm; color: #486581; font-size: 9pt; }}
  </style>
</head>
<body>
  <section class="cover">
    <div class="cover-band"></div>
    <div class="eyebrow">{html.escape(dataset_name.upper())}</div>
    <h1>{html.escape(document['title'])}</h1>
    <div class="subtitle">{html.escape(document['subtitle'])}</div>
    <div class="meta-grid">
      <div class="meta-card"><span class="meta-label">语料版本</span><span class="meta-value">v{html.escape(metadata['version'])}</span></div>
      <div class="meta-card"><span class="meta-label">知识快照</span><span class="meta-value">{html.escape(snapshot)}</span></div>
      <div class="meta-card"><span class="meta-label">证据章节</span><span class="meta-value">{len(sections)} 个</span></div>
    </div>
    <div class="scope">
      <strong>用途：</strong>评测中文分块、BM25 与向量混合召回、Rerank、困难负样本和多跳检索。<br>
      <strong>证据：</strong>每个章节均有唯一证据 ID；页码在 PDF 生成后自动定位并写入数据集。<br>
      <strong>时效：</strong>涉及“最新”的事实仅对 {html.escape(snapshot)} 快照负责，后续应重新采集官方源。
    </div>
    <p class="safety">安全说明：漏洞章节只提供原理概括、影响、检测和缓解信息，不包含可直接武器化的利用代码或操作步骤。</p>
  </section>
  {''.join(section_html)}
</body>
</html>
"""


def run_checked(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        rendered = " ".join(command)
        raise RuntimeError(
            f"命令失败 ({result.returncode}): {rendered}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def convert_to_pdf(libreoffice: Path, html_path: Path, output_dir: Path) -> Path:
    expected = output_dir / f"{html_path.stem}.pdf"
    expected.unlink(missing_ok=True)
    result = run_checked(
        [str(libreoffice), "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(html_path)]
    )
    if not expected.is_file():
        raise RuntimeError(f"LibreOffice 未生成预期 PDF: {expected}\n{result.stdout}\n{result.stderr}")
    return expected


def pdf_page_count(pdfinfo: Path, pdf_path: Path) -> int:
    result = run_checked([str(pdfinfo), str(pdf_path)])
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, flags=re.MULTILINE)
    if not match:
        raise RuntimeError(f"无法从 pdfinfo 输出解析页数: {pdf_path}")
    return int(match.group(1))


def extract_page_text(pdftotext: Path, pdf_path: Path, page: int) -> str:
    result = run_checked(
        [str(pdftotext), "-f", str(page), "-l", str(page), "-layout", str(pdf_path), "-"]
    )
    return result.stdout.replace("\x0c", "")


def locate_evidence_pages(
    pdftotext: Path,
    pdf_path: Path,
    page_count: int,
    evidence_ids: list[str],
) -> tuple[dict[str, int], dict[int, str]]:
    texts = {page: extract_page_text(pdftotext, pdf_path, page) for page in range(1, page_count + 1)}
    mapping: dict[str, int] = {}
    for evidence_id in evidence_ids:
        matches = [page for page, text in texts.items() if evidence_id in text]
        if len(matches) != 1:
            raise RuntimeError(
                f"{pdf_path.name} 中证据 {evidence_id} 应恰好出现一次，实际页码: {matches}"
            )
        mapping[evidence_id] = matches[0]
    return mapping, texts


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for block in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file_obj:
        for row in rows:
            file_obj.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def public_test_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key
        not in {
            "answerable",
            "evidence_ids",
            "expected_answer",
            "must_include",
            "relevant_evidence",
            "relevant_documents",
            "relevant_pages",
        }
    }


def main() -> int:
    args = parse_args()
    source_path = args.source.resolve()
    output_dir = args.output_dir.resolve()
    work_dir = args.work_dir.resolve()
    dataset_dir = args.dataset_dir.resolve()

    data = json.loads(source_path.read_text(encoding="utf-8"))
    validate_source(data)

    libreoffice = find_executable(
        args.libreoffice,
        ["soffice", "libreoffice"],
        [Path(r"D:\software\LibreOffice\program\soffice.com"), Path(r"C:\Program Files\LibreOffice\program\soffice.com")],
    )
    pdftotext = find_executable(
        args.pdftotext,
        ["pdftotext"],
        [Path(r"D:\software\texlive\2024\bin\windows\pdftotext.exe")],
    )
    pdfinfo = find_executable(
        args.pdfinfo,
        ["pdfinfo"],
        [Path(r"D:\software\texlive\2024\bin\windows\pdfinfo.exe")],
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir.mkdir(parents=True, exist_ok=True)

    evidence_index: dict[str, dict[str, Any]] = {
        item["evidence_id"]: dict(item) for item in data.get("external_evidence", [])
    }
    manifest_documents: list[dict[str, Any]] = []
    for document in data["documents"]:
        stem = Path(document["filename"]).stem
        pdf_path = output_dir / document["filename"]
        if args.reuse_pdf:
            if not pdf_path.is_file():
                raise FileNotFoundError(f"找不到可复用的 PDF: {pdf_path}")
        else:
            html_path = work_dir / f"{stem}.html"
            html_path.write_text(build_html(document, data["dataset"]), encoding="utf-8")
            pdf_path = convert_to_pdf(libreoffice, html_path, output_dir)
        page_count = pdf_page_count(pdfinfo, pdf_path)
        evidence_ids = [section["evidence_id"] for section in document["sections"]]
        pages, page_texts = locate_evidence_pages(pdftotext, pdf_path, page_count, evidence_ids)

        document_sources: list[str] = []
        for section in document["sections"]:
            evidence_id = section["evidence_id"]
            source_urls = [source[1] for source in section["sources"]]
            document_sources.extend(source_urls)
            evidence_index[evidence_id] = {
                "evidence_id": evidence_id,
                "document_id": document["document_id"],
                "filename": document["filename"],
                "page": pages[evidence_id],
                "title": section["title"],
                "summary": section["summary"],
                "source_urls": source_urls,
            }

        empty_pages = [page for page, text in page_texts.items() if not text.strip()]
        if empty_pages:
            raise RuntimeError(f"{pdf_path.name} 存在空白页: {empty_pages}")

        manifest_documents.append(
            {
                "document_id": document["document_id"],
                "filename": document["filename"],
                "title": document["title"],
                "page_count": page_count,
                "sha256": sha256_file(pdf_path),
                "evidence_pages": pages,
                "source_urls": sorted(set(document_sources)),
            }
        )

    enriched_questions: list[dict[str, Any]] = []
    for question in data["questions"]:
        row = dict(question)
        relevant = [evidence_index[evidence_id] for evidence_id in question["evidence_ids"]]
        row["snapshot_date"] = data["dataset"]["snapshot_date"]
        row["relevant_evidence"] = relevant
        row["relevant_documents"] = sorted({item["filename"] for item in relevant})
        row["relevant_pages"] = sorted(
            {f"{item['filename']}#page={item['page']}" for item in relevant}
        )
        enriched_questions.append(row)

    dev_rows = [row for row in enriched_questions if row["split"] == "dev"]
    test_rows = [row for row in enriched_questions if row["split"] == "test"]
    write_jsonl(dataset_dir / "cybersecurity-dev.jsonl", dev_rows)
    write_jsonl(dataset_dir / "cybersecurity-test-gold.jsonl", test_rows)
    write_jsonl(dataset_dir / "cybersecurity-test-queries.jsonl", [public_test_row(row) for row in test_rows])

    manifest = {
        "dataset": data["dataset"],
        "generated_with": {
            "python": sys.version.split()[0],
            "libreoffice": str(libreoffice),
            "pdftotext": str(pdftotext),
            "pdfinfo": str(pdfinfo),
        },
        "documents": manifest_documents,
        "evidence": list(evidence_index.values()),
    }
    write_json(dataset_dir / "corpus-manifest.json", manifest)

    stats = {
        "documents": len(data["documents"]),
        "pages": sum(item["page_count"] for item in manifest_documents),
        "evidence_sections": len(evidence_index),
        "questions": len(enriched_questions),
        "dev_questions": len(dev_rows),
        "test_questions": len(test_rows),
        "answerable": Counter(str(row["answerable"]).lower() for row in enriched_questions),
        "categories": Counter(row["category"] for row in enriched_questions),
        "difficulty": Counter(row["difficulty"] for row in enriched_questions),
    }
    write_json(dataset_dir / "stats.json", stats)

    print(f"已生成 {stats['documents']} 份 PDF，共 {stats['pages']} 页")
    print(f"已生成 {stats['questions']} 条问题：dev={len(dev_rows)}，test={len(test_rows)}")
    print(f"PDF: {output_dir}")
    print(f"数据集: {dataset_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
