"""Code-side checks of every fact the model proposes.

A fact is accepted only if, in this order:
  1. every cited span exists in the local page registry of that document;
  2. the quote appears literally (whitespace-normalized) in the cited span text;
  3. the meaning code is supported by the words on the cited line or its label neighbours
     (same visual row, or the label printed just under the value);
  4. the normalized value is what the quote says (numbers and dates parsed from the quote).
Anything else stays pending, with its reason. Nothing here asks a model.
"""
from __future__ import annotations

import re
from datetime import date, datetime

NUMERIC = {"sold_price", "accepted_offer_price", "asking_price", "price_per_sf_reported",
           "gross_building_area", "lot_area", "year_built", "parking", "stories"}
DATES = {"sale_date", "offer_acceptance_date", "listing_date", "price_change_date"}
MEANING_WORDS = {
    "sold_price": ["sold", "sale price"],
    "accepted_offer_price": ["offer"],
    "asking_price": ["listed", "list price", "price reduced", "asking"],
    "price_per_sf_reported": ["per sq"],
    "sale_date": ["sold", "sale date"],
    "offer_acceptance_date": ["offer", "meeting"],
    "listing_date": ["listed"],
    "price_change_date": ["reduced", "price change"],
    "gross_building_area": ["sq ft", "sqft", "sq. ft", "square feet"],
    "lot_area": ["lot", "acre"],
    "year_built": ["built"],
    "zoning": ["zoning"],
    "parcel_or_lot_id": ["lot number", "parcel", "tax id", "parid"],
    "property_rights": ["ownership", "fee simple", "leased fee", "leasehold"],
    "sale_condition": ["contingenc", "closing in", "settlement", "financing", "as is", "concession", "seller will", "buyer will"],
}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], start=1)}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def numbers_in(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", text):
        tok = m.group(0).rstrip(",")
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?|\d+(\.\d+)?", tok):
            out.append(float(tok.replace(",", "")))
    return out


def dates_in(text: str) -> list[date]:
    out = []
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text):
        try:
            out.append(date(int(m.group(3)), int(m.group(1)), int(m.group(2))))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in re.finditer(r"\b([A-Za-z]+)\.? (\d{1,2}),? (\d{4})\b", text):
        mo = MONTHS.get(m.group(1).lower())
        if mo:
            out.append(date(int(m.group(3)), mo, int(m.group(2))))
    return out


class Registry:
    """Local page and span registry built from the text layers."""

    def __init__(self, layers: list[dict]):
        self.docs = {L["doc"]: L for L in layers}
        self.spans = {}
        self.page_of = {}
        for L in layers:
            for p in L["pages"]:
                for s in p["spans"]:
                    self.spans[s["id"]] = s
                    self.page_of[s["id"]] = p

    def context(self, span_ids: list[str]) -> str:
        """Cited lines plus label neighbours: same visual row, or the line printed just under or just over a cited value."""
        texts = []
        for sid in span_ids:
            s, page = self.spans[sid], self.page_of[sid]
            texts.append(s["text"])
            if not s.get("bbox"):  # structured records have no page geometry
                continue
            x0, y0, x1, y1 = s["bbox"]
            cy = (y0 + y1) / 2
            for o in page["spans"]:
                if o["id"] == sid or not o.get("bbox"):
                    continue
                ox0, oy0, ox1, oy1 = o["bbox"]
                same_row = abs((oy0 + oy1) / 2 - cy) < 4
                overlap = min(x1, ox1) - max(x0, ox0) > 0
                label_below = 0 <= oy0 - y1 < 8 and overlap
                label_above = 0 <= y0 - oy1 < 8 and overlap
                if same_row or label_below or label_above:
                    texts.append(o["text"])
        return " ".join(texts)


def check_fact(fact: dict, reg: Registry) -> tuple[str, str | None]:
    """Return ("verified", None) or ("rejected", reason)."""
    doc, code, value = fact["doc"], fact["meaning_code"], str(fact["value"]).strip()
    if doc not in reg.docs:
        return "rejected", f"unknown document {doc!r}"
    for sid in fact["span_ids"]:
        if sid not in reg.spans or not sid.startswith(doc + ":"):
            return "rejected", f"span {sid} is not a line of {doc}"
    cited = norm(" ".join(reg.spans[s]["text"] for s in fact["span_ids"]))
    quote = norm(fact["quote"])
    if not quote or quote not in cited:
        return "rejected", "quote is not on the cited lines as written"
    words = MEANING_WORDS.get(code)
    if words:
        ctx = reg.context(fact["span_ids"]).lower()
        if not any(w in ctx for w in words):
            what = code.replace("_", " ")
            return "rejected", f"nothing on or beside the cited line says this is {'an' if what[0] in 'aeiou' else 'a'} {what}"
    if code in NUMERIC:
        try:
            v = float(value.replace(",", "").replace("$", ""))
        except ValueError:
            return "rejected", f"value {value!r} is not a number"
        derived = fact.get("derived_from")
        if derived:  # the only transformation allowed: a sum of numbers the quote states
            nums = numbers_in(quote)
            if derived.get("op") != "sum" or not all(any(abs(o - n) < 1e-9 for n in nums) for o in derived["operands"]):
                return "rejected", "the operands of the derived value are not all in the quote"
            if abs(sum(derived["operands"]) - v) > 1e-9:
                return "rejected", f"the operands do not add up to {value}"
        elif not any(abs(v - n) < 1e-9 for n in numbers_in(quote)):
            return "rejected", f"the quote does not state {value}"
    elif code in DATES:
        try:
            v = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return "rejected", f"value {value!r} is not a YYYY-MM-DD date"
        if v not in dates_in(quote):
            return "rejected", f"the quote does not state the date {value}"
    else:
        q = quote.lower()
        # a word counts as present if the quote has it, or a word with the same first six letters (provides / providing)
        qwords = re.findall(r"[a-z0-9][a-z0-9\-\+/\.]*", q)
        present = lambda w: w in q or (len(w) > 6 and any(x[:6] == w[:6] for x in qwords))
        missing = [w for w in re.findall(r"[a-z0-9][a-z0-9\-\+/\.]*", value.lower()) if len(w) > 2 and not present(w.strip("."))]
        if missing:
            return "rejected", "the value adds words the quote does not contain: " + ", ".join(missing[:4])
    return "verified", None


def check_all(facts: list[dict], reg: Registry) -> list[dict]:
    out = []
    for i, f in enumerate(facts):
        status, reason = check_fact(f, reg)
        out.append({**f, "fact_no": i + 1, "status": status, "reason": reason})
    return out


def excerpt(layers: list[dict], facts: list[dict]) -> list[dict]:
    """Keep only the cited lines and their label neighbours, enough to re-run every check without the documents."""
    reg = Registry(layers)
    keep = set()
    for f in facts:
        for sid in f["span_ids"]:
            if sid not in reg.spans:
                continue
            keep.add(sid)
            s, page = reg.spans[sid], reg.page_of[sid]
            if not s.get("bbox"):
                continue
            x0, y0, x1, y1 = s["bbox"]
            for o in page["spans"]:
                if not o.get("bbox"):
                    continue
                ox0, oy0, ox1, oy1 = o["bbox"]
                ov = min(x1, ox1) - max(x0, ox0) > 0
                if abs((oy0 + oy1) / 2 - (y0 + y1) / 2) < 4 or (0 <= oy0 - y1 < 8 and ov) or (0 <= y0 - oy1 < 8 and ov):
                    keep.add(o["id"])
    out = []
    for L in layers:
        pages = [{**{k: p[k] for k in ("page", "width", "height")}, "spans": [s for s in p["spans"] if s["id"] in keep]} for p in L["pages"]]
        out.append({**{k: L[k] for k in ("doc", "file_sha256", "layer_sha256", "text_source")}, "excerpt": True,
                    "pages": [p for p in pages if p["spans"]]})
    return out
