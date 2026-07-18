"""从 Word chart XML 抽取可读标题/轴/系列文本。"""
from __future__ import annotations

from xml.etree import ElementTree as ET

_MAX_POINTS = 40


def chart_xml_to_text(xml_bytes: bytes, *, max_points: int = _MAX_POINTS) -> str:
    """解析 chart*.xml，返回 `[图表] ...` 文本块；失败返回空。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ""

    def local(tag: str) -> str:
        return tag.split("}")[-1] if "}" in tag else tag

    def findall_local(elem: ET.Element, name: str) -> list[ET.Element]:
        return [c for c in elem.iter() if local(c.tag) == name]

    def text_of(elem: ET.Element | None) -> str:
        if elem is None:
            return ""
        # c:v or a:t
        for node in elem.iter():
            if local(node.tag) in {"v", "t"} and (node.text or "").strip():
                return (node.text or "").strip()
        return (elem.text or "").strip()

    title = ""
    for t in findall_local(root, "chart"):
        for title_el in findall_local(t, "title"):
            title = text_of(title_el) or title
            break

    cats: list[str] = []
    for cat in findall_local(root, "cat"):
        for pt in findall_local(cat, "pt"):
            cats.append(text_of(pt))
        if cats:
            break

    series_bits: list[str] = []
    for ser in findall_local(root, "ser"):
        name = ""
        for tx in findall_local(ser, "tx"):
            name = text_of(tx)
            break
        vals: list[str] = []
        for val in findall_local(ser, "val"):
            for pt in findall_local(val, "pt"):
                vals.append(text_of(pt))
            break
        if not name and not vals:
            continue
        if len(vals) > max_points:
            vals = vals[:max_points] + ["..."]
        pair = []
        if cats and vals:
            for i, v in enumerate(vals):
                if v == "...":
                    pair.append("...")
                    break
                c = cats[i] if i < len(cats) else str(i)
                pair.append(f"{c}={v}")
            series_bits.append(f"{name or 'series'}: " + ", ".join(pair))
        else:
            series_bits.append(f"{name or 'series'}: " + ", ".join(vals))

    lines = ["[图表]"]
    if title:
        lines.append(f"标题: {title}")
    if cats and not series_bits:
        lines.append("分类: " + ", ".join(cats[:max_points]))
    lines.extend(series_bits)
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
