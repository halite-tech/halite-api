"""Derived metrics shared by detectors. Results are cached on the dataset."""
from __future__ import annotations

import pandas as pd

from backend.ingest.dataset import Dataset, MissingData


def _cached(ds: Dataset, key: str, fn):
    if key not in ds.cache:
        ds.cache[key] = fn()
    return ds.cache[key]


def sales_lines(ds: Dataset) -> pd.DataFrame:
    """Sales line items with gross, discount, and net revenue per line.

    Voided lines are neither revenue nor food usage. Comped lines are not
    revenue but the food was still used.
    """
    def build():
        df = ds.require("sales", "order_id", "timestamp", "item", "quantity", "unit_price").copy()
        for col in ("is_void", "is_comp"):
            df[col] = df[col].fillna(False).astype(bool) if col in df.columns else False
        df["discount"] = df["discount"].fillna(0.0) if "discount" in df.columns else 0.0
        df["gross"] = df["quantity"].fillna(0) * df["unit_price"].fillna(0)
        lost = df["is_void"] | df["is_comp"]
        df["net"] = (df["gross"] - df["discount"]).where(~lost, 0.0)
        df["date"] = df["timestamp"].dt.normalize()
        return df
    return _cached(ds, "sales_lines", build)


def annual_revenue(ds: Dataset) -> float | None:
    if not ds.has("sales", "timestamp", "quantity", "unit_price"):
        return None
    lines = sales_lines(ds)
    df, days = ds.window("sales")
    return ds.annualize(lines.loc[df.index, "net"].sum(), days)


def avg_check(ds: Dataset) -> float:
    lines = sales_lines(ds)
    per_order = lines.groupby("order_id")["net"].sum()
    per_order = per_order[per_order > 0]
    if per_order.empty:
        raise MissingData("no paid orders in sales data", ("sales",))
    return float(per_order.mean())


def sku_costs(ds: Dataset) -> pd.Series:
    """Cost per base unit for each SKU, from regular (non-emergency) purchases,
    falling back to inventory unit costs."""
    def build():
        costs = pd.Series(dtype=float)
        if ds.has("purchases", "sku", "qty_received", "unit_price"):
            p = ds.tables["purchases"]
            if "is_emergency" in p.columns:
                p = p[~p["is_emergency"].fillna(False).astype(bool)]
            p = p.dropna(subset=["sku", "unit_price"])
            qty = p["qty_received"].fillna(0).clip(lower=0)
            spend = (qty * p["unit_price"]).groupby(p["sku"]).sum()
            units = qty.groupby(p["sku"]).sum()
            costs = (spend / units.where(units > 0)).dropna()
        if ds.has("inventory", "sku", "unit_cost"):
            inv = ds.tables["inventory"].dropna(subset=["sku", "unit_cost"])
            inv_costs = inv.groupby("sku")["unit_cost"].mean()
            costs = costs.combine_first(inv_costs)
        if costs.empty:
            raise MissingData("no ingredient costs (upload invoices or inventory with unit cost)",
                              ("purchases",))
        return costs
    return _cached(ds, "sku_costs", build)


def item_costs(ds: Dataset) -> pd.Series:
    """Theoretical food cost per menu item from recipes."""
    def build():
        rec = ds.require("recipes", "item", "sku", "qty_per_item")
        costs = sku_costs(ds)
        line_cost = rec["qty_per_item"] * rec["sku"].map(costs)
        return line_cost.groupby(rec["item"]).sum(min_count=1).dropna()
    return _cached(ds, "item_costs", build)


def hourly_labor(ds: Dataset) -> pd.DataFrame:
    """Timeclock punches split into clock-hour buckets: hour, employee_id, hours, wage."""
    def build():
        tc = ds.require("timeclock", "employee_id", "clock_in", "clock_out", "wage")
        tc = tc.dropna(subset=["clock_in", "clock_out"])
        tc = tc[tc["clock_out"] > tc["clock_in"]]
        rows = []
        one_hour = pd.Timedelta(hours=1)
        for emp, start, end, wage in zip(tc["employee_id"], tc["clock_in"], tc["clock_out"], tc["wage"]):
            h = start.floor("h")
            while h < end:
                nxt = h + one_hour
                frac = (min(end, nxt) - max(start, h)).total_seconds() / 3600
                rows.append((h, emp, frac, wage))
                h = nxt
        return pd.DataFrame(rows, columns=["hour", "employee_id", "hours", "wage"])
    return _cached(ds, "hourly_labor", build)


def hourly_sales(ds: Dataset) -> pd.Series:
    def build():
        lines = sales_lines(ds)
        return lines.groupby(lines["timestamp"].dt.floor("h"))["net"].sum()
    return _cached(ds, "hourly_sales", build)


def weeks_in(days: int) -> float:
    return max(days, 1) / 7
