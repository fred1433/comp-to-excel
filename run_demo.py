"""Run the whole controlled entry once and record every outcome in run/.

    python run_demo.py            # replays the recorded model response; needs PostgreSQL 14+ (initdb, pg_ctl)
    python run_demo.py --no-excel # skip the Excel for Mac observation

Nothing here calls the model API. Excel for Mac is used when present, only to open, recalculate and read.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

from comptrace import acceptance, excel_mac, gridmath, writer
from comptrace.county import county_layer
from comptrace.pg import LocalPostgres
from comptrace.pipeline import DESTINATION, Run
from comptrace.workbook import (ADJ_PSF, GBA, GROSS, LAND, LTB, PPSF, PRICE, PROPERTY, REC_PSF, SHEET, TRANSACTIONAL,
                                VALUE, appraiser_cells, build, input_cells)

ROOT = Path(__file__).resolve().parent


def rewrite_part(src: Path, dst: Path, part: str, fn) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == part:
                data = fn(data)
            zout.writestr(info, data, compress_type=info.compress_type)
    return dst


def tamper_tests(TEMPLATE: Path, delivered: Path, allowed: set[str], expected_inputs: dict, tmp: Path) -> list[dict]:
    out = []
    try:
        writer.patch(TEMPLATE, tmp / "refused.xlsx", SHEET, {f"D{PPSF}": 60.0}, allowed | {f"D{PPSF}"})
        out.append({"test": f"write into a formula cell (D{PPSF}, price per sq ft)", "expected": "refused", "result": "accepted", "passed": False})
    except writer.WriteRefused as e:
        out.append({"test": f"write into a formula cell (D{PPSF}, price per sq ft)", "expected": "refused", "result": "refused", "detail": str(e), "passed": True})
    sheet = writer.sheet_part(TEMPLATE, SHEET)
    cases = [
        (f"formula edited by hand (D{PPSF} multiplied by 1.1)", sheet,
         lambda x: x.replace(b'IF(OR(D9="",D10=""),"",D9/D10)</f>', b'IF(OR(D9="",D10=""),"",D9/D10*1.1)</f>')),
        ("defined name repointed (SqFtPerAcre to Settings!B4), no formula text changed", "xl/workbook.xml",
         lambda x: x.replace(b"Settings!$B$2", b"Settings!$B$4")),
        (f"row {PPSF} (price per sq ft) hidden, no formula or value changed", sheet,
         lambda x: x.replace(f'<row r="{PPSF}"'.encode(), f'<row r="{PPSF}" hidden="1"'.encode(), 1)),
        (f"sale price D{PRICE} rewritten as text instead of a number", sheet,
         lambda x: x.replace(f'<c r="D{PRICE}" s="8"><v>390000.0</v></c>'.encode(), f'<c r="D{PRICE}" s="8" t="inlineStr"><is><t>390000.0</t></is></c>'.encode())),
        ("protected constant changed (Settings!B2, 43,560 to 43,000), no formula text changed", writer.sheet_part(TEMPLATE, "Settings"),
         lambda x: x.replace(b"<v>43560</v>", b"<v>43000</v>")),
    ]
    short = ["a formula edited by hand", "a defined name repointed", f"row {PPSF} hidden", "the sale price stored as text", "a protected constant changed"]
    for (name, part, fn), sh in zip(cases, short):
        bad = rewrite_part(delivered, tmp / "tampered.xlsx", part, fn)
        if acceptance.sha256_file(bad) == acceptance.sha256_file(delivered):
            out.append({"test": name, "expected": "detected", "result": "tampering did not apply", "passed": False})
            continue
        r = acceptance.check(TEMPLATE, bad, {SHEET: allowed}, expected_inputs)
        pd = acceptance.part_diff(TEMPLATE, bad, sheet, allowed)
        caught = not r["ok"] or not pd["ok"]
        out.append({"test": name, "short": sh, "expected": "detected", "result": "detected" if caught else "not detected",
                    "detail": (r["problems"] + pd["problems"])[:3], "manifest_detected": not r["ok"], "part_diff_detected": not pd["ok"],
                    "hash_after": r["after"]["protected_sha256"], "passed": caught})
    return out


def audit_tests(pg) -> list[dict]:
    out = []
    for role in ("comp_extractor", "comp_reviewer", "comp_writer"):
        for sql, what in [("update comp.audit_log set detail = '{}' where seq = 1", "UPDATE"),
                          ("delete from comp.audit_log where seq = 1", "DELETE"),
                          ("truncate comp.audit_log", "TRUNCATE")]:
            c = pg.connect("comp_app")
            c.execute(f"set role {role}")
            try:
                c.execute(sql)
                c.commit()
                out.append({"role": role, "statement": what, "result": "allowed", "passed": False})
            except psycopg.Error as e:
                c.rollback()
                out.append({"role": role, "statement": what, "result": "refused", "error": e.diag.message_primary, "passed": True})
            finally:
                c.close()
    return out


def main(use_excel: bool = True, run_dir: Path = ROOT / "run", wb_dir: Path = ROOT / "workbooks") -> dict:
    RUN, WB = Path(run_dir), Path(wb_dir)
    TEMPLATE = WB / "template.xlsx"
    DELIVERED = WB / "comparable-1-entered.xlsx"
    TEST_COPY = WB / "formula-check-test-inputs.xlsx"
    RUN.mkdir(exist_ok=True)
    WB.mkdir(exist_ok=True)
    tmp = RUN / "tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    build(TEMPLATE)
    for p in (DELIVERED, TEST_COPY):
        p.unlink(missing_ok=True)

    manifest = json.loads((ROOT / "sources/manifest.json").read_text())
    model = json.loads((ROOT / "fixtures/model_outputs/extraction_3540.json").read_text())
    county = json.loads((ROOT / "fixtures/county_facts_3540.json").read_text())
    layers = {L["doc"]: L for L in json.loads((ROOT / "fixtures/cited_lines.json").read_text())}
    layers["county"] = county_layer(county)
    sources = {d: {"url": m["url"], "retrieved_at": m["retrieved_at"]} for d, m in manifest["documents"].items()}

    with LocalPostgres() as pg:
        with pg.connect() as c:
            c.execute((ROOT / "comptrace/schema.sql").read_text())
        run = Run(pg, layers, sources, TEMPLATE, RUN)
        run.step("database", "started", server=f"PostgreSQL {pg.version()}", roles=["comp_extractor", "comp_reviewer", "comp_writer"])

        for doc in ("county", "minutes", "mls-cb", "mls-coalition"):
            run.ingest(doc)
        run.record_model_call(model["call"])
        facts = run.check_and_store(county["facts"] + model["extraction"]["facts"])

        ids = run.create_from_county(county, facts)
        run.duplicate_instrument_refused(ids["instrument"])
        run.match_listing("mls-cb", facts)
        run.ingest("mls-cb")  # the same file dropped into the folder a second time
        run.match_minutes(facts)
        run.match_listing("mls-coalition", facts)
        run.confirm_review("minutes", "demo operator")

        entry = run.assemble(facts)

        r = run.write(DELIVERED)
        run.step("write", r["outcome"], what="write before approval", reason=r.get("reason"), file_created=DELIVERED.exists())

        run.approve("demo operator (Frederic de Lavenne de Choulot), not an appraiser")
        run.change_after_approval_refused(DELIVERED)

        try:
            run.write(DELIVERED, crash_after_save=True)
        except SystemExit:
            run.step("write", "worker_stopped_after_save", note="simulated: the workbook was saved, the completion was never recorded",
                     file_exists=DELIVERED.exists(), file_sha256=acceptance.sha256_file(DELIVERED))
        r = run.write(DELIVERED)
        run.step("write", r["outcome"], op_id=r.get("op_id"), output_sha256=r.get("output_sha256"))
        verification = r["verification"]
        r2 = run.write(DELIVERED)
        run.step("write", r2["outcome"], what="the same approval run again", output_sha256=r2.get("output_sha256"))

        with pg.connect() as c:
            ops = c.execute("select status, count(*) from comp.write_operation group by status").fetchall()
        run.step("write", "operations", rows=[list(o) for o in ops])

        audit_refusals = audit_tests(pg)
        with pg.connect() as c:
            chain_ok = all(ok for _, ok in c.execute("select * from comp.verify_audit_chain()"))
            audit_rows = c.execute("select seq, to_char(at at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), actor, action, object, detail, prev_sha256, row_sha256 from comp.audit_log order by seq").fetchall()
            c.execute("alter table comp.audit_log disable trigger audit_no_update")
            c.execute("update comp.audit_log set detail = detail || '{\"edited\": true}' where seq = 3")
            c.execute("alter table comp.audit_log enable trigger audit_no_update")
            broken = [s for s, ok in c.execute("select * from comp.verify_audit_chain()") if not ok]
        run.step("audit", "append_only_for_tested_application_roles", refusals=audit_refusals, chain_verified=chain_ok,
                 owner_edit_detected_at_rows=broken, rows=len(audit_rows))
        steps = run.steps
        run.audit_conn.close()

    allowed = set(input_cells("D").values())
    expected_inputs = {f"{SHEET}!{cell}": acceptance.expected(date.fromisoformat(entry[field]["value"]) if field == "sale_date" else entry[field]["value"])
                       for field, cell in input_cells("D").items()}
    tamper = tamper_tests(TEMPLATE, DELIVERED, allowed, expected_inputs, tmp)

    price, land = entry["sale_price"]["value"], entry["land_acres"]["value"]
    grid = gridmath.column(price, None, land)  # no building area chosen: nothing per square foot exists
    excel = None
    A = appraiser_cells("D")
    tr = [A[f"t{r}"] for r, _, _ in TRANSACTIONAL]
    pr = [A[f"p{r}"] for r, _ in PROPERTY]
    test_basis = entry["gba_sf"]["choices"][0]["sf"]
    t_vals = [("pct", 0.0), ("pct", 0.0), ("pct", 0.05), ("usd", 2.5), ("pct", 0.08)]
    p_vals = [0.05, -0.05, 0.1, 0.0, -0.03, 0.0, 0.0]
    test_inputs = {A["gba_sf"]: test_basis, **{c: v for c, (_, v) in zip(tr, t_vals)}, **dict(zip(pr, p_vals)),
                   f"C{GBA}": 6500.0, f"C{REC_PSF}": 58.5}
    writer.patch(DELIVERED, TEST_COPY, SHEET, test_inputs, set(test_inputs))
    grid_test = gridmath.column(price, test_basis, land, t_vals, p_vals)
    cells = {"ppsf": f"D{PPSF}", "ltb": f"D{LTB}", "adj": f"D{ADJ_PSF}", "gross": f"D{GROSS}", "value": f"C{VALUE}"}
    refs = [f"{SHEET}!{c}" for c in (f"D{PRICE}", f"D{GBA}", f"D{LAND}", *cells.values())] + ["Settings!B6"]
    if use_excel and excel_mac.available():
        excel = {"delivered": excel_mac.observe(DELIVERED, refs), "test_copy": excel_mac.observe(TEST_COPY, refs)}
        d, t = excel["delivered"]["cells"], excel["test_copy"]["cells"]
        v = lambda book, key: book[f"{SHEET}!{cells[key]}"]["value"]
        cents = lambda x: round(x * 100) if isinstance(x, float) else x
        blank = lambda key, what: {"cell": cells[key], "what": what, "excel": v(d, key), "python": "", "agree_to_the_cent": v(d, key) == ""}
        excel["comparison"] = [
            blank("ppsf", "price per sq ft, no building area chosen"),
            blank("ltb", "land-to-building ratio, no building area chosen"),
            blank("adj", "adjusted price per sq ft, no adjustments entered"),
            blank("value", "indicated value, no reconciliation entered"),
            {"cell": cells["ppsf"] + " (test copy)", "what": f"price per sq ft, test basis {test_basis:,.0f} sq ft", "excel": v(t, "ppsf"),
             "python": grid_test["price_per_sf"], "agree_to_the_cent": cents(v(t, "ppsf")) == cents(grid_test["price_per_sf"])},
            {"cell": cells["adj"] + " (test copy)", "what": "adjusted price per sq ft, test inputs", "excel": v(t, "adj"),
             "python": grid_test["adjusted_per_sf"], "agree_to_the_cent": cents(v(t, "adj")) == cents(grid_test["adjusted_per_sf"])},
            {"cell": cells["gross"] + " (test copy)", "what": "gross adjustment, test inputs", "excel": v(t, "gross"),
             "python": grid_test["gross_pct"], "agree_to_the_cent": round(v(t, "gross"), 6) == round(grid_test["gross_pct"], 6)},
            {"cell": cells["value"] + " (test copy)", "what": "indicated value, test inputs", "excel": v(t, "value"),
             "python": round(58.5 * 6500, -3), "agree_to_the_cent": v(t, "value") == float(round(58.5 * 6500, -3))},
        ]
        excel["delivered_file_is_the_recalculated_file"] = excel["delivered"]["workbook_sha256"] == acceptance.sha256_file(DELIVERED)

    result = {
        "started_at": started,
        "files": {"template": {"path": "workbooks/template.xlsx", "sha256": acceptance.sha256_file(TEMPLATE)},
                  "delivered": {"path": "workbooks/comparable-1-entered.xlsx", "sha256": acceptance.sha256_file(DELIVERED)},
                  "test_copy": {"path": "workbooks/formula-check-test-inputs.xlsx", "sha256": acceptance.sha256_file(TEST_COPY),
                                "note": "test inputs typed by us to exercise the formulas; not an appraisal, not delivered"}},
        "destination": DESTINATION,
        "steps": steps,
        "facts": [{k: f.get(k) for k in ("id", "doc", "meaning_code", "meaning", "value", "quote", "span_ids", "status", "reason", "scope", "derived_from")} for f in facts],
        "acceptance": {"part_diff": verification["part_diff"], "manifest": verification["acceptance"]},
        "tamper_tests": tamper,
        "gridmath": {"delivered": grid, "test_copy": grid_test},
        "excel": excel,
        "attribution": county["attribution"],
    }
    (RUN / "run.json").write_text(json.dumps(result, indent=1, default=str))
    with open(RUN / "audit.jsonl", "w") as f:
        for row in audit_rows:
            f.write(json.dumps(dict(zip(("seq", "at", "actor", "action", "object", "detail", "prev_sha256", "row_sha256"), row)), default=str) + "\n")
    shutil.rmtree(tmp, ignore_errors=True)
    return result


if __name__ == "__main__":
    res = main(use_excel="--no-excel" not in sys.argv)
    for s in res["steps"]:
        print(f"{s['n']:>2} {s['step']:<12} {s['outcome']}" + (f"  ({s.get('doc')})" if s.get("doc") else ""))
    print("tamper tests:", [t["result"] for t in res["tamper_tests"]])
    if res["excel"]:
        print("excel:", res["excel"]["delivered"]["version"], [(c["cell"], c["agree_to_the_cent"]) for c in res["excel"]["comparison"]])
