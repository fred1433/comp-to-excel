"""Cut the cited lines out of the scanned council minutes and mark them, for the page.

Only the borough's own minutes are shown as images (a few lines each). Listing pages are quoted as text only.
Needs sources/private/minutes_2025-09-11.pdf (scripts/fetch_sources.py).
"""
import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "sources/private/minutes_2025-09-11.pdf"
LAYER = [json.loads((ROOT / "sources/private/layer_minutes.json").read_text())]
DPI = 200


def spans(page):
    L = next(x for x in LAYER if x["doc"] == "minutes")
    return {s["id"]: s for p in L["pages"] if p["page"] == page for s in p["spans"]}


def crop(page, mark, context, out, color):
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", str(DPI), "-f", str(page), "-l", str(page), "-png", str(PDF), f"{td}/p"], check=True)
        img = Image.open(next(Path(td).glob("p*.png"))).convert("RGB")
    k = DPI / 72
    S = spans(page)
    boxes = [S[i]["bbox"] for i in context]
    x0 = min(b[0] for b in boxes) - 14; y0 = min(b[1] for b in boxes) - 4
    x1 = max(b[2] for b in boxes) + 14; y1 = max(b[3] for b in boxes) + 5
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for i in mark:
        b = S[i]["bbox"]
        d.rectangle([b[0] * k - 4, b[1] * k - 3, b[2] * k + 4, b[3] * k + 3], fill=color)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    img = img.crop((int(x0 * k), int(y0 * k), int(x1 * k), int(y1 * k)))
    img.save(out, optimize=True)
    return {"file": out.name, "page": page, "marked": mark, "size": img.size}


if __name__ == "__main__":
    dist = ROOT / "site/dist/img"
    dist.mkdir(parents=True, exist_ok=True)
    meta = {
        "motion": crop(3, ["minutes:p3:l23", "minutes:p3:l24"], ["minutes:p3:l22", "minutes:p3:l23", "minutes:p3:l24", "minutes:p3:l25", "minutes:p3:l26"],
                       dist / "minutes-p3-motion.png", (255, 214, 64, 110)),
        "header": crop(1, ["minutes:p1:l3"], ["minutes:p1:l1", "minutes:p1:l2", "minutes:p1:l3"], dist / "minutes-p1-date.png", (214, 72, 56, 70)),
    }
    (ROOT / "site/src/crops.json").write_text(json.dumps(meta, indent=1))
    print(meta)
