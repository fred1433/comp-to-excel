"""Code-side checks of every fact the model proposes.

A fact is accepted only if, in this order:
  1. every cited span exists in the local page registry of that document;
  2. the quote appears literally (whitespace-normalized) in the cited span text;
  3. the normalized value is what the quote says (numbers and dates parsed from the quote; a price must be written
     as an amount of money; a negation in the quote must survive in a text value);
  4. for selected field types, the line that holds the value, with its own row and the label printed just above or
     under it, names what the value is and does not name something else (sold against asking, building against lot);
     a county field is checked against the field it comes from; a derived sum names each operand's field once.
These checks do not replace appraisal review.
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
        picked: dict[str, dict] = {}
        for sid in span_ids:
            s, page = self.spans[sid], self.page_of[sid]
            picked[sid] = s
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
                    picked[o["id"]] = o
        # each line once, in reading order (page, then top to bottom, then left to right)
        def pos(sp):
            page = int(sp["id"].split(":p")[1].split(":")[0]) if ":p" in sp["id"] else 0
            b = sp.get("bbox") or [0, 0, 0, 0]
            return (page, round(b[1]), b[0])
        return " ".join(sp["text"] for sp in sorted(picked.values(), key=pos))


PRICE_CODES = {"sold_price", "accepted_offer_price", "asking_price", "price_per_sf_reported"}
# words that, next to the value, say it is something else
CONTRADICTS = {
    "sold_price": ["price reduced", "listed", "asking", "per sq"],
    "asking_price": ["sold"],
    "sale_date": ["listed", "price reduced"],
    "listing_date": ["sold", "reduced"],
    "price_change_date": ["sold", "listed"],
    "gross_building_area": ["lot", "acre"],
}
# structured county records: the field itself is the label
COUNTY_FIELDS = {"sold_price": {"PRICE", "SALEPR1"}, "sale_date": {"SALEDT"}, "other": {"INSTRUNO"}, "current_owner": {"OWN1"},
                 "address": {"ADDRESS"}, "parcel_or_lot_id": {"PARID"}, "lot_area": {"ACRES"}, "year_built": {"YRBLT"},
                 "gross_building_area": {"AREA"}}
NEGATIONS = {"no", "not", "without", "non", "never", "none"}


def _article(what: str) -> str:
    return "an" if what[0] in "aeiou" else "a"


def _number_occurrences(text: str, v: float) -> list[re.Match]:
    out = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", text):
        tok = m.group(0).rstrip(",")
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?|\d+(\.\d+)?", tok) and abs(float(tok.replace(",", "")) - v) < 1e-9:
            out.append(m)
    return out


def _is_amount(text: str, m: re.Match) -> bool:
    """An amount of money: written with $ and not followed by a unit of time or a percent sign."""
    before, after = text[: m.start()].rstrip(), text[m.end():].lstrip().lower()
    return before.endswith("$") and not re.match(r"(%|days?\b|years?\b|months?\b|weeks?\b)", after)


def _bound(code: str, span_ids: list[str], reg: "Registry", holds) -> tuple[bool, str]:
    """The value must sit on a cited line whose own label context (that line, its row, the label above or below it)
    says what the value means and does not say it is something else. Words from other cited lines do not count."""
    words, bad = MEANING_WORDS.get(code, []), CONTRADICTS.get(code, [])
    carriers = [sid for sid in span_ids if holds(reg.spans[sid]["text"])]
    if not carriers:
        return False, "the value is not on any cited line"
    what = code.replace("_", " ")
    for sid in carriers:
        ctx = reg.context([sid]).lower()
        if words and not any(w in ctx for w in words):
            continue
        if any(w in ctx for w in bad):
            continue
        return True, ""
    return False, f"the line that holds the value does not label it as {_article(what)} {what}"


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
    structured = all(not reg.spans[s].get("bbox") for s in fact["span_ids"])
    what = code.replace("_", " ")
    if structured:
        fields = {s.rsplit(":", 1)[1] for s in fact["span_ids"]}
        allowed = COUNTY_FIELDS.get(code)
        if allowed is None or not fields <= allowed:
            return "rejected", f"field {', '.join(sorted(fields))} does not hold {_article(what)} {what}"
        if code == "gross_building_area" and not fact.get("derived_from"):
            return "rejected", "one floor line of the county AREA field is not a building area; use a labelled sum"

    if code in NUMERIC:
        try:
            v = float(value.replace(",", "").replace("$", ""))
        except ValueError:
            return "rejected", f"value {value!r} is not a number"
        derived = fact.get("derived_from")
        if derived:  # the only transformation allowed: a sum of labelled operands, each a distinct field of the quote
            ops = derived.get("operands", [])
            if derived.get("op") != "sum" or not ops or not all(isinstance(o, dict) and o.get("label") for o in ops):
                return "rejected", "each operand of a derived value must name the field it comes from"
            labels = [o["label"] for o in ops]
            if len(set(labels)) != len(labels):
                return "rejected", "an operand field is used more than once"
            fields = {norm(x) for x in quote.split("=", 1)[-1].split(",")}
            for o in ops:
                if f"{o['label']} {o['value']}" not in fields:
                    return "rejected", f"the quote has no field reading exactly \"{o['label']} {o['value']}\""
            if abs(sum(o["value"] for o in ops) - v) > 1e-9:
                return "rejected", f"the operands do not add up to {value}"
        else:
            occ = _number_occurrences(quote, v)
            if not occ:
                return "rejected", f"the quote does not state {value}"
            if code in PRICE_CODES and not structured and not any(_is_amount(quote, m) for m in occ):
                return "rejected", f"{value} is not written as an amount of money in the quote"
            if not structured:
                ok, why = _bound(code, fact["span_ids"], reg, lambda t: bool(_number_occurrences(t, v)))
                if not ok:
                    return "rejected", why
    elif code in DATES:
        try:
            v = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return "rejected", f"value {value!r} is not a YYYY-MM-DD date"
        if v not in dates_in(quote):
            return "rejected", f"the quote does not state the date {value}"
        if not structured:
            ok, why = _bound(code, fact["span_ids"], reg, lambda t: v in dates_in(t))
            if not ok:
                return "rejected", why
    else:
        words = MEANING_WORDS.get(code)
        if words and not structured:
            ctx = reg.context(fact["span_ids"]).lower()
            if not any(w in ctx for w in words):
                return "rejected", f"nothing on or beside the cited line says this is {_article(what)} {what}"
        q = quote.lower()
        vl = value.lower()
        qwords = re.findall(r"[a-z0-9][a-z0-9\-\+/\.]*", q)
        vwords = re.findall(r"[a-z0-9][a-z0-9\-\+/\.]*", vl)
        # a word counts as present if the quote has it, or a word with the same first six letters (provides / providing)
        present = lambda w: w in qwords or w in q or (len(w) > 6 and any(x[:6] == w[:6] for x in qwords))
        missing = [w for w in vwords if (len(w) > 2 or w in NEGATIONS) and not present(w.strip("."))]
        if missing:
            return "rejected", "the value adds words the quote does not contain: " + ", ".join(missing[:4])
        # a negation must survive: read it in the cited text, including the words just before the quote starts,
        # and require the value to keep the same pair ("no contingencies"), not the two words apart
        start = cited.lower().find(q)
        before = re.findall(r"[a-z0-9][a-z0-9\-\+/\.]*", cited.lower()[:start])[-2:] if start > 0 else []
        window = before + qwords
        vtext = " ".join(w.strip(".,;") for w in vwords)
        for i, w in enumerate(window[:-1]):
            nxt = window[i + 1].strip(".,;")
            if w in NEGATIONS and nxt in [x.strip(".,;") for x in vwords] and f"{w} {nxt}" not in vtext:
                return "rejected", f"the value drops the negation in \"{w} {nxt}\""
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
