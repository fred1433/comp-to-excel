"""Independent recomputation of the grid, written from the adjustment convention, not from the workbook formulas.

Used to check, to the cent, the values Excel computes. `None` means blank: a blank input never becomes zero.
"""
from __future__ import annotations

SQFT_PER_ACRE = 43560


def column(price, gba, acres, transactional=(), property_adj=()):
    """transactional: list of (kind, value) in order, kind in {"pct", "usd"}; property_adj: list of pct values."""
    out = {"price_per_sf": None, "land_to_building": None, "steps": [], "net_property": None,
           "adjusted_per_sf": None, "gross_pct": None, "net_pct": None}
    if price is not None and gba:
        out["price_per_sf"] = price / gba
    if acres is not None and gba:
        out["land_to_building"] = acres * SQFT_PER_ACRE / gba
    running = out["price_per_sf"]
    gross = 0.0
    for kind, v in transactional:
        if running is None or v is None:
            running = None
        else:
            gross += abs(v) * running if kind == "pct" else abs(v)
            running = running * (1 + v) if kind == "pct" else running + v
        out["steps"].append(running)
    if property_adj and all(v is not None for v in property_adj):
        out["net_property"] = sum(property_adj)
    if running is not None and out["net_property"] is not None and len(out["steps"]) == 5:
        out["adjusted_per_sf"] = running * (1 + out["net_property"])
        gross += sum(abs(v) for v in property_adj) * running
        out["gross_pct"] = gross / out["price_per_sf"]
        out["net_pct"] = out["adjusted_per_sf"] / out["price_per_sf"] - 1
    return out
