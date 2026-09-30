"""Berks County assessment records, read from the county's public open-data services.

The county's terms forbid redistributing its data in original format and ask derived products to say:
"Information is based on geospatial data from the County of Berks." So the raw records stay local
(sources/private/); the repository keeps only the few field values the demo cites, with that statement.
County records are structured, so no model reads them: each field becomes a fact whose span id is
`county:<parcel>:<FIELD>`.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://services3.arcgis.com/dGYe1jDYrTw1wwpc/arcgis/rest/services"
MASTER = BASE + "/Berks_Assessment_CAMA_Master_File/FeatureServer/14/query"
COMMERCIAL = BASE + "/Berks_Assessment_CAMA_Commercial_File/FeatureServer/13/query"
ATTRIBUTION = "Information is based on geospatial data from the County of Berks."
EASTERN = timezone(timedelta(hours=-4))  # the county stores dates as local midnight (EDT in October)


def query(url: str, where: str) -> list[dict]:
    body = urllib.parse.urlencode({"where": where, "outFields": "*", "f": "json"}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=60) as r:
        return [f["attributes"] for f in json.load(r)["features"]]


def fetch(parid: str) -> dict:
    master = query(MASTER, f"PARID='{parid}'")
    instr = master[0]["INSTRUNO"]
    return {
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "master": master,
        "same_instrument": query(MASTER, f"INSTRUNO='{instr}'"),
        "commercial": query(COMMERCIAL, f"PARID='{parid}'"),
    }


def local_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, EASTERN).date().isoformat()


FLOOR_LABEL = {"B1": "lower level", "1": "floor 1", "2": "floor 2", "3": "floor 3"}


def facts_from_records(rec: dict) -> dict:
    """Derive the cited county facts. Returns {"facts": [...], "sections": [...]} in the fact format of extract.py."""
    m = rec["master"][0]
    parid = m["PARID"]
    sid = lambda field, p=parid: f"county:{p}:{field}"
    facts = [
        {"doc": "county", "meaning_code": "sold_price", "meaning": "sale price on the assessment record",
         "value": str(m["PRICE"]), "span_ids": [sid("PRICE")], "quote": f"PRICE (Sale Price) = {m['PRICE']}"},
        {"doc": "county", "meaning_code": "sale_date", "meaning": "county 'Sale Date' field (the record does not say whether this is the deed date or the recording date)",
         "value": local_date(m["SALEDT"]), "span_ids": [sid("SALEDT")], "quote": f"SALEDT (Sale Date) = {local_date(m['SALEDT'])}"},
        {"doc": "county", "meaning_code": "other", "meaning": "recorded instrument number",
         "value": m["INSTRUNO"], "span_ids": [sid("INSTRUNO")], "quote": f"INSTRUNO (Instrument Number) = {m['INSTRUNO']}"},
        {"doc": "county", "meaning_code": "address", "meaning": "site address on the assessment record",
         "value": f"{m['ADRNO']} {m['ADRSTR']} {m['ADRSUF']}", "span_ids": [sid("ADDRESS")],
         "quote": f"ADRNO/ADRSTR/ADRSUF (site address) = {m['ADRNO']} {m['ADRSTR']} {m['ADRSUF']}"},
        {"doc": "county", "meaning_code": "current_owner", "meaning": "current owner of record (OWN1), not read from the deed",
         "value": m["OWN1"], "span_ids": [sid("OWN1")], "quote": f"OWN1 (Owner Info 1) = {m['OWN1']}"},
    ]
    for p in rec["same_instrument"]:
        facts.append({"doc": "county", "meaning_code": "parcel_or_lot_id", "meaning": "parcel conveyed by this instrument",
                      "value": p["PARID"], "span_ids": [sid("PARID", p["PARID"])],
                      "quote": f"PARID {p['PARID']}: INSTRUNO = {p['INSTRUNO']}, ACRES = {p['ACRES']}, DESC1 = {p['DESC1']}"})
        facts.append({"doc": "county", "meaning_code": "lot_area", "meaning": f"acreage of parcel {p['PARID']}",
                      "value": str(p["ACRES"]), "span_ids": [sid("ACRES", p["PARID"])], "quote": f"PARID {p['PARID']}: ACRES = {p['ACRES']}"})
    sections = []
    for c in rec["commercial"]:
        if c.get("YRBLT"):
            facts.append({"doc": "county", "meaning_code": "year_built", "meaning": "year built on the commercial record",
                          "value": str(c["YRBLT"]), "span_ids": [sid("YRBLT")], "quote": f"YRBLT (Year Built) = {c['YRBLT']}"})
        if c.get("SALEYR1"):
            facts.append({"doc": "county", "meaning_code": "sold_price", "meaning": f"earlier sale, {c['SALEYR1']}-{c['SALEMTH1']}, from the county sale history of this parcel (no instrument, no parties)",
                          "value": str(c["SALEPR1"]), "span_ids": [sid("SALEPR1")],
                          "quote": f"SALEPR1 (Sale Price 1) = {c['SALEPR1']}; SALEYR1/SALEMTH1 (Sale Year 1/Sale Month 1) = {c['SALEYR1']}/{c['SALEMTH1']}"})
        for i in range(1, 9):
            if c.get(f"AREA{i}"):
                sections.append({"line": i, "floor_from": c[f"FLRFROM{i}"].strip(), "floor_to": c[f"FLRTO{i}"].strip(),
                                 "area_sf": c[f"AREA{i}"], "use_type": c[f"USETYPE{i}"]})
    above = [s for s in sections if not s["floor_from"].startswith("B")]
    below = [s for s in sections if s["floor_from"].startswith("B")]
    facts.append({"doc": "county", "meaning_code": "gross_building_area", "meaning": "sum of the assessor's floor lines 1 to 3",
                  "value": str(sum(s["area_sf"] for s in above)), "span_ids": [sid("AREA")],
                  "derived_from": {"op": "sum", "operands": [{"label": FLOOR_LABEL.get(s["floor_from"], s["floor_from"]), "value": s["area_sf"]} for s in above]},
                  "quote": "AREA by floor (sq ft) = " + ", ".join(f"{FLOOR_LABEL.get(s['floor_from'], s['floor_from'])} {s['area_sf']}" for s in sections)})
    return {"parid": parid, "retrieved_at": rec["retrieved_at"], "attribution": ATTRIBUTION, "facts": facts,
            "sections": sections, "below_grade_sf": sum(s["area_sf"] for s in below)}


def county_layer(county: dict) -> dict:
    """Present the cited county fields as a one-page span registry, so county facts go through the same checks."""
    spans, seen = [], set()
    for f in county["facts"]:
        for sid in f["span_ids"]:
            if sid in seen:
                continue
            seen.add(sid)
            spans.append({"id": sid, "text": f["quote"], "bbox": None})
    import hashlib
    pages = [{"page": 1, "width": None, "height": None, "spans": spans}]
    return {"doc": "county", "file_sha256": hashlib.sha256(json.dumps(county, sort_keys=True).encode()).hexdigest(),
            "text_source": "county open-data record", "pages": pages,
            "layer_sha256": hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest()}
