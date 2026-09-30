"""Recalculate a workbook in Microsoft Excel for Mac and read cells back (AppleScript).

Excel for Mac is sandboxed: the workbook is copied into Excel's own container first, opened there, recalculated in
full, read, and closed without saving. On the machine of the recorded run Excel is not activated, so it can open and
recalculate but cannot save; the file that is delivered is therefore the written file itself, and Excel is shown
to have recalculated that exact file (same SHA-256).
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).with_name("excel_recalc.applescript")
CONTAINER = Path.home() / "Library/Containers/com.microsoft.Excel/Data/tmp/comp-to-excel"
APP = Path("/Applications/Microsoft Excel.app")


def available() -> bool:
    return APP.exists() and shutil.which("osascript") is not None


def _number(text: str):
    t = text.strip()
    if t == "":
        return ""
    try:
        return float(t.replace(",", "."))
    except ValueError:
        return t


def observe(workbook: Path, refs: list[str]) -> dict:
    CONTAINER.mkdir(parents=True, exist_ok=True)
    local = CONTAINER / f"observe-{hashlib.sha256(workbook.read_bytes()).hexdigest()[:12]}.xlsx"
    shutil.copyfile(workbook, local)
    was_running = subprocess.run(["pgrep", "-f", "MacOS/Microsoft Excel"], capture_output=True).returncode == 0
    try:
        out = subprocess.run(["osascript", str(SCRIPT), str(local), *refs], capture_output=True, text=True, timeout=300, check=True).stdout
    finally:
        if not was_running:
            subprocess.run(["osascript", "-e", 'tell application "Microsoft Excel" to quit saving no'], capture_output=True)
    lines = out.strip("\n").split("\n")
    version = lines[0].split("\t", 1)[1]
    cells = {}
    for ln in lines[1:]:
        ref, klass, value, formula = (ln.split("\t") + ["", "", ""])[:4]
        cells[ref] = {"class": klass, "value": _number(value) if klass in ("real", "integer", "double") else value, "formula": formula}
    return {"application": "Microsoft Excel for Mac", "version": version, "workbook_sha256": hashlib.sha256(local.read_bytes()).hexdigest(),
            "recalculation": "calculate full", "saved": False, "cells": cells}
