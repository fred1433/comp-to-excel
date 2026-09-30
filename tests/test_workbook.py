"""Surgical writes and the acceptance contract, on real .xlsx files."""
import zipfile
from datetime import date

import pytest

from comptrace import acceptance, gridmath, writer
from comptrace.workbook import GBA, PPSF, PRICE, SHEET, build, input_cells

ALLOWED = set(input_cells("D").values())


@pytest.fixture()
def template(tmp_path):
    return build(tmp_path / "template.xlsx")


def entered(template, tmp_path):
    m = input_cells("D")
    return writer.patch(template, tmp_path / "out.xlsx", SHEET,
                        {m["address"]: "3540 St. Lawrence Ave", m["sale_date"]: date(2025, 10, 10), m["sale_price"]: 390000.0,
                         m["gba_reported"]: "County 7,354; listing 6,000"}, ALLOWED)


EXPECTED_WRITES = {"D5": "3540 St. Lawrence Ave", "D8": date(2025, 10, 10), "D9": 390000.0, "D11": "County 7,354; listing 6,000"}


def test_template_is_deterministic(tmp_path):
    a, b = build(tmp_path / "a.xlsx"), build(tmp_path / "b.xlsx")
    assert acceptance.sha256_file(a) == acceptance.sha256_file(b)


def test_only_the_worksheet_part_and_the_written_cells_change(template, tmp_path):
    out = entered(template, tmp_path)
    d = acceptance.part_diff(template, out, writer.sheet_part(template, SHEET), ALLOWED)
    assert d["ok"] and d["changed_cells"] == ["D5", "D8", "D9", "D11"]
    assert [p["part"] for p in d["parts"] if not p["identical"]] == ["xl/worksheets/sheet1.xml"]


def test_formula_cells_are_refused(template, tmp_path):
    with pytest.raises(writer.WriteRefused, match="formula"):
        writer.patch(template, tmp_path / "x.xlsx", SHEET, {f"D{PPSF}": 1.0}, ALLOWED | {f"D{PPSF}"})


def test_building_area_is_left_to_the_appraiser(template, tmp_path):
    assert f"D{GBA}" not in ALLOWED
    with pytest.raises(writer.WriteRefused, match="input map"):
        writer.patch(template, tmp_path / "x.xlsx", SHEET, {f"D{GBA}": 7354.0}, ALLOWED)


def test_unmapped_cells_are_refused(template, tmp_path):
    with pytest.raises(writer.WriteRefused, match="input map"):
        writer.patch(template, tmp_path / "x.xlsx", SHEET, {"E9": 1.0}, ALLOWED)


@pytest.mark.parametrize("part,old,new,where", [
    ("xl/worksheets/sheet1.xml", b'D9/D10)</f>', b'D9/D10*1.1)</f>', f"formulas/D{PPSF}"),
    ("xl/worksheets/sheet1.xml", f'<row r="{PPSF}"'.encode(), f'<row r="{PPSF}" hidden="1"'.encode(), f"rows/{PPSF}"),
    ("xl/worksheets/sheet1.xml", b'<row r="9"', b'<row r="9" ht="0.1" customHeight="1"', "rows/9"),
    ("xl/worksheets/sheet1.xml", b'<c r="D9" s="8"><v>390000.0</v></c>', b'<c r="D9" s="8" t="inlineStr"><is><t>390000.0</t></is></c>', "D9: expected ['number'"),
    ("xl/workbook.xml", b"Settings!$B$2", b"Settings!$B$4", "defined_names/SqFtPerAcre"),
    ("xl/worksheets/sheet2.xml", b"<v>43560</v>", b"<v>43000</v>", "constants/B2"),
    ("xl/worksheets/sheet1.xml", b'<c r="D9"', b'<c r="D9"', None),
])
def test_tampering_is_detected(template, tmp_path, part, old, new, where):
    out = entered(template, tmp_path)
    bad = tmp_path / "bad.xlsx"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(bad, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == part:
                assert old in data
                data = data.replace(old, new, 1)
            zout.writestr(info, data, compress_type=info.compress_type)
    expected = {f"{SHEET}!{c}": None for c in ALLOWED}
    expected.update({f"{SHEET}!{c}": acceptance.expected(v) for c, v in EXPECTED_WRITES.items()})
    r = acceptance.check(template, bad, {SHEET: ALLOWED}, expected)
    if where is None:
        assert r["ok"]
    else:
        assert not r["ok"] and any(where in p for p in r["problems"])


def test_conditional_format_and_validation_changes_are_detected(template, tmp_path):
    out = entered(template, tmp_path)
    for old, new in [(b"D49&gt;GrossAdjustmentFlag", b"D49&gt;0.5"), (b"<formula1>-0.5</formula1>", b"<formula1>-0.9</formula1>")]:
        bad = tmp_path / "bad.xlsx"
        with zipfile.ZipFile(out) as zin, zipfile.ZipFile(bad, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == "xl/worksheets/sheet1.xml":
                    assert old in data
                    data = data.replace(old, new, 1)
                zout.writestr(info, data, compress_type=info.compress_type)
        before, after = acceptance.manifest(template, {SHEET: ALLOWED}), acceptance.manifest(bad, {SHEET: ALLOWED})
        assert before["protected_sha256"] != after["protected_sha256"]


def test_macro_project_survives_byte_for_byte(root, tmp_path):
    vba = root / "tests/fixtures/vbaProject.bin"
    t = build(tmp_path / "template.xlsm", vba_project=vba)
    out = writer.patch(t, tmp_path / "out.xlsm", SHEET, {f"D{PRICE}": 390000.0}, ALLOWED)
    d = acceptance.part_diff(t, out, writer.sheet_part(t, SHEET), ALLOWED)
    assert next(p for p in d["parts"] if p["part"] == "xl/vbaProject.bin")["identical"]
    m = acceptance.manifest(out, {SHEET: ALLOWED})
    assert m["protected"]["macro_project_sha256"] == acceptance.sha256_file(vba)


def test_blank_adjustment_is_never_zero():
    g = gridmath.column(390000, 7354, 0.57, [("pct", 0.0), ("pct", None), ("pct", 0.0), ("usd", 0.0), ("pct", 0.0)], [0.0] * 7)
    assert g["price_per_sf"] and g["adjusted_per_sf"] is None and g["steps"][1] is None
