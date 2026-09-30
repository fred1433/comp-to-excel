"""The demonstration workbook: a sales comparison grid built by us to stand in for a firm's own model.

Layout: one visible sheet `Sales Comparison` with a subject column and three comparable columns, and a hidden
sheet `Settings` holding named constants. Reported facts are input cells; everything calculated is a formula.
Adjustments and reconciliation are appraiser input cells. An empty adjustment is never read as zero: every
adjusted line stays blank until all the adjustments above it are filled.

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
FACT_ROWS = [
    (5, "address", "Address"),
    (6, "parcels", "Parcel(s)"),
    (7, "instrument", "Recorded instrument"),
    (8, "sale_date", "Sale date"),
    (9, "sale_price", "Sale price ($)"),
    (10, "gba_sf", "Gross building area (sq ft, above grade)"),
    (11, "land_acres", "Land area (acres)"),
    (12, "year_built", "Year built"),
    (13, "zoning", "Zoning"),
    (14, "property_rights", "Property rights conveyed (reported)"),
    (15, "sale_conditions", "Conditions of sale (reported facts)"),
    (16, "sources", "Sources"),
    (17, "verification", "Verification"),
]
TRANSACTIONAL = [  # (input row, label, kind)
    (23, "Property rights conveyed", "pct"),
    (25, "Financing terms", "pct"),
    (27, "Conditions of sale", "pct"),
    (29, "Expenditures after purchase ($/sq ft)", "usd"),
    (31, "Market conditions (time)", "pct"),
]
PROPERTY = [(35, "Location"), (36, "Size"), (37, "Age and condition"), (38, "Quality"),
            (39, "Land-to-building ratio"), (40, "Use and zoning"), (41, "Non-realty components")]


def input_cells(col: str) -> dict[str, str]:
    """field name -> cell, for the reported-fact rows of one column."""
    return {field: f"{col}{row}" for row, field, _ in FACT_ROWS}


def adjustment_cells(col: str) -> dict[str, str]:
    cells = {f"t{r}": f"{col}{r}" for r, _, _ in TRANSACTIONAL}
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
    appr_pct = f(bg_color="#FFF6DD", num_format="0.0%", border=1, border_color="#E2D3A8")
    appr_usd = f(bg_color="#FFF6DD", num_format="$#,##0.00", border=1, border_color="#E2D3A8")
    appr_txt = f(bg_color="#FFF6DD", text_wrap=True, border=1, border_color="#E2D3A8")
    calc_usd = f(num_format="$#,##0.00")
    calc_dec = f(num_format="0.00")
    calc_pct = f(num_format="0.0%")
    calc_val = f(num_format="$#,##0", bold=True)

    ws.set_column("A:A", 2)
    ws.set_column("B:B", 38)
    ws.set_column("C:F", 26)
    ws.freeze_panes(4, 2)

    ws.write("B2", "Sales comparison grid (demonstration template)", title)
    ws.write("B3", "Blue cells: reported facts, entered from approved records. Yellow cells: appraiser input. "
                   "A blank adjustment is not zero: adjusted lines stay blank until the adjustments above them are entered.", note)
    ws.write("B4", "", head)
    for key, col, text in [("subject", "C", "Subject"), ("comp1", "D", "Comparable 1"), ("comp2", "E", "Comparable 2"), ("comp3", "F", "Comparable 3")]:
        ws.write(f"{col}4", text, head)

    fmt_for = {"sale_date": fact_date, "sale_price": fact_num, "gba_sf": fact_num, "land_acres": fact_dec, "year_built": fact_int}
    for row, field, text in FACT_ROWS:
        ws.write(f"B{row}", text, label)
        for col in "CDEF":
            if col == "C" and field in ("sale_date", "sale_price", "instrument", "sale_conditions", "property_rights"):
                ws.write_blank(f"{col}{row}", None, f(bg_color="#F4F4F4", border=1, border_color="#DDDDDD"))
                continue
            ws.write_blank(f"{col}{row}", None, fmt_for.get(field, fact_txt))

    ws.write("B19", "Calculated from reported facts", section)
    ws.write("B20", "Sale price per sq ft of gross building area", label)
    ws.write("B21", "Land-to-building ratio", label)
    for col in "DEF":
        ws.write_formula(f"{col}20", f'=IF(OR({col}9="",{col}10=""),"",{col}9/{col}10)', calc_usd, "")
        ws.write_formula(f"{col}21", f'=IF(OR({col}11="",{col}10=""),"",{col}11*SqFtPerAcre/{col}10)', calc_dec, "")
    ws.write_formula("C21", '=IF(OR(C11="",C10=""),"",C11*SqFtPerAcre/C10)', calc_dec, "")

    ws.write("B22", "Transactional adjustments (appraiser input, applied in sequence)", section)
    prev = "20"
    for r, text, kind in TRANSACTIONAL:
        ws.write(f"B{r}", text + (" (%)" if kind == "pct" else ""), label)
        ws.write(f"B{r + 1}", "Adjusted price per sq ft", f(italic=True))
        for col in "DEF":
            ws.write_blank(f"{col}{r}", None, appr_pct if kind == "pct" else appr_usd)
            op = f"{col}{prev}*(1+{col}{r})" if kind == "pct" else f"{col}{prev}+{col}{r}"
            ws.write_formula(f"{col}{r + 1}", f'=IF(OR({col}{prev}="",{col}{r}=""),"",{op})', calc_usd, "")
        prev = str(r + 1)

    ws.write("B34", "Property adjustments (appraiser input, added together)", section)
    for r, text in PROPERTY:
        ws.write(f"B{r}", text + " (%)", label)
        for col in "DEF":
            ws.write_blank(f"{col}{r}", None, appr_pct)
    ws.write("B42", "Net property adjustment", label)
    ws.write("B43", "Adjusted price per sq ft", f(bold=True))
    ws.write("B44", "Gross adjustment (% of unadjusted price)", label)
    ws.write("B45", "Net adjustment (% of unadjusted price)", label)
    for col in "DEF":
        ws.write_formula(f"{col}42", f'=IF(COUNTBLANK({col}35:{col}41)>0,"",SUM({col}35:{col}41))', calc_pct, "")
        ws.write_formula(f"{col}43", f'=IF(OR({col}32="",{col}42=""),"",{col}32*(1+{col}42))', f(num_format="$#,##0.00", bold=True), "")
        ws.write_formula(
            f"{col}44",
            f'=IF({col}43="","",(ABS({col}23)*{col}20+ABS({col}25)*{col}24+ABS({col}27)*{col}26+ABS({col}29)'
            f'+ABS({col}31)*{col}30+SUMPRODUCT(ABS({col}35:{col}41))*{col}32)/{col}20)', calc_pct)
        ws.write_formula(f"{col}45", f'=IF({col}43="","",{col}43/{col}20-1)', calc_pct, "")

    ws.write("B47", "Reconciliation (appraiser)", section)
    ws.write("B48", "Reconciled price per sq ft", label)
    ws.write_blank("C48", None, appr_usd)
    ws.write("B49", "Indicated value, rounded", f(bold=True))
    ws.write_formula("C49", '=IF(OR(C48="",C10=""),"",ROUND(C48*C10,ValueRounding))', calc_val, "")
    ws.write("B50", "Reconciliation comment", label)
    ws.write_blank("C50", None, appr_txt)

    ws.conditional_format("D44:F44", {"type": "formula", "criteria": "=AND(ISNUMBER(D44),D44>GrossAdjustmentFlag)",
                                      "format": wb.add_format({"bg_color": "#F8D7D3", "font_color": "#8A1C12"})})
    for col in "DEF":
        for r, _, kind in TRANSACTIONAL:
            if kind == "pct":
                ws.data_validation(f"{col}{r}", {"validate": "decimal", "criteria": "between", "minimum": -0.5, "maximum": 0.5,
                                                 "input_title": "Appraiser input", "input_message": "Percentage, for example 5% or -3%"})
        ws.data_validation(f"{col}35:{col}41", {"validate": "decimal", "criteria": "between", "minimum": -0.5, "maximum": 0.5})
        ws.data_validation(f"{col}8", {"validate": "date", "criteria": ">=", "value": date(2000, 1, 1)})

    st.write("A1", "Setting")
    st.write("B1", "Value")
    st.write("A2", "Square feet per acre")
    st.write_number("B2", 43560)
    st.write("A3", "Rounding of indicated value (digits, -3 = nearest $1,000)")
    st.write_number("B3", -3)
    st.write("A4", "Flag gross adjustment above")
    st.write_number("B4", 0.25)
    st.write("A6", "Reported-fact cells filled, comparable 1")
    st.write_formula("B6", f"=COUNTA('{SHEET}'!D5:D17)", None, 0)
    wb.define_name("SqFtPerAcre", "=Settings!$B$2")
    wb.define_name("ValueRounding", "=Settings!$B$3")
    wb.define_name("GrossAdjustmentFlag", "=Settings!$B$4")
    wb.set_properties({"title": "Sales comparison grid (demonstration template)", "author": "comp-to-excel",
                       "created": datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)})
    wb.set_calc_mode("auto")
    wb.close()
    return path
