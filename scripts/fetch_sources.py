"""Re-fetch the source documents into sources/private/ (git-ignored) and rebuild their text layers.

The listing pages are saved as PDF through a local Chrome DevTools endpoint (scripts/cdp_print.py), the way an
appraiser saves an MLS sheet into a job folder; the two sites refuse headless browsers. Pages change over time,
so a re-fetch can differ from the recorded run: compare SHA-256 with sources/manifest.json.
"""
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from comptrace.county import fetch, facts_from_records  # noqa: E402
from comptrace.textlayer import build_layer, sha256_file  # noqa: E402

PRIVATE = ROOT / "sources" / "private"
MANIFEST = json.loads((ROOT / "sources" / "manifest.json").read_text())


def main(with_listings: bool):
    PRIVATE.mkdir(parents=True, exist_ok=True)
    for doc, meta in MANIFEST["documents"].items():
        target = PRIVATE / meta["local_file"]
        if doc == "county":
            rec = fetch(meta["parcel"])
            target.write_text(json.dumps(rec, indent=1))
            continue
        if meta["kind"] == "listing page saved as PDF":
            if not with_listings:
                continue
            subprocess.run([sys.executable, str(ROOT / "scripts" / "cdp_print.py"), meta["url"], str(target)], check=True)
        else:
            req = urllib.request.Request(meta["url"], headers={"User-Agent": "Mozilla/5.0"})
            target.write_bytes(urllib.request.urlopen(req, timeout=60).read())
        layer = build_layer(target, doc)
        (PRIVATE / f"layer_{doc}.json").write_text(json.dumps(layer, indent=1))
        same = layer["file_sha256"] == meta["sha256"]
        print(f"{doc}: {'same file as the recorded run' if same else 'DIFFERENT from the recorded run'} ({layer['file_sha256'][:12]})")


if __name__ == "__main__":
    main("--listings" in sys.argv)
