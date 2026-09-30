"""Surgical writer: puts approved values into mapped input cells of the sheet XML, and touches nothing else.

The workbook package is rebuilt part by part in the original order with the original compression settings; only
the worksheet part changes, and inside it only the `<c>` elements of the mapped cells. Refusals:
  * a cell that is not in the approved input map;
  * a cell whose current element holds a formula;
  * a cell that is absent from the template (the template must declare every input cell).
Strings are written inline (`t="inlineStr"`) so the shared-strings part stays byte-identical.
"""
from __future__ import annotations

import re
import zipfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape


class WriteRefused(Exception):
    pass


def excel_serial(d: date) -> int:
    return (d - date(1899, 12, 30)).days


def _cell_pattern(ref: str) -> re.Pattern:
    return re.compile(rb'<c r="' + ref.encode() + rb'"(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</c>)', re.S)


def _render(ref: str, style: bytes, value) -> bytes:
    if isinstance(value, bool):
        raise WriteRefused(f"{ref}: booleans are not an input type here")
    if isinstance(value, date):
        return b'<c r="%s"%s><v>%d</v></c>' % (ref.encode(), style, excel_serial(value))
    if isinstance(value, (int, float)):
        return b'<c r="%s"%s><v>%s</v></c>' % (ref.encode(), style, repr(value).encode())
    text = escape(str(value))
    space = ' xml:space="preserve"' if text != text.strip() else ""
    return b'<c r="%s"%s t="inlineStr"><is><t%s>%s</t></is></c>' % (ref.encode(), style, space.encode(), text.encode())


def patch_sheet_xml(xml: bytes, writes: dict[str, object], allowed: set[str]) -> bytes:
    for ref, value in writes.items():
        if ref not in allowed:
            raise WriteRefused(f"{ref} is not in the approved input map")
        m = _cell_pattern(ref).search(xml)
        if not m:
            raise WriteRefused(f"{ref} is not declared in the template")
        if m.group("body") and b"<f" in m.group("body"):
            raise WriteRefused(f"{ref} holds a formula; formula cells are never written")
        style = re.search(rb'\ss="\d+"', m.group("attrs"))
        new = _render(ref, style.group(0) if style else b"", value)
        xml = xml[: m.start()] + new + xml[m.end():]
    return xml


def sheet_part(pkg: Path, sheet_name: str) -> str:
    with zipfile.ZipFile(pkg) as z:
        wbx = z.read("xl/workbook.xml").decode()
        rels = z.read("xl/_rels/workbook.xml.rels").decode()
    rid = re.search(r'<sheet name="%s" sheetId="\d+"(?: state="\w+")? r:id="(rId\d+)"' % re.escape(escape(sheet_name)), wbx).group(1)
    target = re.search(r'<Relationship Id="%s" Type="[^"]+" Target="([^"]+)"' % rid, rels).group(1)
    return "xl/" + target.lstrip("/").removeprefix("xl/")


def patch(template: Path, out: Path, sheet_name: str, writes: dict[str, object], allowed: set[str]) -> Path:
    part = sheet_part(template, sheet_name)
    with zipfile.ZipFile(template) as zin:
        new_sheet = patch_sheet_xml(zin.read(part), writes, allowed)
        tmp = Path(str(out) + ".partial")
        with zipfile.ZipFile(tmp, "w") as zout:
            for info in zin.infolist():
                data = new_sheet if info.filename == part else zin.read(info.filename)
                zout.writestr(info, data, compress_type=info.compress_type)
    tmp.replace(out)
    return out
