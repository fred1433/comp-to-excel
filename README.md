# comp-to-excel

A public-data demonstration of controlled sale entry: one recorded Pennsylvania sale (3540 St. Lawrence Ave,
Reading, PA, instrument 2025031513) goes from its source documents, through staging in PostgreSQL and a human
approval, into an Excel sales comparison grid, and the workbook's formulas are shown not to change.

Page: https://theaipipe.com/comp-to-excel/

## What it does

- Reads four documents: the Berks County assessment record (county open data), the borough council minutes
  (a scanned PDF, read by Tesseract OCR) and two listing pages for MLS PABK2052516 (saved as PDF).
- Claude Sonnet 5 proposes facts with the lines they come from (one recorded call, `fixtures/model_outputs/`).
  Code then checks each one: the quote must be on the cited line, the line must say what the value means
  (an asking price is not a sale price), the value must be what the quote says. 46 pass, 4 are held back.
- Stages everything in PostgreSQL with separate roles for reading, reviewing and writing, a unique recorded
  instrument per transaction, row-level security, and a hash-chained audit log that the application roles
  cannot update, delete or truncate.
- Writes the approved values into mapped input cells of the sheet XML only. The approval is bound to a version
  (record, evidence, input map, template, destination); any change afterwards refuses the write. An interrupted
  write is resumed without writing twice.
- Proves the workbook: every package part byte-identical except the grid sheet, only approved cells changed,
  and a fingerprint of formulas, defined names, conditional formats, validations, calculation settings, styles
  and constants identical before and after. A hand-edited formula, a repointed name and a changed constant are
  each detected. Excel for Mac recalculated the delivered file; values agree with an independent recomputation.

## Run it on a clean clone

Needs Python 3.12 and PostgreSQL 14 or later (`initdb` and `pg_ctl` on the PATH; a throwaway cluster is created
in a temporary directory and stopped afterwards). No API key, no network.

    python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python -m pytest -q          # 23 tests
    .venv/bin/python run_demo.py --no-excel  # the whole run; writes run/ and workbooks/

`run/run.json` and `run/audit.jsonl` are the recorded run shown on the page (with the Excel for Mac observation).
`scripts/fetch_sources.py` re-fetches the documents into `sources/private/` to rebuild text layers and crops.

## What it does not show

- Microsoft tenant integration: documents were read from a local folder.
- Desktop Excel on Windows: Excel for Mac 16.113 was used, not activated, so it recalculated the file and could
  not save it. The delivered file is the written file, with the same SHA-256 Excel recalculated.
- Verification with a party to the sale, or the deed image (the instrument number comes from the county record).
- A firm's own model: the grid here is ours, with one adjustment convention among several.

## Sources and reuse

No source document is stored here: `sources/manifest.json` gives URLs, dates and SHA-256; `fixtures/` holds
only the cited lines. Information is based on geospatial data from the County of Berks. Listing facts are quoted
from MLS PABK2052516 as shown on coldwellbankerhomes.com and coalitionpg.com. `tests/fixtures/vbaProject.bin` is
the XlsxWriter sample macro project (BSD 2-Clause), used only to check that a macro project survives a write.
