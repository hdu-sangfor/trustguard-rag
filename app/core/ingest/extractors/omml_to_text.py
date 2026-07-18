"""简易 OMML → 线性可读文本。"""
from __future__ import annotations

from xml.etree import ElementTree as ET

_NS = {
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
}


def omml_element_to_text(elem: ET.Element) -> str:
    """将单个 m:oMath / 子节点转为线性文本。"""
    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

    if tag == "t":
        return elem.text or ""

    if tag == "sSup":  # superscript: base^sup
        base = _child_text(elem, "e")
        sup = _child_text(elem, "sup")
        return f"{base}^{{{sup}}}" if sup else base

    if tag == "sSub":  # subscript
        base = _child_text(elem, "e")
        sub = _child_text(elem, "sub")
        return f"{base}_{{{sub}}}" if sub else base

    if tag == "sSubSup":
        base = _child_text(elem, "e")
        sub = _child_text(elem, "sub")
        sup = _child_text(elem, "sup")
        out = base
        if sub:
            out += f"_{{{sub}}}"
        if sup:
            out += f"^{{{sup}}}"
        return out

    if tag == "f":  # fraction
        num = _child_text(elem, "num")
        den = _child_text(elem, "den")
        return f"({num})/({den})"

    if tag == "rad":  # radical
        deg = _child_text(elem, "deg")
        e = _child_text(elem, "e")
        if deg:
            return f"root[{deg}]({e})"
        return f"sqrt({e})"

    if tag in {"d", "nary", "func", "acc", "bar", "box", "borderBox", "groupChr", "limLow", "limUpp"}:
        return "".join(omml_element_to_text(c) for c in list(elem))

    if tag == "r":
        return "".join(omml_element_to_text(c) for c in list(elem))

    if tag in {"oMath", "oMathPara", "e", "num", "den", "sup", "sub", "deg"}:
        return "".join(omml_element_to_text(c) for c in list(elem))

    # 未知节点：递归子节点
    parts = [omml_element_to_text(c) for c in list(elem)]
    joined = "".join(parts).strip()
    return joined


def _child_text(parent: ET.Element, local: str) -> str:
    for child in parent:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == local:
            return omml_element_to_text(child).strip()
    return ""


def omml_xml_to_text(xml_bytes_or_elem: bytes | ET.Element) -> str:
    """从 OMML 片段得到可读文本；失败返回空串。"""
    try:
        if isinstance(xml_bytes_or_elem, bytes):
            root = ET.fromstring(xml_bytes_or_elem)
        else:
            root = xml_bytes_or_elem
        text = omml_element_to_text(root).strip()
        return text
    except Exception:  # noqa: BLE001
        return ""
