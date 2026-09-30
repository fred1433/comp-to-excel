"""Page text layers with line-level spans and coordinates.

Text PDFs: poppler's `pdftotext -bbox-layout`. Scanned PDFs: `pdftoppm` at 300 dpi, then Tesseract TSV.
Every line becomes a span with a stable id `<doc>:p<page>:l<line>` and a box in PDF points
(origin top-left), so a citation can be checked against the text and highlighted on the page.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

DPI = 300


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(Path(p).read_bytes())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def text_pdf_layer(pdf: Path, doc: str) -> list[dict]:
    out = subprocess.run(["pdftotext", "-bbox-layout", str(pdf), "-"], capture_output=True, check=True).stdout
    xml = re.sub(rb"<!DOCTYPE[^>]*>", b"", out)
    root = ET.fromstring(xml)
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    pages = []
    for pn, page in enumerate(root.iter("{http://www.w3.org/1999/xhtml}page"), start=1):
        spans = []
        for line in page.iter("{http://www.w3.org/1999/xhtml}line"):
            words = line.findall("x:word", ns)
            text = _norm(" ".join((w.text or "") for w in words))
            if not text:
                continue
            spans.append({
                "id": f"{doc}:p{pn}:l{len(spans) + 1}",
                "text": text,
                "bbox": [round(float(line.get(k)), 1) for k in ("xMin", "yMin", "xMax", "yMax")],
            })
        pages.append({"page": pn, "width": float(page.get("width")), "height": float(page.get("height")), "spans": spans})
    return pages


def scanned_pdf_layer(pdf: Path, doc: str) -> list[dict]:
    pages = []
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", str(DPI), "-gray", "-png", str(pdf), f"{td}/pg"], check=True)
        pngs = sorted(Path(td).glob("pg-*.png"), key=lambda p: int(p.stem.split("-")[1]))
        for pn, png in enumerate(pngs, start=1):
            tsv = subprocess.run(["tesseract", str(png), "stdout", "--psm", "4", "tsv"], capture_output=True, check=True).stdout.decode()
            rows = list(csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE))
            lines: dict[tuple, dict] = {}
            width = height = None
            for r in rows:
                if r["level"] == "1":
                    width, height = int(r["width"]), int(r["height"])
                if r["level"] != "5" or not (r["text"] or "").strip():
                    continue
                key = (r["block_num"], r["par_num"], r["line_num"])
                x, y, w, h = (int(r[k]) for k in ("left", "top", "width", "height"))
                ln = lines.setdefault(key, {"words": [], "box": [x, y, x + w, y + h]})
                ln["words"].append(r["text"])
                b = ln["box"]
                ln["box"] = [min(b[0], x), min(b[1], y), max(b[2], x + w), max(b[3], y + h)]
            k = 72.0 / DPI
            spans = []
            for key in sorted(lines, key=lambda t: (lines[t]["box"][1], lines[t]["box"][0])):
                ln = lines[key]
                text = _norm(" ".join(ln["words"]))
                if not text:
                    continue
                spans.append({
                    "id": f"{doc}:p{pn}:l{len(spans) + 1}",
                    "text": text,
                    "bbox": [round(v * k, 1) for v in ln["box"]],
                })
            pages.append({"page": pn, "width": round(width * k, 1), "height": round(height * k, 1), "spans": spans})
    return pages


def is_scanned(pdf: Path) -> bool:
    fonts = subprocess.run(["pdffonts", str(pdf)], capture_output=True, check=True).stdout.decode().splitlines()
    return len(fonts) <= 2


def build_layer(pdf: Path, doc: str) -> dict:
    scanned = is_scanned(pdf)
    pages = scanned_pdf_layer(pdf, doc) if scanned else text_pdf_layer(pdf, doc)
    layer = {"doc": doc, "file_sha256": sha256_file(pdf), "text_source": "tesseract-ocr" if scanned else "pdf-text", "pages": pages}
    layer["layer_sha256"] = sha256_bytes(json.dumps(pages, sort_keys=True).encode())
    return layer
