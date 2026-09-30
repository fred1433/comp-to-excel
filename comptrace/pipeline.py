"""The controlled entry of one sale, step by step, against a real PostgreSQL database.

Every step appends to `self.steps` what it did and what it refused, and writes an audit event through a separate
autocommit connection, so a failed transaction never erases its own audit trail.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from datetime import date
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from . import acceptance, writer
from .verify import Registry, check_all
from .workbook import SHEET, destination, input_cells

HERE = Path(__file__).resolve().parent
DESTINATION = destination("D") + " (Comparable 1)"

DOC_LABELS = {
    "county": "Berks County assessment record (county open-data service)",
    "minutes": "St. Lawrence Borough Council minutes, September 11, 2025 (scanned PDF)",
    "mls-cb": "MLS PABK2052516 listing page, coldwellbankerhomes.com (saved as PDF)",
    "mls-coalition": "MLS PABK2052516 listing page, coalitionpg.com (saved as PDF)",
}
TRANSACTION_CODES = {"sold_price", "sale_date", "accepted_offer_price", "offer_acceptance_date"}
LISTING_CODES = {"asking_price", "listing_date", "price_change_date", "price_per_sf_reported", "listing_broker", "selling_broker"}


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def county_sections(layers: dict) -> dict:
    area = next(s["text"] for p in layers["county"]["pages"] for s in p["spans"] if s["id"].endswith(":AREA"))
    below = sum(int(n) for n in re.findall(r"lower level (\d+)", area))
    return {"below": below}


def norm_address(s: str) -> str:
    s = s.upper().replace(".", " ").replace(",", " ")
    s = re.sub(r"\bSAINT\b", "ST", s)
    s = re.sub(r"\bAVENUE\b|\bAVE\b|\bAV\b", "AV", s)
    return " ".join(s.split()[:4])


class Run:
    def __init__(self, pg, layers: dict[str, dict], sources: dict[str, dict], template: Path, outdir: Path):
        self.pg, self.layers, self.sources, self.template, self.outdir = pg, layers, sources, Path(template), Path(outdir)
        self.steps: list[dict] = []
        self.audit_conn = pg.connect("comp_app", autocommit=True)
        self.reg = Registry(list(layers.values()))

    # ---------- plumbing ----------
    def conn(self, role: str) -> psycopg.Connection:
        c = self.pg.connect("comp_app")
        c.execute(f"set role {role}")
        return c

    def audit(self, role: str, action: str, obj: str | None = None, **detail) -> None:
        self.audit_conn.execute(f"set role {role}")
        self.audit_conn.execute("insert into comp.audit_log(action, object, detail, prev_sha256, row_sha256) values (%s,%s,%s,'','')",
                                (action, obj, Jsonb(detail)))

    def step(self, name: str, outcome: str, **data) -> dict:
        s = {"n": len(self.steps) + 1, "step": name, "outcome": outcome, **data}
        self.steps.append(s)
        return s

    # ---------- 1. documents ----------
    def ingest(self, doc: str) -> bool:
        L, src = self.layers[doc], self.sources[doc]
        with self.conn("comp_extractor") as c:
            row = c.execute("select doc from comp.source_document where file_sha256 = %s", (L["file_sha256"],)).fetchone()
            if row:
                self.audit("comp_extractor", "document_replay_ignored", doc, file_sha256=L["file_sha256"])
                c.execute("insert into comp.match_review(doc, verdict, reasons) values (%s,'same_document_replay',%s)",
                          (doc, Jsonb({"file_sha256": L["file_sha256"], "already_ingested_as": row[0]})))
                self.step("ingest", "replay_ignored", doc=doc, file_sha256=L["file_sha256"])
                return False
            c.execute("insert into comp.source_document values (%s,%s,%s,%s,%s,%s,%s)",
                      (doc, L["file_sha256"], L["layer_sha256"], L["text_source"], DOC_LABELS[doc], src.get("url"), src.get("retrieved_at")))
            with c.cursor() as cur:
                cur.executemany("insert into comp.span values (%s,%s,%s,%s,%s)",
                                [(s["id"], doc, p["page"], s["text"], s.get("bbox")) for p in L["pages"] for s in p["spans"]])
        self.audit("comp_extractor", "document_read", doc, file_sha256=L["file_sha256"], layer_sha256=L["layer_sha256"],
                   text_source=L["text_source"], spans=sum(len(p["spans"]) for p in L["pages"]))
        self.step("ingest", "stored", doc=doc, file_sha256=L["file_sha256"], text_source=L["text_source"])
        return True

    # ---------- 2-3. facts ----------
    def record_model_call(self, call: dict) -> None:
        self.audit("comp_extractor", "model_call", call.get("model"), request_id=call.get("request_id"),
                   input_tokens=call.get("input_tokens"), output_tokens=call.get("output_tokens"),
                   documents=[d["doc"] for d in call.get("documents", [])], replayed_from_record=True)
        self.step("model_call", "recorded_response_replayed", model=call.get("model"), request_id=call.get("request_id"),
                  input_tokens=call.get("input_tokens"), output_tokens=call.get("output_tokens"))

    def check_and_store(self, facts: list[dict]) -> list[dict]:
        checked = check_all(facts, self.reg)
        with self.conn("comp_extractor") as c:
            for f in checked:
                scope = ("transaction" if f["meaning_code"] in TRANSACTION_CODES and f["doc"] != "minutes"
                         else "authorization" if f["doc"] == "minutes" and f["meaning_code"] in TRANSACTION_CODES
                         else "listing" if f["meaning_code"] in LISTING_CODES else "property")
                f["scope"] = scope
                f["id"] = c.execute(
                    "insert into comp.fact(doc, meaning_code, meaning, value, span_ids, quote, derived_from, check_status, reject_reason, scope)"
                    " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                    (f["doc"], f["meaning_code"], f["meaning"], f["value"], f["span_ids"], f["quote"],
                     Jsonb(f.get("derived_from")) if f.get("derived_from") else None, f["status"], f["reason"], scope)).fetchone()[0]
        for f in checked:
            if f["status"] == "rejected":
                self.audit("comp_extractor", "fact_rejected", f"fact {f['id']}", doc=f["doc"], meaning_code=f["meaning_code"],
                           value=f["value"], reason=f["reason"])
        n_ok = sum(f["status"] == "verified" for f in checked)
        self.step("check_facts", "done", verified=n_ok, rejected=len(checked) - n_ok,
                  rejected_facts=[{k: f[k] for k in ("id", "doc", "meaning_code", "value", "quote", "reason")} for f in checked if f["status"] == "rejected"])
        return checked

    # ---------- 4. matching ----------
    def create_from_county(self, county: dict, facts: list[dict]) -> dict:
        v = lambda code, pred=lambda f: True: [f for f in facts if f["doc"] == "county" and f["meaning_code"] == code and f["status"] == "verified" and pred(f)]
        instrument = v("other")[0]["value"]
        price = [f for f in v("sold_price") if "earlier" not in f["meaning"]][0]
        earlier = [f for f in v("sold_price") if "earlier" in f["meaning"]]
        sale_date = v("sale_date")[0]["value"]
        parcels = [f["value"] for f in v("parcel_or_lot_id")]
        with self.conn("comp_extractor") as c:
            prop = c.execute("insert into comp.property(label) values (%s) returning id", (v("address")[0]["value"],)).fetchone()[0]
            for p in parcels:
                c.execute("insert into comp.parcel values (%s,%s)", (p, prop))
            t1 = c.execute("insert into comp.sale_transaction(property_id, instrument_no, sale_date, price) values (%s,%s,%s,%s) returning id",
                           (prop, instrument, sale_date, price["value"])).fetchone()[0]
            for p in parcels:
                c.execute("insert into comp.transaction_parcel values (%s,%s)", (t1, p))
            c.execute("update comp.fact set transaction_id = %s where doc = 'county' and scope = 'transaction' and not (meaning like 'earlier%%')", (t1,))
            c.execute("update comp.fact set property_id = %s where doc = 'county'", (prop,))
            t0 = None
            for e in earlier:
                period = re.search(r"(\d{4})-(\d{2})", e["meaning"]).group(0)
                t0 = c.execute("insert into comp.sale_transaction(property_id, sale_period, price) values (%s,%s,%s) returning id",
                               (prop, period, e["value"])).fetchone()[0]
                c.execute("insert into comp.transaction_parcel values (%s,%s)", (t0, county["parid"]))
                c.execute("update comp.fact set transaction_id = %s where id = %s", (t0, e["id"]))
                c.execute("insert into comp.match_review(doc, verdict, transaction_id, reasons) values ('county','resale',%s,%s)",
                          (t0, Jsonb({"same_parcel": county["parid"], "sale_period": period, "price": e["value"],
                                      "why_new": "different period and price on the same parcel: an earlier sale, not the same transaction"})))
        self.audit("comp_extractor", "transaction_created", f"transaction {t1}", instrument_no=instrument, parcels=parcels)
        self.step("match", "transaction_created_from_recorded_instrument", doc="county", transaction_id=t1, instrument_no=instrument,
                  sale_date=sale_date, price=price["value"], parcels=parcels)
        if t0:
            self.step("match", "resale_kept_apart", doc="county", transaction_id=t0, sale_period=period, price=earlier[0]["value"])
        self.property_id, self.t1 = prop, t1
        return {"property_id": prop, "t1": t1, "t0": t0, "instrument": instrument}

    def duplicate_instrument_refused(self, instrument: str) -> None:
        c = self.conn("comp_extractor")
        try:
            c.execute("insert into comp.sale_transaction(property_id, instrument_no, sale_date, price) values (%s,%s,'2025-10-10',390000)",
                      (self.property_id, instrument))
            c.commit()
            outcome, err = "accepted", None
        except psycopg.errors.UniqueViolation as e:
            c.rollback()
            outcome, err = "refused_by_database", e.diag.message_primary
        finally:
            c.close()
        self.audit("comp_extractor", "duplicate_transaction_" + outcome, f"instrument {instrument}", error=err)
        self.step("match", outcome, what="second transaction row on the same recorded instrument", error=err)

    def match_listing(self, doc: str, facts: list[dict]) -> None:
        mine = [f for f in facts if f["doc"] == doc and f["status"] == "verified"]
        with self.conn("comp_extractor") as c:
            t = c.execute("select id, sale_date::text, price::float, instrument_no from comp.sale_transaction where id = %s", (self.t1,)).fetchone()
            parcels = [r[0] for r in c.execute("select parid from comp.transaction_parcel where transaction_id = %s", (self.t1,))]
            label = c.execute("select label from comp.property where id = %s", (self.property_id,)).fetchone()[0]
            prices = {float(f["value"]) for f in mine if f["meaning_code"] == "sold_price"}
            dates = {f["value"] for f in mine if f["meaning_code"] == "sale_date"}
            lots = {f["value"] for f in mine if f["meaning_code"] == "parcel_or_lot_id"}
            addrs = {norm_address(f["value"]) for f in mine if f["meaning_code"] == "address"}
            reasons = {
                "address_matches_property": norm_address(label) in addrs,
                "sold_price_equals_recorded_price": t[2] in prices,
                "sold_date_equals_recorded_date": t[1] in dates,
                "lot_number_equals_last_4_digits_of_parcel_id_heuristic": any(p.endswith(l) for p in parcels for l in lots),
                "carries_recorded_instrument": False,
            }
            if reasons["address_matches_property"] and reasons["sold_price_equals_recorded_price"] and reasons["sold_date_equals_recorded_date"]:
                verdict, tid = "same_sale_other_source", self.t1
            elif reasons["address_matches_property"] and not prices and not dates:
                verdict, tid = "property_facts", None
            else:
                verdict, tid = "held_for_review", self.t1
            c.execute("insert into comp.match_review(doc, verdict, transaction_id, reasons) values (%s,%s,%s,%s)", (doc, verdict, tid, Jsonb(reasons)))
            if verdict == "same_sale_other_source":
                c.execute("update comp.fact set transaction_id = %s where doc = %s and scope = 'transaction' and check_status = 'verified'", (self.t1, doc))
            c.execute("update comp.fact set property_id = %s where doc = %s", (self.property_id, doc))
        self.audit("comp_extractor", "match_" + verdict, doc, transaction_id=tid, reasons=reasons)
        self.step("match", verdict, doc=doc, transaction_id=tid, reasons=reasons)

    def match_minutes(self, facts: list[dict]) -> None:
        mine = [f for f in facts if f["doc"] == "minutes" and f["status"] == "verified"]
        offer = [f for f in mine if f["meaning_code"] == "accepted_offer_price"]
        with self.conn("comp_extractor") as c:
            t = c.execute("select price::float, sale_date from comp.sale_transaction where id = %s", (self.t1,)).fetchone()
            reasons = {
                "document_kind": "council authorization, not a closing",
                "accepted_offer_equals_recorded_price": bool(offer) and float(offer[0]["value"]) == t[0],
                "meeting_to_recorded_sale_days": (t[1] - date(2025, 9, 11)).days,
                "carries_recorded_instrument": False,
            }
            c.execute("insert into comp.match_review(doc, verdict, transaction_id, reasons) values ('minutes','held_for_review',%s,%s)",
                      (self.t1, Jsonb(reasons)))
            c.execute("update comp.fact set property_id = %s where doc = 'minutes'", (self.property_id,))
        self.audit("comp_extractor", "match_held_for_review", "minutes", transaction_id=self.t1, reasons=reasons)
        self.step("match", "held_for_review", doc="minutes", transaction_id=self.t1, reasons=reasons)

    def confirm_review(self, doc: str, who: str) -> None:
        with self.conn("comp_reviewer") as c:
            c.execute("update comp.match_review set confirmed_by = %s, confirmed_at = now() where doc = %s and verdict = 'held_for_review'", (who, doc))
            c.execute("reset role"); c.execute("set role comp_extractor")
            c.execute("update comp.fact set transaction_id = %s where doc = %s and scope = 'authorization' and check_status = 'verified'", (self.t1, doc))
        self.audit("comp_reviewer", "match_confirmed", doc, transaction_id=self.t1, confirmed_by=who,
                   as_="authorization that preceded this recorded sale")
        self.step("review", "match_confirmed", doc=doc, confirmed_by=who, linked_as="authorization that preceded the recorded sale")

    # ---------- 5. comp record ----------
    def assemble(self, facts: list[dict]) -> dict:
        ok = [f for f in facts if f["status"] == "verified"]
        pick = lambda doc, code, pred=lambda f: True: next(f for f in ok if f["doc"] == doc and f["meaning_code"] == code and pred(f))
        c_price = pick("county", "sold_price", lambda f: "earlier" not in f["meaning"])
        c_date = pick("county", "sale_date")
        c_instr = pick("county", "other")
        c_addr = pick("county", "address")
        c_parcels = [f for f in ok if f["doc"] == "county" and f["meaning_code"] == "parcel_or_lot_id"]
        c_acres = [f for f in ok if f["doc"] == "county" and f["meaning_code"] == "lot_area"]
        c_gba = pick("county", "gross_building_area")
        c_year = pick("county", "year_built")
        m_price = pick("mls-cb", "sold_price", lambda f: "Sold" in f["quote"])
        m_date = pick("mls-cb", "sale_date")
        m_zoning = pick("mls-cb", "zoning")
        m_rights = pick("mls-cb", "property_rights")
        m_excl = pick("mls-cb", "exclusion")
        m_ask = sorted([f for f in ok if f["doc"] == "mls-cb" and f["meaning_code"] == "asking_price"], key=lambda f: -float(f["value"]))
        m_list_date = pick("mls-cb", "listing_date")
        m_change_date = pick("mls-cb", "price_change_date")
        m_ppsf = pick("mls-cb", "price_per_sf_reported")
        m_year = pick("mls-cb", "year_built")
        m_lot = pick("mls-cb", "lot_area", lambda f: f["value"] == "0.31")
        k_gba = pick("mls-coalition", "gross_building_area")
        n_offer = pick("minutes", "accepted_offer_price")
        n_cond = pick("minutes", "sale_condition")
        n_seller = pick("minutes", "seller")
        n_hall = pick("minutes", "address")
        c_owner = pick("county", "current_owner")
        m_stories = pick("mls-cb", "stories")
        c_sections = county_sections(self.layers)
        acres_total = round(sum(float(f["value"]) for f in c_acres), 2)
        ids = lambda *fs: [f["id"] for f in fs]
        us = lambda iso: f"{int(iso[5:7])}/{int(iso[8:10])}/{iso[:4]}"
        entry = {
            "address": {"value": "3540 St. Lawrence Ave, Reading, PA 19606 (with rear parcel)", "facts": ids(c_addr, *c_parcels),
                        "rule": "county site address; the recorded instrument conveys two parcels"},
            "parcels": {"value": "; ".join(f["value"] for f in c_parcels), "facts": ids(*c_parcels), "rule": "parcels on the recorded instrument"},
            "instrument": {"value": c_instr["value"], "facts": ids(c_instr), "rule": "county instrument number"},
            "sale_date": {"value": c_date["value"], "facts": ids(c_date, m_date),
                          "rule": "county 'Sale Date' field, which does not say whether it is the deed or the recording date; the MLS sold date agrees"},
            "sale_price": {"value": float(c_price["value"]), "facts": ids(c_price, m_price), "corroboration": ids(n_offer),
                           "rule": "county assessment record's sale-price field; the record lists instrument 2025031513. The MLS sold price agrees; the council's accepted offer, 29 days earlier, corroborates"},
            "gba_sf": {"value": None, "facts": ids(c_gba, k_gba), "status": "appraiser to choose",
                       "choices": [{"basis": "county assessor, floor lines 1 to 3", "sf": float(c_gba["value"]), "fact": c_gba["id"]},
                                   {"basis": "listing site", "sf": float(k_gba["value"]), "fact": k_gba["id"]}],
                       "rule": "sources disagree on the area, so the cell is left for the appraiser; no price per sq ft exists until then"},
            "gba_reported": {"value": (f"County assessor: {float(c_gba['value']):,.0f} sq ft on floor lines 1 to 3, plus {c_sections['below']:,} on the lower level (county code B1). "
                                       f"Listing site: {float(k_gba['value']):,.0f} sq ft. MLS: {m_stories['value']} stories."),
                             "facts": ids(c_gba, k_gba, m_stories), "rule": "every reported area with its source and scope"},
            "land_acres": {"value": acres_total, "facts": ids(*c_acres, m_lot), "rule": "sum of the parcels on the recorded instrument",
                           "conflict": {"value": float(m_lot["value"]), "fact": m_lot["id"], "note": "the MLS lot size covers the front parcel only"}},
            "year_built": {"value": float(c_year["value"]), "facts": ids(c_year, m_year), "rule": "county record preferred over an MLS estimate",
                           "conflict": {"value": float(m_year["value"]), "fact": m_year["id"], "note": "MLS shows 1900, with Year Built Source: Estimated on the next line"}},
            "zoning": {"value": m_zoning["value"].replace("AND", "and"), "facts": ids(m_zoning), "rule": "MLS, only source"},
            "grantor": {"value": "St. Lawrence Borough; the property is the former borough hall", "facts": ids(n_seller, n_hall),
                        "rule": "seller as reported in the council minutes, which record the vote to sell the old borough hall; the deed was not read"},
            "grantee": {"value": f"{c_owner['value'].title().replace('Llc', 'LLC')}; buyer not read from the deed",
                        "facts": ids(c_owner), "rule": "current owner on the county record today; the deed, and so the buyer of record, was not retrieved"},
            "property_rights": {"value": f"{m_rights['value']}, per the MLS Ownership field; not verified", "facts": ids(m_rights),
                                "rule": "reported, not verified"},
            "sale_conditions": {"value": f"No contingencies; closing in 30 days or as soon as possible (council motion, 9/11/2025)",
                                "facts": ids(n_cond), "rule": "the conditions stated in the accepted offer; no verdict on arm's length"},
            "marketing": {"value": (f"Listed at ${float(m_ask[0]['value']):,.0f} on {us(m_list_date['value'])}; reduced to "
                                    f"${float(m_ask[1]['value']):,.0f} on {us(m_change_date['value'])}; sold on {us(m_date['value'])} (MLS history)"),
                          "facts": ids(m_ask[0], m_list_date, m_ask[1], m_change_date, m_date), "rule": "MLS price history"},
            "non_realty": {"value": f"Excluded from the sale: {m_excl['value'].lower()} (MLS)", "facts": ids(m_excl), "rule": "reported, not verified"},
            "sources": {"value": "Berks County assessment record (open data, retrieved 9/30/2026); St. Lawrence Borough Council minutes, 9/11/2025; MLS PABK2052516 on two listing sites",
                        "facts": [], "rule": "documents behind the facts above"},
            "verification": {"value": "Public records reviewed; no transaction-party verification performed", "facts": [], "rule": "state of verification"},
        }
        notes = {"mls_price_per_sf": {"fact": m_ppsf["id"], "value": float(m_ppsf["value"]),
                                      "reading": f"{float(m_ask[1]['value']):,.0f} / {float(k_gba['value']):,.0f} sq ft = {float(m_ask[1]['value']) / float(k_gba['value']):.2f}: "
                                                 "the listing's price per sq ft uses the reduced asking price, so it is not a sale unit price"}}
        with self.conn("comp_extractor") as c:
            cid = c.execute("insert into comp.comp_record(transaction_id, entry, status, version_sha256) values (%s,%s,'extracted',%s) returning id",
                            (self.t1, Jsonb(entry), sha(entry))).fetchone()[0]
        # evidence check: every fact behind the entry must be verified in the database
        with self.conn("comp_reviewer") as c:
            used = sorted({i for e in entry.values() for i in e["facts"] + e.get("corroboration", [])}
                          | {e["conflict"]["fact"] for e in entry.values() if "conflict" in e})
            bad = c.execute("select id from comp.fact where id = any(%s) and check_status <> 'verified'", (used,)).fetchall()
            status = "evidence_checked" if not bad else "extracted"
            c.execute("update comp.comp_record set status = %s where id = %s", (status, cid))
        self.comp_id, self.entry = cid, entry
        self.audit("comp_reviewer", "comp_record_" + status, f"comp {cid}", facts=used)
        self.step("assemble", status, comp_id=cid, entry=entry, notes=notes)
        return entry

    # ---------- 6-8. approval and writing ----------
    def input_map(self) -> dict[str, str]:
        return input_cells("D")

    def version(self, conn, comp_id: int, destination: str) -> tuple[str, dict]:
        entry = conn.execute("select entry from comp.comp_record where id = %s", (comp_id,)).fetchone()[0]
        ids = sorted({i for e in entry.values() for i in e["facts"] + e.get("corroboration", [])})
        evidence = conn.execute(
            "select f.id, f.doc, f.value, f.quote, f.span_ids, d.file_sha256, d.layer_sha256 from comp.fact f join comp.source_document d using (doc) "
            "where f.id = any(%s) order by f.id", (ids,)).fetchall()
        basis = {"entry": entry, "evidence": [list(r) for r in evidence], "input_map": self.input_map(),
                 "template_sha256": acceptance.sha256_file(self.template), "destination": destination}
        return sha(basis), basis

    def write(self, output: Path, crash_after_save: bool = False) -> dict:
        """Writer role. Refuses unless an approval matches the current version of the record."""
        c = self.conn("comp_writer")
        try:
            rec = c.execute("select id, status from comp.comp_record where transaction_id = %s", (self.t1,)).fetchone()
            if not rec:
                raise writer.WriteRefused("no record approved for entry is visible to the writer role")
            appr = c.execute("select id, version_sha256 from comp.approval where comp_id = %s and destination = %s order by id desc limit 1",
                             (rec[0], DESTINATION)).fetchone()
            current, basis = self.version(c, rec[0], DESTINATION)
            if not appr or appr[1] != current:
                raise writer.WriteRefused("the record, its evidence, the input map or the template changed after approval")
            entry = basis["entry"]
            m = self.input_map()
            writes = {}
            for field, cell in m.items():
                val = entry[field]["value"]
                writes[cell] = date.fromisoformat(val) if field == "sale_date" else val
            done = c.execute("select op_id, output_sha256, output_path from comp.write_operation where approval_id = %s and status = 'done'", (appr[0],)).fetchone()
            if done:  # a receipt is not a confirmation: look at the file again
                state = writer.resume_state(Path(done[2]), done[1])
                outcome = "already_written_file_verified" if state == "expected" else f"receipt_mismatch_{state}"
                self.audit("comp_writer", outcome, str(done[0]), output_sha256=done[1])
                return {"outcome": outcome, "op_id": str(done[0]), "output_sha256": done[1]}
            pending = c.execute("select op_id, expected_patched_sha256, output_path from comp.write_operation where approval_id = %s and status = 'intent'",
                                (appr[0],)).fetchone()
            if pending:
                return self._resume(c, pending, writes, m)
            probe = output.with_suffix(".probe.xlsx")
            writer.patch(self.template, probe, SHEET, writes, set(m.values()))
            expected = acceptance.sha256_file(probe)
            probe.unlink()
            op = uuid.uuid4()
            c.execute("insert into comp.write_operation(op_id, approval_id, version_sha256, template_sha256, expected_patched_sha256, output_path, status)"
                      " values (%s,%s,%s,%s,%s,%s,'intent')", (op, appr[0], current, basis["template_sha256"], expected, str(output)))
            c.commit()
            self.audit("comp_writer", "write_intent", str(op), approval_id=appr[0], expected_output_sha256=expected)
            writer.patch(self.template, output, SHEET, writes, set(m.values()))
            if crash_after_save:
                self.audit("comp_writer", "worker_stopped_before_completion", str(op), note="simulated crash after the workbook was saved")
                raise SystemExit("simulated crash after save")
            return self._finish(c, op, output, writes, m)
        except writer.WriteRefused as e:
            c.rollback()
            self.audit("comp_writer", "write_refused", DESTINATION, reason=str(e))
            return {"outcome": "refused", "reason": str(e)}
        finally:
            c.close()

    def _verify_output(self, output: Path, writes: dict, m: dict) -> dict:
        allowed = set(m.values())
        pdiff = acceptance.part_diff(self.template, output, writer.sheet_part(self.template, SHEET), allowed)
        expected_inputs = {f"{SHEET}!{cell}": acceptance.expected(val) for cell, val in writes.items()}
        acc = acceptance.check(self.template, output, {SHEET: allowed}, expected_inputs)
        return {"part_diff": pdiff, "acceptance": acc, "ok": pdiff["ok"] and acc["ok"]}

    def _finish(self, c, op, output: Path, writes: dict, m: dict) -> dict:
        v = self._verify_output(output, writes, m)
        if not v["ok"]:
            raise writer.WriteRefused("output failed the acceptance contract: " + "; ".join(v["part_diff"]["problems"] + v["acceptance"]["problems"]))
        out_sha = acceptance.sha256_file(output)
        c.execute("update comp.write_operation set status = 'done', finished_at = now(), output_sha256 = %s where op_id = %s", (out_sha, op))
        c.commit()
        self.audit("comp_writer", "write_done", str(op), output_sha256=out_sha)
        return {"outcome": "written", "op_id": str(op), "output_sha256": out_sha, "verification": v}

    def _resume(self, c, pending, writes: dict, m: dict) -> dict:
        """Three states: the expected file is there (verify and finish), no file (regenerate), another file is there
        (stop and report a conflict; never overwrite what someone may have typed since)."""
        op, expected, path = pending
        output = Path(path)
        state = writer.resume_state(output, expected)
        if state == "expected":
            self.audit("comp_writer", "write_resumed", str(op), found="output saved with the expected hash; verified, not rewritten")
            r = self._finish(c, op, output, writes, m)
            r["outcome"] = "resumed_without_rewrite"
            return r
        if state == "missing":
            writer.patch(self.template, output, SHEET, writes, set(m.values()))
            self.audit("comp_writer", "write_resumed", str(op), found="no output file; regenerated from the template")
            r = self._finish(c, op, output, writes, m)
            r["outcome"] = "resumed_regenerated"
            return r
        found = acceptance.sha256_file(output)
        self.audit("comp_writer", "write_conflict", str(op), expected_sha256=expected, found_sha256=found,
                   decision="stopped; the file on disk was not overwritten")
        return {"outcome": "conflict_not_overwritten", "op_id": str(op), "expected_sha256": expected, "found_sha256": found}

    def approve(self, who: str) -> str:
        with self.conn("comp_reviewer") as c:
            status = c.execute("select status from comp.comp_record where id = %s", (self.comp_id,)).fetchone()[0]
            if status != "evidence_checked":
                raise RuntimeError("only a record whose evidence checked out can be approved")
            version, basis = self.version(c, self.comp_id, DESTINATION)
            c.execute("insert into comp.approval(comp_id, version_sha256, approved_by, destination) values (%s,%s,%s,%s)",
                      (self.comp_id, version, who, DESTINATION))
            c.execute("update comp.comp_record set status = 'approved_for_demo_entry', version_sha256 = %s where id = %s", (version, self.comp_id))
        self.audit("comp_reviewer", "approved_for_demo_entry", f"comp {self.comp_id}", approved_by=who, version_sha256=version,
                   appraiser_verification="pending")
        self.step("approve", "approved_for_demo_entry", approved_by=who, version_sha256=version, template_sha256=basis["template_sha256"],
                  destination=DESTINATION, appraiser_verification="pending")
        return version

    def change_after_approval_refused(self, output: Path) -> None:
        """Edit one value after approval, try to write, then put it back."""
        with self.conn("comp_reviewer") as c:
            entry = c.execute("select entry from comp.comp_record where id = %s", (self.comp_id,)).fetchone()[0]
            original = entry["sale_price"]["value"]
            entry["sale_price"]["value"] = 391000.0
            c.execute("update comp.comp_record set entry = %s where id = %s", (Jsonb(entry), self.comp_id))
        r = self.write(output)
        with self.conn("comp_reviewer") as c:
            entry["sale_price"]["value"] = original
            c.execute("update comp.comp_record set entry = %s where id = %s", (Jsonb(entry), self.comp_id))
        self.step("write", r["outcome"], what="sale price changed to 391,000 after approval", reason=r.get("reason"), file_created=output.exists())
