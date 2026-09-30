"""The demonstration workbook: a sales comparison grid built by us to stand in for a firm's own model.

Layout: one visible sheet `Sales Comparison` with a subject column and three comparable columns, and a hidden
sheet `Settings` holding named constants. Reported facts are input cells written from approved records.
Everything calculated is a formula. The building area used as the unit of comparison, the adjustments and the
reconciliation are appraiser input cells: when sources disagree on the area, the machine reports both and leaves
the choice empty, so no price per square foot exists until an appraiser picks the basis. An empty input is never
read as zero: every calculated line stays blank until what it depends on is filled.

Adjustment convention used here (one convention among several in practice): transactional adjustments are applied
in sequence, each to the price per square foot adjusted by the lines above it; expenditures after purchase are a
dollar amount per square foot; property adjustments are percentages that are added together and applied once.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import xlsxwriter

SHEET = "Sales Comparison"
COLS = {"subject": "C", "comp1": "D", "comp2": "E", "comp3": "F"}
FACT_ROWS = [  # (row, field, label, machine-written?)
    (5, "address", "Address", True),
    (6, "parcels", "Parcel(s)", True),
    (7, "instrument", "Recorded instrument", True),
    (8, "sale_date", "Sale date (county record)", True),
    (9, "sale_price", "Sale price ($)", True),
    (10, "gba_sf", "Building area for comparison (sq ft), appraiser's choice", False),
    (11, "gba_reported", "Building area as reported by each source", True),
    (12, "land_acres", "Land area (acres)", True),
    (13, "year_built", "Year built", True),
    (14, "zoning", "Zoning", True),
    (15, "grantor", "Grantor (seller)", True),
    (16, "grantee", "Grantee (buyer)", True),
    (17, "property_rights", "Property rights (reported)", True),
    (18, "sale_conditions", "Conditions of sale (reported)", True),
    (19, "marketing", "Marketing history (reported)", True),
    (20, "non_realty", "Non-realty items (reported)", True),
    (21, "sources", "Sources", True),
    (22, "verification", "Verification", True),
]
ROW = {field: row for row, field, _, _ in FACT_ROWS}
PRICE, GBA, LAND = ROW["sale_price"], ROW["gba_sf"], ROW["land_acres"]
CALC_HEAD, PPSF, LTB = 24, 25, 26
TRANS_HEAD = 27
TRANSACTIONAL = [  # (input row, label, kind); the adjusted line sits on the next row
    (28, "Property rights conveyed", "pct"),
    (30, "Financing terms", "pct"),
    (32, "Conditions of sale", "pct"),
    (34, "Expenditures after purchase ($/sq ft)", "usd"),
    (36, "Market conditions (time)", "pct"),
]
PROP_HEAD = 39
PROPERTY = [(40, "Location"), (41, "Size"), (42, "Age and condition"), (43, "Quality"),
            (44, "Land-to-building ratio"), (45, "Use and zoning"), (46, "Non-realty components")]
NET_PROP, ADJ_PSF, GROSS, NET = 47, 48, 49, 50
REC_HEAD, REC_PSF, VALUE, REC_NOTE = 52, 53, 54, 55


def input_cells(col: str) -> dict[str, str]:
    """field name -> cell, for the reported-fact rows the machine may write in one column."""
    return {field: f"{col}{row}" for row, field, _, machine in FACT_ROWS if machine}


def appraiser_cells(col: str) -> dict[str, str]:
    cells = {"gba_sf": f"{col}{GBA}"}
    cells.update({f"t{r}": f"{col}{r}" for r, _, _ in TRANSACTIONAL})
    cells.update({f"p{r}": f"{col}{r}" for r, _ in PROPERTY})
    return cells


def build(path: Path, vba_project: Path | None = None) -> Path:
    wb = xlsxwriter.Workbook(str(path), {"strings_to_numbers": False})
    if vba_project:
        wb.add_vba_project(str(vba_project))
    ws = wb.add_worksheet(SHEET)
    st = wb.add_worksheet("Settings")
    st.hide()

    base = {"font_name": "Arial", "font_size": 10, "valign": "top"}
    f = lambda **k: wb.add_format({**base, **k})
    title = f(bold=True, font_size=13)
    note = f(italic=True, font_color="#555555", text_wrap=True)
    head = f(bold=True, bottom=1, align="center", text_wrap=True)
    section = f(bold=True, top=1, font_color="#1F3A5F")
    label = f(text_wrap=True)
    fact_txt = f(bg_color="#EEF3F8", text_wrap=True, border=1, border_color="#C9D3DE")
    fact_num = f(bg_color="#EEF3F8", num_format="#,##0", border=1, border_color="#C9D3DE")
    fact_dec = f(bg_color="#EEF3F8", num_format="0.00", border=1, border_color="#C9D3DE")
    fact_int = f(bg_color="#EEF3F8", num_format="0", border=1, border_color="#C9D3DE")
    fact_date = f(bg_color="#EEF3F8", num_format="mm/dd/yyyy", border=1, border_color="#C9D3DE")
    appr_num = f(bg_color="#FFF6DD", num_format="#,##0", border=1, border_color="#E2D3A8")
    appr_pct = f(bg_color="#FFF6DD", num_format="0.0%", border=1, border_color="#E2D3A8")
    appr_usd = f(bg_color="#FFF6DD", num_format="$#,##0.00", border=1, border_color="#E2D3A8")
    appr_txt = f(bg_color="#FFF6DD", text_wrap=True, border=1, border_color="#E2D3A8")
    grey = f(bg_color="#F4F4F4", border=1, border_color="#DDDDDD")
    calc_usd = f(num_format="$#,##0.00")
    calc_dec = f(num_format="0.00")
    calc_pct = f(num_format="0.0%")
    calc_val = f(num_format="$#,##0", bold=True)

    ws.set_column("A:A", 2)
    ws.set_column("B:B", 40)
    ws.set_column("C:F", 26)
    ws.freeze_panes(4, 2)

    ws.write("B2", "Sales comparison grid (demonstration template)", title)
    ws.write("B3", "Blue cells: reported facts, entered from approved records. Yellow cells: appraiser input, including the "
                   "building area used for comparison. A blank input is not zero: calculated lines stay blank until their inputs are filled.", note)
    ws.write("B4", "", head)
    for col, text in [("C", "Subject"), ("D", "Comparable 1"), ("E", "Comparable 2"), ("F", "Comparable 3")]:
        ws.write(f"{col}4", text, head)

    fmt_for = {"sale_date": fact_date, "sale_price": fact_num, "land_acres": fact_dec, "year_built": fact_int, "gba_sf": appr_num}
    subject_na = {"sale_date", "sale_price", "instrument", "sale_conditions", "grantor", "grantee", "marketing", "non_realty", "property_rights"}
    for row, field, text, _ in FACT_ROWS:
        ws.write(f"B{row}", text, label)
        for col in "CDEF":
            if col == "C" and field in subject_na:
                ws.write_blank(f"{col}{row}", None, grey)
                continue
            ws.write_blank(f"{col}{row}", None, fmt_for.get(field, fact_txt))

    P, G, L = PRICE, GBA, LAND
    ws.write(f"B{CALC_HEAD}", "Calculated from reported facts and the chosen area", section)
    ws.write(f"B{PPSF}", "Sale price per sq ft of the chosen building area", label)
    ws.write(f"B{LTB}", "Land-to-building ratio", label)
    for col in "DEF":
        ws.write_formula(f"{col}{PPSF}", f'=IF(OR({col}{P}="",{col}{G}=""),"",{col}{P}/{col}{G})', calc_usd, "")
        ws.write_formula(f"{col}{LTB}", f'=IF(OR({col}{L}="",{col}{G}=""),"",{col}{L}*SqFtPerAcre/{col}{G})', calc_dec, "")
    ws.write_formula(f"C{LTB}", f'=IF(OR(C{L}="",C{G}=""),"",C{L}*SqFtPerAcre/C{G})', calc_dec, "")

    ws.write(f"B{TRANS_HEAD}", "Transactional adjustments (appraiser input, applied in sequence)", section)
    prev = PPSF
    for r, text, kind in TRANSACTIONAL:
        ws.write(f"B{r}", text + (" (%)" if kind == "pct" else ""), label)
        ws.write(f"B{r + 1}", "Adjusted price per sq ft", f(italic=True))
        for col in "DEF":
            ws.write_blank(f"{col}{r}", None, appr_pct if kind == "pct" else appr_usd)
            op = f"{col}{prev}*(1+{col}{r})" if kind == "pct" else f"{col}{prev}+{col}{r}"
            ws.write_formula(f"{col}{r + 1}", f'=IF(OR({col}{prev}="",{col}{r}=""),"",{op})', calc_usd, "")
        prev = r + 1
    last = prev

    ws.write(f"B{PROP_HEAD}", "Property adjustments (appraiser input, added together)", section)
    first_p, last_p = PROPERTY[0][0], PROPERTY[-1][0]
    for r, text in PROPERTY:
        ws.write(f"B{r}", text + " (%)", label)
        for col in "DEF":
            ws.write_blank(f"{col}{r}", None, appr_pct)
    ws.write(f"B{NET_PROP}", "Net property adjustment", label)
    ws.write(f"B{ADJ_PSF}", "Adjusted price per sq ft", f(bold=True))
    ws.write(f"B{GROSS}", "Gross adjustment (% of unadjusted price)", label)
    ws.write(f"B{NET}", "Net adjustment (% of unadjusted price)", label)
    t = [r for r, _, _ in TRANSACTIONAL]
    for col in "DEF":
        ws.write_formula(f"{col}{NET_PROP}", f'=IF(COUNTBLANK({col}{first_p}:{col}{last_p})>0,"",SUM({col}{first_p}:{col}{last_p}))', calc_pct, "")
        ws.write_formula(f"{col}{ADJ_PSF}", f'=IF(OR({col}{last}="",{col}{NET_PROP}=""),"",{col}{last}*(1+{col}{NET_PROP}))',
                         f(num_format="$#,##0.00", bold=True), "")
        ws.write_formula(
            f"{col}{GROSS}",
            f'=IF({col}{ADJ_PSF}="","",(ABS({col}{t[0]})*{col}{PPSF}+ABS({col}{t[1]})*{col}{t[0] + 1}+ABS({col}{t[2]})*{col}{t[1] + 1}'
            f'+ABS({col}{t[3]})+ABS({col}{t[4]})*{col}{t[3] + 1}+SUMPRODUCT(ABS({col}{first_p}:{col}{last_p}))*{col}{last})/{col}{PPSF})',
            calc_pct, "")
        ws.write_formula(f"{col}{NET}", f'=IF({col}{ADJ_PSF}="","",{col}{ADJ_PSF}/{col}{PPSF}-1)', calc_pct, "")

    ws.write(f"B{REC_HEAD}", "Reconciliation (appraiser)", section)
    ws.write(f"B{REC_PSF}", "Reconciled price per sq ft", label)
    ws.write_blank(f"C{REC_PSF}", None, appr_usd)
    ws.write(f"B{VALUE}", "Indicated value, rounded", f(bold=True))
    ws.write_formula(f"C{VALUE}", f'=IF(OR(C{REC_PSF}="",C{G}=""),"",ROUND(C{REC_PSF}*C{G},ValueRounding))', calc_val, "")
    ws.write(f"B{REC_NOTE}", "Reconciliation comment", label)
    ws.write_blank(f"C{REC_NOTE}", None, appr_txt)

    ws.conditional_format(f"D{GROSS}:F{GROSS}", {"type": "formula", "criteria": f"=AND(ISNUMBER(D{GROSS}),D{GROSS}>GrossAdjustmentFlag)",
                                                 "format": wb.add_format({"bg_color": "#F8D7D3", "font_color": "#8A1C12"})})
    for col in "DEF":
        for r, _, kind in TRANSACTIONAL:
            if kind == "pct":
                ws.data_validation(f"{col}{r}", {"validate": "decimal", "criteria": "between", "minimum": -0.5, "maximum": 0.5,
                                                 "input_title": "Appraiser input", "input_message": "Percentage, for example 5% or -3%"})
        ws.data_validation(f"{col}{first_p}:{col}{last_p}", {"validate": "decimal", "criteria": "between", "minimum": -0.5, "maximum": 0.5})
        ws.data_validation(f"{col}{ROW['sale_date']}", {"validate": "date", "criteria": ">=", "value": date(2000, 1, 1)})
        ws.data_validation(f"{col}{G}", {"validate": "decimal", "criteria": ">", "value": 0,
                                         "input_title": "Appraiser input", "input_message": "Pick the building area basis from the reported areas above"})

    st.write("A1", "Setting")
    st.write("B1", "Value")
    st.write("A2", "Square feet per acre")
    st.write_number("B2", 43560)
    st.write("A3", "Rounding of indicated value (digits, -3 = nearest $1,000)")
    st.write_number("B3", -3)
    st.write("A4", "Flag gross adjustment above")
    st.write_number("B4", 0.25)
    st.write("A6", "Reported-fact cells filled, comparable 1")
    st.write_formula("B6", f"=COUNTA('{SHEET}'!D{FACT_ROWS[0][0]}:D{FACT_ROWS[-1][0]})", None, "")
    wb.define_name("SqFtPerAcre", "=Settings!$B$2")
    wb.define_name("ValueRounding", "=Settings!$B$3")
    wb.define_name("GrossAdjustmentFlag", "=Settings!$B$4")
    wb.set_properties({"title": "Sales comparison grid (demonstration template)", "author": "comp-to-excel",
                       "created": datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)})
    wb.set_calc_mode("auto")
    wb.close()
    return path
