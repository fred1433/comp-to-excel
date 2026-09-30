"""Acceptance contract for a written workbook.

Two independent checks:
  * part_diff: the package, part by part. Every part is byte-identical to the template except the target
    worksheet; inside it, only the `<c>` elements of the approved cells differ, and the XML around the cell data
    is byte-identical.
  * manifest: what the workbook computes with, read back from the file: every formula on every sheet (hidden sheets
    included, shared and array formulas expanded), defined names, conditional-format rules, data-validation rules,
    calculation settings, every constant outside the input cells, cell styles, sheet structure and the macro
    project if there is one. Its hash must be identical before and after; only the approved input cells may change.
A formula edited by hand, a defined name repointed or a protected constant changed each change the hash.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import openpyxl
from openpyxl.worksheet.formula import ArrayFormula


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def canonical_sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


_CELL = re.compile(rb'<c r="([A-Z]+\d+)"[^>]*?(?:/>|>.*?</c>)', re.S)


def _cells(xml: bytes) -> dict[str, bytes]:
    return {m.group(1).decode(): m.group(0) for m in _CELL.finditer(xml)}


_ROW = re.compile(rb"<row [^>]*>")


def _row_tags(xml: bytes) -> list[bytes]:
    return _ROW.findall(xml)


def _outside_sheetdata(xml: bytes) -> bytes:
    return re.sub(rb"<sheetData>.*</sheetData>", b"<sheetData/>", xml, flags=re.S)


def part_diff(template: Path, patched: Path, sheet_part: str, approved_cells: set[str]) -> dict:
    with zipfile.ZipFile(template) as a, zipfile.ZipFile(patched) as b:
        names_a, names_b = [i.filename for i in a.infolist()], [i.filename for i in b.infolist()]
        parts = []
        for n in sorted(set(names_a) | set(names_b)):
            ha = hashlib.sha256(a.read(n)).hexdigest() if n in names_a else None
            hb = hashlib.sha256(b.read(n)).hexdigest() if n in names_b else None
            parts.append({"part": n, "before": ha, "after": hb, "identical": ha == hb})
        xa, xb = a.read(sheet_part), b.read(sheet_part)
    ca, cb = _cells(xa), _cells(xb)
    changed = sorted((set(ca) | set(cb)) - {k for k in set(ca) & set(cb) if ca[k] == cb[k]}, key=lambda r: (int(re.sub(r"\D", "", r)), r))
    problems = []
    for p in parts:
        if not p["identical"] and p["part"] != sheet_part:
            problems.append(f"part {p['part']} changed")
    if names_a != names_b:
        problems.append("part order or part list changed")
    if _outside_sheetdata(xa) != _outside_sheetdata(xb):
        problems.append(f"{sheet_part} changed outside its cell data")
    if _row_tags(xa) != _row_tags(xb):
        problems.append(f"{sheet_part}: row attributes changed (height, hidden, outline or style)")
    for r in changed:
        if r not in approved_cells:
            problems.append(f"cell {r} changed but is not an approved input")
    return {"parts": parts, "changed_cells": changed, "problems": problems, "ok": not problems}


def _color(c):
    return None if c is None else (c.rgb if isinstance(c.rgb, str) else f"theme{c.theme}")


def _style(cell) -> list:
    f, fl, b, al, pr = cell.font, cell.fill, cell.border, cell.alignment, cell.protection
    return [cell.number_format, bool(f.b), bool(f.i), f.sz and float(f.sz), f.name, _color(f.color),
            fl.fill_type, _color(fl.fgColor) if fl.fill_type else None,
            [getattr(b, s).style for s in ("left", "right", "top", "bottom")],
            bool(al.wrap_text), al.horizontal, al.vertical, bool(pr.locked), bool(pr.hidden)]


def manifest(path: Path, input_cells: dict[str, set[str]]) -> dict:
    wb = openpyxl.load_workbook(path, data_only=False, keep_vba=True)
    sheets, inputs = {}, {}
    for ws in wb.worksheets:
        guarded = input_cells.get(ws.title, set())
        formulas, constants, styles = {}, {}, {}
        for row in ws.iter_rows():
            for c in row:
                ref = c.coordinate
                v = c.value
                if isinstance(v, ArrayFormula):
                    formulas[ref] = f"{{array {v.ref}}}{v.text}"
                elif isinstance(v, str) and v.startswith("="):
                    formulas[ref] = v
                elif ref in guarded:
                    inputs[f"{ws.title}!{ref}"] = None if v is None else typed(v)
                elif v is not None:
                    constants[ref] = [type(v).__name__, str(v)]
                if c.has_style:
                    styles[ref] = _style(c)
        cf = []
        for rng, rules in ws.conditional_formatting._cf_rules.items():
            for r in rules:
                dxf = r.dxf
                cf.append([str(rng.sqref), r.type, r.operator, list(r.formula or []), r.stopIfTrue,
                           _color(dxf.font.color) if dxf and dxf.font else None,
                           _color(dxf.fill.bgColor) if dxf and dxf.fill else None])
        dv = [[str(d.sqref), d.type, d.operator, d.formula1, d.formula2, bool(d.allow_blank)] for d in ws.data_validations.dataValidation]
        sheets[ws.title] = {
            "state": ws.sheet_state, "formulas": formulas, "constants": constants, "styles": styles,
            "conditional_formats": sorted(cf), "data_validations": sorted(dv),
            "merged": sorted(str(m) for m in ws.merged_cells.ranges), "freeze": ws.freeze_panes,
            "names": {n: d.attr_text for n, d in ws.defined_names.items()},
            "rows": {str(r): [bool(d.hidden), d.height, d.outlineLevel, bool(d.customHeight)]
                     for r, d in ws.row_dimensions.items() if d.hidden or d.height is not None or d.outlineLevel},
            "columns": {k: [bool(d.hidden), d.width, d.outlineLevel] for k, d in ws.column_dimensions.items()
                        if d.hidden or d.customWidth or d.outlineLevel},
        }
    names = {n: {"refers_to": d.attr_text, "scope": d.localSheetId, "hidden": bool(d.hidden)} for n, d in wb.defined_names.items()}
    calc = wb.calculation
    calc_settings = {"calcMode": calc.calcMode or "auto", "fullPrecision": calc.fullPrecision in (None, True),
                     "iterate": bool(calc.iterate), "iterateCount": calc.iterateCount, "iterateDelta": calc.iterateDelta}
    with zipfile.ZipFile(path) as z:
        vba = hashlib.sha256(z.read("xl/vbaProject.bin")).hexdigest() if "xl/vbaProject.bin" in z.namelist() else None
    protected = {"sheet_order": [ws.title for ws in wb.worksheets], "sheets": sheets, "defined_names": names,
                 "calculation": calc_settings, "macro_project_sha256": vba}
    formula_view = {"formulas": {t: s["formulas"] for t, s in sheets.items()}, "defined_names": names}
    return {
        "protected_sha256": canonical_sha(protected),
        "formulas_and_names_sha256": canonical_sha(formula_view),
        "counts": {"formulas": sum(len(s["formulas"]) for s in sheets.values()), "defined_names": len(names),
                   "conditional_formats": sum(len(s["conditional_formats"]) for s in sheets.values()),
                   "data_validations": sum(len(s["data_validations"]) for s in sheets.values()),
                   "constants": sum(len(s["constants"]) for s in sheets.values()),
                   "hidden_sheets": sum(1 for s in sheets.values() if s["state"] != "visible")},
        "inputs": inputs,
        "protected": protected,
    }


def typed(v) -> list:
    """An input value with its cell type, so a number written as text is not the same input."""
    from datetime import datetime
    if isinstance(v, bool):
        return ["bool", str(v)]
    if isinstance(v, (int, float)):
        return ["number", repr(float(v))]
    if isinstance(v, datetime):
        return ["date", v.isoformat()]
    return ["text", str(v)]


def expected(value) -> list | None:
    """What an input cell should read back as, for a value the writer wrote."""
    from datetime import date, datetime
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    return typed(value)


def _walk_diff(a, b, path=""):
    if type(a) != type(b):
        return [f"{path}: {a!r} -> {b!r}"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                out.append(f"{path}/{k}: added {b[k]!r}")
            elif k not in b:
                out.append(f"{path}/{k}: removed {a[k]!r}")
            else:
                out += _walk_diff(a[k], b[k], f"{path}/{k}")
        return out
    return [] if a == b else [f"{path}: {a!r} -> {b!r}"]


def check(template: Path, candidate: Path, input_cells: dict[str, set[str]], expected_inputs: dict[str, list | None]) -> dict:
    before, after = manifest(template, input_cells), manifest(candidate, input_cells)
    problems = _walk_diff(before["protected"], after["protected"])
    for ref, want in expected_inputs.items():
        got = after["inputs"].get(ref)
        if got != want:
            problems.append(f"input {ref}: expected {want!r}, found {got!r}")
    for ref, got in after["inputs"].items():
        if ref not in expected_inputs and got is not None:
            problems.append(f"input {ref} filled but not approved")
    return {"before": {k: before[k] for k in ("protected_sha256", "formulas_and_names_sha256", "counts")},
            "after": {k: after[k] for k in ("protected_sha256", "formulas_and_names_sha256", "counts")},
            "problems": problems, "ok": not problems}
