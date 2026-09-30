"""Build site/dist/ from the recorded run (run/run.json). No network, no API."""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
    DOC[d]["url"] = m.get("found_on") or m["url"]
    DOC[d]["sha256"] = m.get("sha256")

CROP_FOR = {("minutes", "accepted_offer_price"): "motion", ("minutes", "sale_condition"): "motion",
            ("minutes", "offer_acceptance_date"): "header"}


def evidence(fid, role="source"):
    f = facts[fid]
    page = None
    if f["span_ids"] and ":p" in f["span_ids"][0]:
        page = int(f["span_ids"][0].split(":p")[1].split(":")[0])
    crop = crops.get(CROP_FOR.get((f["doc"], f["meaning_code"])), None)
    return {"id": fid, "doc": f["doc"], "meaning": f["meaning"], "value": f["value"], "quote": f["quote"], "page": page,
            "spans": f["span_ids"], "status": f["status"], "reason": f["reason"], "role": role,
            "img": ("img/" + crop["file"]) if crop else None, "derived": f.get("derived_from")}


FIELDS = [
    ("sale_price", "Sale price", "D9", lambda v: f"${v:,.0f}"),
    ("sale_date", "Sale date", "D8", lambda v: f"{int(v[5:7])}/{int(v[8:10])}/{v[:4]}"),
    ("instrument", "Recorded instrument", "D7", str),
    ("parcels", "Parcels conveyed", "D6", lambda v: v.replace("; ", "\n")),
    ("address", "Address", "D5", str),
    ("gba_sf", "Gross building area", "D10", lambda v: f"{v:,.0f} sq ft above grade"),
    ("land_acres", "Land area", "D11", lambda v: f"{v:.2f} acres, two parcels"),
    ("year_built", "Year built", "D12", lambda v: f"{v:.0f}"),
    ("zoning", "Zoning", "D13", str),
    ("property_rights", "Property rights", "D14", str),
    ("sale_conditions", "Conditions of sale", "D15", str),
    ("verification", "Verification", "D17", str),
]
sheet = []
for key, label, cell, fmt in FIELDS:
    e = entry[key]
    cid = e.get("conflict", {}).get("fact")
    ev = [evidence(i) for i in e["facts"] if i != cid]
    if "conflict" in e:
        ev.append(evidence(e["conflict"]["fact"], "conflict"))
    sheet.append({"key": key, "label": label, "cell": cell, "display": fmt(e["value"]), "rule": e["rule"], "evidence": ev,
                  "conflict": e.get("conflict", {}).get("note"), "raw": e["value"]})

excel = run["excel"]
d20 = excel["delivered"]["cells"]["Sales Comparison!D20"]
held = [evidence(f["id"]) for f in run["facts"] if f["status"] == "rejected"]
steps = {s["n"]: s for s in run["steps"]}
acc = run["acceptance"]
audit = next(s for s in run["steps"] if s["step"] == "audit")
tamper = run["tamper_tests"]
model = next(s for s in run["steps"] if s["step"] == "model_call")

grid_rows = [(5, "Address"), (6, "Parcel(s)"), (7, "Recorded instrument"), (8, "Sale date"), (9, "Sale price ($)"),
             (10, "Gross building area (sq ft, above grade)"), (11, "Land area (acres)"), (12, "Year built"), (13, "Zoning"),
             (14, "Property rights conveyed (reported)"), (15, "Conditions of sale (reported facts)"), (16, "Sources"), (17, "Verification"),
             (20, "Sale price per sq ft of gross building area"), (21, "Land-to-building ratio"), (23, "Property rights conveyed (%)"),
             (24, "Adjusted price per sq ft"), (43, "Adjusted price per sq ft, after all adjustments"), (49, "Indicated value, rounded")]
def cellview(key, v):
    if key == "sale_date":
        return f"{v[5:7]}/{v[8:10]}/{v[:4]}"
    if key in ("sale_price", "gba_sf"):
        return f"{v:,.0f}"
    if key == "land_acres":
        return f"{v:.2f}"
    if key == "year_built":
        return f"{v:.0f}"
    return str(v)


grid_values = {f["cell"]: cellview(f["key"], f["raw"]) for f in sheet}
grid_values["D16"] = entry["sources"]["value"]
grid_values["D20"] = f"${d20['value']:,.2f}"
land_ratio = excel["delivered"]["cells"]["Sales Comparison!D21"]["value"]
grid_values["D21"] = f"{land_ratio:.2f}"
grid = [{"row": r, "label": lab, "value": grid_values.get(f"D{r}", ""),
         "kind": "fact" if r <= 17 else ("formula" if r in (20, 21, 24, 43, 49) else "appraiser")} for r, lab in grid_rows]

data = {
    "sheet": sheet, "held": held, "grid": grid, "notes": notes,
    "docs": DOC,
    "price_per_sf": {"value": d20["value"], "formula": d20["formula"], "price": entry["sale_price"]["value"], "gba": entry["gba_sf"]["value"]},
    "files": run["files"],
    "hashes": {"before": acc["manifest"]["before"]["protected_sha256"], "after": acc["manifest"]["after"]["protected_sha256"],
               "counts": acc["manifest"]["before"]["counts"], "parts": len(acc["part_diff"]["parts"]),
               "parts_changed": [p["part"] for p in acc["part_diff"]["parts"] if not p["identical"]],
               "cells_changed": acc["part_diff"]["changed_cells"]},
    "excel": {"version": excel["delivered"]["version"], "sha": excel["delivered"]["workbook_sha256"],
              "same_file": excel["delivered_file_is_the_recalculated_file"], "comparison": excel["comparison"]},
    "tamper": tamper,
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
print("built", DIST / "index.html", len(html), "bytes")

# The Worker serves site/public/, mounted under /comp-to-excel/.
PUBLIC = ROOT / "site/public"
shutil.rmtree(PUBLIC, ignore_errors=True)
shutil.copytree(DIST, PUBLIC / "comp-to-excel")
(PUBLIC / "_headers").write_text("/comp-to-excel/*\n  X-Robots-Tag: noindex, nofollow\n")
