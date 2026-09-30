"""Build site/dist/ from the recorded run (run/run.json). No network, no API."""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from comptrace.extract import MeaningCode  # noqa: E402
from comptrace.verify import MEANING_WORDS  # noqa: E402
from comptrace.verify import Registry  # noqa: E402
from comptrace.workbook import FACT_ROWS, LTB, PPSF, input_cells  # noqa: E402
import zipfile  # noqa: E402
SRC, DIST = ROOT / "site/src", ROOT / "site/dist"

run = json.loads((ROOT / "run/run.json").read_text())
crops = json.loads((SRC / "crops.json").read_text())
manifest = json.loads((ROOT / "sources/manifest.json").read_text())
facts = {f["id"]: f for f in run["facts"]}
entry = next(s for s in run["steps"] if s["step"] == "assemble")["entry"]
notes = next(s for s in run["steps"] if s["step"] == "assemble")["notes"]

DOC = {
    "county": {"name": "Berks County assessment record", "kind": "County open-data record, retrieved 9/30/2026"},
    "minutes": {"name": "St. Lawrence Borough Council minutes, 9/11/2025", "kind": "Scanned PDF, read by OCR"},
    "mls-cb": {"name": "MLS PABK2052516, coldwellbankerhomes.com", "kind": "Listing page saved as PDF on 9/30/2026"},
    "mls-coalition": {"name": "MLS PABK2052516, coalitionpg.com", "kind": "Listing page saved as PDF on 9/30/2026"},
}
for d, m in manifest["documents"].items():
    DOC[d]["url"] = m["url"]
    DOC[d]["sha256"] = m.get("sha256")
GROUP = {"county": "county", "minutes": "minutes", "mls-cb": "mls", "mls-coalition": "mls"}  # two sites, one MLS listing
ARC = "https://services3.arcgis.com/dGYe1jDYrTw1wwpc/arcgis/rest/services"
COMMERCIAL_FIELDS = {"AREA", "YRBLT", "SALEPR1"}
REG = Registry(json.loads((ROOT / "fixtures/cited_lines.json").read_text()))


def source_url(f):
    if f["doc"] == "county":
        parid, field = f["span_ids"][0].split(":")[1:3]
        layer = "Berks_Assessment_CAMA_Commercial_File/FeatureServer/13" if field in COMMERCIAL_FIELDS else "Berks_Assessment_CAMA_Master_File/FeatureServer/14"
        return f"{ARC}/{layer}/query?where=PARID%3D%27{parid}%27&outFields=*&f=html"
    url = DOC[f["doc"]]["url"]
    if f["doc"] == "minutes" and f["span_ids"]:
        url += "#page=" + f["span_ids"][0].split(":p")[1].split(":")[0]
    return url


def page_context(f):
    """The cited lines with their own row and labels, as they sit on the page."""
    if f["doc"] == "county" or not all(s in REG.spans for s in f["span_ids"]):
        return None
    ctx = REG.context(f["span_ids"])
    ctx = ctx.replace("\u2014", "-")  # the listing prints a dash for "no change"
    return ctx if " ".join(ctx.split()) != " ".join(f["quote"].split()) else None

CROP_FOR = {("minutes", "accepted_offer_price"): "motion", ("minutes", "sale_condition"): "motion",
            ("minutes", "offer_acceptance_date"): "header"}


CITED = {sp["id"]: sp["text"] for d in json.loads((ROOT / "fixtures/cited_lines.json").read_text()) for pg in d["pages"] for sp in pg["spans"]}


def evidence(fid, role="source"):
    f = facts[fid]
    nxt = None
    if role == "conflict" and f["span_ids"] and ":l" in f["span_ids"][-1]:
        head, n = f["span_ids"][-1].rsplit(":l", 1)
        nxt = CITED.get(f"{head}:l{int(n) + 1}")
    page = None
    if f["span_ids"] and ":p" in f["span_ids"][0]:
        page = int(f["span_ids"][0].split(":p")[1].split(":")[0])
    crop = crops.get(CROP_FOR.get((f["doc"], f["meaning_code"])), None)
    return {"id": fid, "doc": f["doc"], "meaning": f["meaning"], "value": f["value"], "quote": f["quote"], "page": page,
            "spans": f["span_ids"], "status": f["status"], "reason": f["reason"], "role": role,
            "img": ("img/" + crop["file"]) if crop else None, "derived": f.get("derived_from"), "next": nxt,
            "url": source_url(f), "context": page_context(f), "group": GROUP[f["doc"]]}


FIELDS = [
    ("sale_price", "Sale price", "D9", lambda v: f"${v:,.0f}"),
    ("sale_date", "Sale date", "D8", lambda v: f"{int(v[5:7])}/{int(v[8:10])}/{v[:4]}"),
    ("instrument", "Recorded instrument", "D7", str),
    ("gba_sf", "Building area for comparison", "D10", lambda v: "Appraiser to choose"),
    ("gba_reported", "Building area as reported", "D11", str),
    ("grantor", "Seller (council minutes)", "D15", str),
    ("grantee", "Current owner (county record)", "D16", str),
    ("parcels", "Parcels conveyed", "D6", lambda v: v.replace("; ", "\n")),
    ("address", "Address", "D5", str),
    ("land_acres", "Land area", "D12", lambda v: f"{v:.2f} acres, two parcels"),
    ("year_built", "Year built", "D13", lambda v: f"{v:.0f}"),
    ("zoning", "Zoning", "D14", str),
    ("property_rights", "Property rights", "D17", str),
    ("sale_conditions", "Conditions of sale", "D18", str),
    ("marketing", "Marketing history", "D19", str),
    ("non_realty", "Non-realty items", "D20", str),
    ("verification", "Verification", "D22", str),
]
sheet = []
for key, label, cell, fmt in FIELDS:
    e = entry[key]
    cid = e.get("conflict", {}).get("fact")
    ev = [evidence(i) for i in e["facts"] if i != cid]
    ev += [evidence(i, "corroboration") for i in e.get("corroboration", [])]
    if cid:
        ev.append(evidence(cid, "conflict"))
    sheet.append({"key": key, "label": label, "cell": cell, "display": fmt(e["value"]), "rule": e["rule"], "evidence": ev,
                  "conflict": e.get("conflict", {}).get("note"), "raw": e["value"], "choices": e.get("choices"),
                  "appraiser": key == "gba_sf"})

excel = run["excel"]
d25 = excel["delivered"]["cells"][f"Sales Comparison!D{PPSF}"]
held = [evidence(f["id"]) for f in run["facts"] if f["status"] == "rejected"]
steps = {s["n"]: s for s in run["steps"]}
acc = run["acceptance"]
audit = next(s for s in run["steps"] if s["step"] == "audit")
tamper = run["tamper_tests"]
model = next(s for s in run["steps"] if s["step"] == "model_call")

grid_rows = [(r, lab) for r, _, lab, _ in FACT_ROWS] + [(PPSF, "Sale price per sq ft of the chosen building area"), (LTB, "Land-to-building ratio")]
def cellview(key, v):
    if v is None:
        return ""
    if key == "sale_date":
        return f"{v[5:7]}/{v[8:10]}/{v[:4]}"
    if key == "sale_price":
        return f"{v:,.0f}"
    if key == "land_acres":
        return f"{v:.2f}"
    if key == "year_built":
        return f"{v:.0f}"
    return str(v)


grid_values = {f["cell"]: cellview(f["key"], f["raw"]) for f in sheet}
grid_values["D21"] = entry["sources"]["value"]
grid_values["D10"] = ""
grid = [{"row": r, "label": lab, "value": grid_values.get(f"D{r}", ""),
         "kind": "appraiser" if r == 10 else ("fact" if r <= 22 else "formula")} for r, lab in grid_rows]

data = {
    "sheet": sheet, "held": held, "grid": grid, "notes": notes,
    "docs": DOC,
    "price_per_sf": {"excel_value": d25["value"], "formula": d25["formula"], "cell": f"D{PPSF}", "choices": entry["gba_sf"]["choices"]},
    "meaning_coverage": [len(MEANING_WORDS), len(MeaningCode.__args__)],
    "files": run["files"],
    "hashes": {"before": acc["manifest"]["before"]["protected_sha256"], "after": acc["manifest"]["after"]["protected_sha256"],
               "counts": acc["manifest"]["before"]["counts"], "parts": len(acc["part_diff"]["parts"]),
               "parts_changed": [p["part"] for p in acc["part_diff"]["parts"] if not p["identical"]],
               "cells_changed": acc["part_diff"]["changed_cells"]},
    "excel": {"version": excel["delivered"]["version"], "sha": excel["delivered"]["workbook_sha256"],
              "same_file": excel["delivered_file_is_the_recalculated_file"], "comparison": excel["comparison"]},
    "tamper": tamper,
    "summary": {"inputs_written": len(acc["part_diff"]["changed_cells"]), "formulas": acc["manifest"]["after"]["counts"]["formulas"],
                "formulas_unchanged": acc["manifest"]["before"]["protected_sha256"] == acc["manifest"]["after"]["protected_sha256"],
                "area_unresolved": entry["gba_sf"]["value"] is None},
    "destination": run["destination"],
    "audit": {"rows": audit["rows"], "refusals": audit["refusals"], "chain": audit["chain_verified"], "owner_edit": audit["owner_edit_detected_at_rows"]},
    "model": {"name": model["model"], "in": model["input_tokens"], "out": model["output_tokens"]},
    "steps": run["steps"],
    "attribution": run["attribution"],
}

DIST.mkdir(parents=True, exist_ok=True)
html = (SRC / "index.html").read_text().replace("/*DATA*/null", json.dumps(data, separators=(",", ":")))
(DIST / "index.html").write_text(html)
(DIST / "files").mkdir(exist_ok=True)
for key in ("template", "delivered", "test_copy"):
    shutil.copyfile(ROOT / run["files"][key]["path"], DIST / "files" / Path(run["files"][key]["path"]).name)
fav = Path.home() / "ProjetsDev/the-ai-pipe-website/website/public"
for name in ("favicon.png", "favicon.svg", "apple-touch-icon.png"):
    if (fav / name).exists():
        shutil.copyfile(fav / name, DIST / name)
# Download: workbook + evidence
approve = next(s for s in run["steps"] if s["step"] == "approve")
writes = [s for s in run["steps"] if s["step"] == "write"]
cells = input_cells("D")
evidence_doc = {
    "sale": "3540 St. Lawrence Ave, Reading, PA 19606; recorded instrument 2025031513",
    "note": "Field-to-source references for comparable-1-entered.xlsx. Demonstrated on this one sale; public records only.",
    "fields": [{"field": f["label"], "cell": f"Sales Comparison!{f['cell']}", "written": f["cell"] in cells.values(),
                "value": f["raw"], "rule": f["rule"],
                "sources": [{"role": e["role"], "document": DOC[e["doc"]]["name"], "url": e["url"], "document_sha256": DOC[e["doc"]].get("sha256"),
                             "page": e["page"], "lines": e["spans"], "quote": e["quote"], "on_the_page": e["context"],
                             "check": e["status"] + (f": {e['reason']}" if e["reason"] else "")} for e in f["evidence"]]}
               for f in sheet],
    "attribution": run["attribution"],
}
receipt = {
    "approval": {k: approve[k] for k in ("approved_by", "version_sha256", "template_sha256", "destination", "appraiser_verification")},
    "cell_map": cells,
    "writes": [{k: w.get(k) for k in ("n", "outcome", "what", "reason", "op_id", "output_sha256", "found_sha256", "expected_sha256")
                if w.get(k) is not None} for w in writes],
    "delivered_file": run["files"]["delivered"],
    "template_file": run["files"]["template"],
}
pkg = DIST / "files" / "comp-to-excel-workbook-and-evidence.zip"
info = lambda n: zipfile.ZipInfo(n, date_time=(2026, 9, 30, 12, 0, 0))
with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr(info("comparable-1-entered.xlsx"), (ROOT / run["files"]["delivered"]["path"]).read_bytes(), zipfile.ZIP_DEFLATED)
    z.writestr(info("evidence.json"), json.dumps(evidence_doc, indent=1), zipfile.ZIP_DEFLATED)
    z.writestr(info("approval-and-write-receipt.json"), json.dumps(receipt, indent=1), zipfile.ZIP_DEFLATED)
    z.writestr(info("acceptance-result.json"), json.dumps({"part_diff": acc["part_diff"], "manifest": acc["manifest"],
                                                         "tamper_tests": tamper}, indent=1), zipfile.ZIP_DEFLATED)
    z.writestr(info("README.txt"), "comparable-1-entered.xlsx: the delivered workbook (column D, Comparable 1).\n"
               "evidence.json: every field, its cell, the rule applied and each source with URL, page, lines and quote.\n"
               "approval-and-write-receipt.json: the approval (version hash, destination), the cell map and every write attempt.\n"
               "acceptance-result.json: part-by-part comparison, formula manifest check and the tampering tests.\n"
               "Code and recorded run: https://github.com/fred1433/comp-to-excel\n", zipfile.ZIP_DEFLATED)
print("built", DIST / "index.html", len(html), "bytes; package", pkg.stat().st_size, "bytes")

# The Worker serves site/public/, mounted under /comp-to-excel/.
PUBLIC = ROOT / "site/public"
shutil.rmtree(PUBLIC, ignore_errors=True)
shutil.copytree(DIST, PUBLIC / "comp-to-excel")
(PUBLIC / "_headers").write_text("/comp-to-excel/*\n  X-Robots-Tag: noindex, nofollow\n")
