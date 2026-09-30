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
from comptrace.workbook import SHEET, build, input_cells

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
        writer.patch(TEMPLATE, tmp / "refused.xlsx", SHEET, {"D20": 60.0}, allowed | {"D20"})
        out.append({"test": "write into a formula cell (D20, price per sq ft)", "expected": "refused", "result": "accepted", "passed": False})
    except writer.WriteRefused as e:
        out.append({"test": "write into a formula cell (D20, price per sq ft)", "expected": "refused", "result": "refused", "detail": str(e), "passed": True})
    sheet = writer.sheet_part(TEMPLATE, SHEET)
    cases = [
        ("formula edited by hand (D20 multiplied by 1.1)", sheet,
         lambda x: x.replace(b'IF(OR(D9="",D10=""),"",D9/D10)</f>', b'IF(OR(D9="",D10=""),"",D9/D10*1.1)</f>')),
        ("defined name repointed (SqFtPerAcre to Settings!B4), no formula text changed", "xl/workbook.xml",
         lambda x: x.replace(b"Settings!$B$2", b"Settings!$B$4")),
        ("protected constant changed (Settings!B2, 43,560 to 43,000), no formula text changed", writer.sheet_part(TEMPLATE, "Settings"),
         lambda x: x.replace(b"<v>43560</v>", b"<v>43000</v>")),
    ]
    for name, part, fn in cases:
        bad = rewrite_part(delivered, tmp / "tampered.xlsx", part, fn)
        if acceptance.sha256_file(bad) == acceptance.sha256_file(delivered):
            out.append({"test": name, "expected": "detected", "result": "tampering did not apply", "passed": False})
            continue
        r = acceptance.check(TEMPLATE, bad, {SHEET: allowed}, expected_inputs)
        out.append({"test": name, "expected": "detected", "result": "detected" if not r["ok"] else "not detected",
                    "detail": r["problems"][:3], "hash_after": r["after"]["protected_sha256"], "passed": not r["ok"]})
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
    expected_inputs = {f"{SHEET}!{cell}": (None if entry[field]["value"] is None else
                                           (f"{entry[field]['value']} 00:00:00" if field == "sale_date" else str(entry[field]["value"])))
                       for field, cell in input_cells("D").items()}
    tamper = tamper_tests(TEMPLATE, DELIVERED, allowed, expected_inputs, tmp)

    grid = gridmath.column(entry["sale_price"]["value"], entry["gba_sf"]["value"], entry["land_acres"]["value"])
    excel = None
    test_inputs = {"D23": 0.0, "D25": 0.0, "D27": 0.05, "D29": 2.5, "D31": 0.08, "D35": 0.05, "D36": -0.05, "D37": 0.1,
                   "D38": 0.0, "D39": -0.03, "D40": 0.0, "D41": 0.0, "C10": 6500.0, "C48": 58.5}
    writer.patch(DELIVERED, TEST_COPY, SHEET, test_inputs, set(test_inputs))
    grid_test = gridmath.column(entry["sale_price"]["value"], entry["gba_sf"]["value"], entry["land_acres"]["value"],
                                [("pct", 0.0), ("pct", 0.0), ("pct", 0.05), ("usd", 2.5), ("pct", 0.08)],
                                [0.05, -0.05, 0.1, 0.0, -0.03, 0.0, 0.0])
    refs = [f"{SHEET}!{c}" for c in ("D9", "D10", "D11", "D20", "D21", "D24", "D32", "D42", "D43", "D44", "D45", "C49")] + ["Settings!B6"]
    if use_excel and excel_mac.available():
        excel = {"delivered": excel_mac.observe(DELIVERED, refs), "test_copy": excel_mac.observe(TEST_COPY, refs)}
        d, t = excel["delivered"]["cells"], excel["test_copy"]["cells"]
        cents = lambda x: round(x * 100) if isinstance(x, float) else x
        excel["comparison"] = [
            {"cell": "D20", "what": "price per sq ft", "excel": d[f"{SHEET}!D20"]["value"], "python": grid["price_per_sf"],
             "agree_to_the_cent": cents(d[f"{SHEET}!D20"]["value"]) == cents(grid["price_per_sf"])},
            {"cell": "D21", "what": "land-to-building ratio", "excel": d[f"{SHEET}!D21"]["value"], "python": grid["land_to_building"],
             "agree_to_the_cent": cents(d[f"{SHEET}!D21"]["value"]) == cents(grid["land_to_building"])},
            {"cell": "D43", "what": "adjusted price per sq ft with no adjustments entered", "excel": d[f"{SHEET}!D43"]["value"], "python": "",
             "agree_to_the_cent": d[f"{SHEET}!D43"]["value"] == ""},
            {"cell": "C49", "what": "indicated value with no reconciliation entered", "excel": d[f"{SHEET}!C49"]["value"], "python": "",
             "agree_to_the_cent": d[f"{SHEET}!C49"]["value"] == ""},
            {"cell": "D43 (test copy)", "what": "adjusted price per sq ft, test inputs", "excel": t[f"{SHEET}!D43"]["value"], "python": grid_test["adjusted_per_sf"],
             "agree_to_the_cent": cents(t[f"{SHEET}!D43"]["value"]) == cents(grid_test["adjusted_per_sf"])},
            {"cell": "D44 (test copy)", "what": "gross adjustment, test inputs", "excel": t[f"{SHEET}!D44"]["value"], "python": grid_test["gross_pct"],
             "agree_to_the_cent": round(t[f"{SHEET}!D44"]["value"], 6) == round(grid_test["gross_pct"], 6)},
            {"cell": "C49 (test copy)", "what": "indicated value, test inputs", "excel": t[f"{SHEET}!C49"]["value"], "python": round(58.5 * 6500, -3),
             "agree_to_the_cent": t[f"{SHEET}!C49"]["value"] == float(round(58.5 * 6500, -3))},
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
