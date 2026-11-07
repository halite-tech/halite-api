from __future__ import annotations

import pandas as pd

from backend.cost_engine.detectors.base import Result, detector, money, sample_rows
from backend.ingest.dataset import MissingData

CAT = "purchasing_suppliers"


def _emergency_mask(p: pd.DataFrame) -> pd.Series:
    if "is_emergency" in p.columns:
        return p["is_emergency"].fillna(False).astype(bool)
    return pd.Series(False, index=p.index)


def reference_prices(p: pd.DataFrame) -> pd.Series:
    """Per-SKU reference price: the median of the cheapest quarter of regular
    (non-emergency) purchases. Robust to a single unusually cheap invoice."""
    regular = p[~_emergency_mask(p)].dropna(subset=["sku", "unit_price"])

    def ref(prices: pd.Series) -> float:
        return float(prices[prices <= prices.quantile(0.25)].median())

    return regular.groupby("sku")["unit_price"].apply(ref)


def _billed(p: pd.DataFrame) -> pd.Series:
    if "qty_billed" in p.columns and p["qty_billed"].notna().any():
        return p["qty_billed"].fillna(p["qty_received"])
    return p["qty_received"]


@detector("price_creep", CAT, "Supplier price creep", ("purchases",))
def price_creep(ds, bm):
    p, days = ds.window("purchases", "sku", "unit_price", "qty_received", "vendor")
    # Emergency buys are priced separately by the late_deliveries detector.
    p = p[~_emergency_mask(p)]
    ref = p["sku"].map(reference_prices(p))
    tol = bm.v("price_creep_tolerance")
    over = p["unit_price"] > ref * (1 + tol)
    excess = ((p["unit_price"] - ref) * _billed(p)).where(over, 0.0)
    annual = ds.annualize(excess.sum(), days)

    by_sku = excess.groupby(p["sku"]).sum().sort_values(ascending=False)
    details = []
    for sku in by_sku[by_sku > 0].head(3).index:
        rows = p[(p["sku"] == sku) & over]
        rise = (rows["unit_price"].mean() / ref[rows.index].mean() - 1) * 100
        vendor = rows["vendor"].mode().iat[0]
        details.append(f"{sku} from {vendor} (+{rise:.0f}%)")
    return Result(
        annual,
        "Paying above your own best recent price on " + (", ".join(details) or "no SKUs") + ".",
        {"skus_affected": int((by_sku > 0).sum())},
        sample_rows(p[over]),
    )


@detector("short_deliveries", CAT, "Short deliveries billed in full", ("purchases",))
def short_deliveries(ds, bm):
    p, days = ds.window("purchases", "qty_billed", "qty_received", "unit_price")
    short_qty = (p["qty_billed"] - p["qty_received"]).clip(lower=0)
    value = short_qty * p["unit_price"]
    annual = ds.annualize(value.sum(), days)
    n = int((short_qty > 0).sum())
    return Result(
        annual,
        f"{n} invoice lines billed for more than was received ({money(value.sum())} over the period).",
        {"short_lines": n},
        sample_rows(p[short_qty > 0]),
    )


@detector("late_deliveries", CAT, "Late deliveries and emergency buys", ("purchases", "vendor_deliveries"))
def late_deliveries(ds, bm):
    p, days = ds.window("purchases", "sku", "unit_price", "qty_received", "is_emergency")
    d = ds.require("vendor_deliveries", "vendor", "promised_at", "delivered_at")
    late = (d["delivered_at"] - d["promised_at"]).dt.total_seconds() / 3600 > bm.v("late_delivery_grace_hours")
    emerg = p[_emergency_mask(p)]
    ref = emerg["sku"].map(reference_prices(p))
    premium_rate = bm.v("emergency_purchase_premium")
    premium = ((emerg["unit_price"] - ref) * emerg["qty_received"]).where(
        ref.notna(), emerg["unit_price"] * emerg["qty_received"] * premium_rate / (1 + premium_rate)
    ).clip(lower=0)
    annual = ds.annualize(premium.sum(), days)
    return Result(
        annual,
        f"{int(late.sum())} of {len(d)} vendor deliveries arrived late; {len(emerg)} emergency "
        f"purchases cost {money(premium.sum())} more than regular prices.",
        {"late_deliveries": int(late.sum()), "deliveries": len(d), "emergency_purchases": len(emerg),
         "late_rate": float(late.mean()) if len(d) else 0.0},
        sample_rows(emerg),
    )
