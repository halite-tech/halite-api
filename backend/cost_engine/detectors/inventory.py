from __future__ import annotations

import pandas as pd

from backend.cost_engine.detectors.base import Result, detector, money, sample_rows
from backend.ingest.dataset import MissingData
from backend.shared.metrics import item_costs, sales_lines, sku_costs

CAT = "inventory_waste"


def _purchase_value(p: pd.DataFrame) -> pd.Series:
    return p["qty_received"].fillna(0) * p["unit_price"].fillna(0)


@detector("food_cost_variance", CAT, "Food cost variance (actual vs. theoretical)",
          ("sales", "recipes", "inventory", "purchases"))
def food_cost_variance(ds, bm):
    inv = ds.require("inventory", "count_date", "sku", "on_hand_qty")
    pur = ds.require("purchases", "invoice_date", "sku", "qty_received")
    rec = ds.require("recipes", "item", "sku", "qty_per_item")
    lines = sales_lines(ds)
    costs = sku_costs(ds)

    inv = inv.assign(d=inv["count_date"].dt.normalize())
    dates = sorted(inv["d"].dropna().unique())
    if len(dates) < 2:
        raise MissingData("need at least two inventory counts", ("inventory",))
    first, last = pd.Timestamp(dates[0]), pd.Timestamp(dates[-1])
    on_hand = inv.groupby(["d", "sku"])["on_hand_qty"].sum()
    begin, end = on_hand.loc[first], on_hand.loc[last]

    pd_dates = pur["invoice_date"].dt.normalize()
    bought = pur[(pd_dates > first) & (pd_dates <= last)].groupby("sku")["qty_received"].sum()
    actual_qty = begin.add(bought, fill_value=0).sub(end, fill_value=0)

    used = lines[(~lines["is_void"]) & (lines["date"] > first) & (lines["date"] <= last)]
    sold = used.groupby("item")["quantity"].sum().rename("sold")
    theo = rec.merge(sold, left_on="item", right_index=True)
    theo_qty = (theo["qty_per_item"] * theo["sold"]).groupby(theo["sku"]).sum()

    skus = actual_qty.index.union(theo_qty.index)
    c = costs.reindex(skus).fillna(0)
    actual_val = actual_qty.reindex(skus).fillna(0) * c
    theo_val = theo_qty.reindex(skus).fillna(0) * c
    variance = float(actual_val.sum() - theo_val.sum())
    allowed = float(theo_val.sum()) * bm.v("allowed_food_variance_pct")
    days = (last - first).days
    annual = ds.annualize(max(0.0, variance - allowed), days)

    by_sku = (actual_val - theo_val).sort_values(ascending=False)
    top = ", ".join(f"{s} {money(v)}" for s, v in by_sku.head(3).items() if v > 0)
    pct = variance / theo_val.sum() * 100 if theo_val.sum() else 0
    return Result(
        annual,
        f"Actual food usage ran {pct:.1f}% above what recipes say you should have used "
        f"(allowed: {bm.v('allowed_food_variance_pct') * 100:.0f}%). Biggest gaps: {top}.",
        {"actual_cost": float(actual_val.sum()), "theoretical_cost": float(theo_val.sum()),
         "variance_pct": pct, "days": days},
        sample_rows(inv[inv["d"].isin([first, last])]),
    )


@detector("waste", CAT, "Waste and spoilage above target", ("waste", "purchases"))
def waste(ds, bm):
    w, wdays = ds.window("waste", "waste_date", "sku", "quantity")
    p, pdays = ds.window("purchases", "qty_received", "unit_price")
    if "waste_cost" in w.columns and w["waste_cost"].notna().any():
        value = w["waste_cost"].fillna(0)
    else:
        value = w["quantity"].fillna(0) * w["sku"].map(sku_costs(ds)).fillna(0)
    waste_annual = ds.annualize(value.sum(), wdays)
    purchases_annual = ds.annualize(_purchase_value(p).sum(), pdays)
    if purchases_annual <= 0:
        raise MissingData("purchases have no value", ("purchases",))
    target = bm.v("waste_pct_of_purchases")
    rate = waste_annual / purchases_annual
    annual = max(0.0, waste_annual - target * purchases_annual)
    top = value.groupby(w["sku"]).sum().sort_values(ascending=False).head(3)
    return Result(
        annual,
        f"Logged waste is {rate * 100:.1f}% of food purchases vs. a {target * 100:.0f}% target "
        f"({money(waste_annual)}/yr wasted). Most wasted: {', '.join(top.index)}.",
        {"waste_annual": waste_annual, "purchases_annual": purchases_annual, "waste_rate": rate},
        sample_rows(w.loc[value.sort_values(ascending=False).index]),
    )


@detector("overstock", CAT, "Cash tied up in excess inventory", ("inventory", "purchases"))
def overstock(ds, bm):
    inv = ds.require("inventory", "count_date", "sku", "on_hand_qty")
    p, pdays = ds.window("purchases", "qty_received", "unit_price")
    costs = sku_costs(ds)
    value = inv["on_hand_qty"].fillna(0) * inv["sku"].map(costs).fillna(0)
    avg_inventory = float(value.groupby(inv["count_date"].dt.normalize()).sum().mean())
    daily_usage = float(_purchase_value(p).sum()) / pdays
    if daily_usage <= 0:
        raise MissingData("purchases have no value", ("purchases",))
    doh = avg_inventory / daily_usage
    target = bm.v("target_days_on_hand")
    excess_value = max(0.0, avg_inventory - target * daily_usage)
    annual = excess_value * bm.v("carrying_cost_rate")
    return Result(
        annual,
        f"You hold {doh:.1f} days of inventory on average vs. a {target:.0f}-day target "
        f"({money(excess_value)} of excess stock).",
        {"days_on_hand": doh, "avg_inventory_value": avg_inventory, "excess_value": excess_value},
    )


@detector("stockouts", CAT, "Sales lost to 86'd items", ("stockouts", "sales"))
def stockouts(ds, bm):
    so, days = ds.window("stockouts", "date", "item", "hours_unavailable")
    lines = sales_lines(ds)
    paid = lines[lines["net"] > 0]
    open_hours = paid["timestamp"].dt.floor("h").nunique()
    if open_hours == 0:
        raise MissingData("no paid sales", ("sales",))
    per_hour = paid.groupby("item")["quantity"].sum() / open_hours
    price = paid.groupby("item")["unit_price"].mean()
    try:
        food = item_costs(ds)
    except MissingData:
        food = price * bm.v("fallback_food_cost_pct")
    margin = (price - food.reindex(price.index).fillna(price * bm.v("fallback_food_cost_pct"))).clip(lower=0)
    lost_qty = so["hours_unavailable"].fillna(0) * so["item"].map(per_hour).fillna(0)
    lost_qty *= 1 - bm.v("stockout_substitution_rate")
    lost = float((lost_qty * so["item"].map(margin).fillna(0)).sum())
    annual = ds.annualize(lost, days)
    return Result(
        annual,
        f"{len(so)} stockouts totalling {so['hours_unavailable'].sum():.0f} hours cost about "
        f"{money(lost)} in lost margin over the period.",
        {"events": len(so), "hours": float(so["hours_unavailable"].sum())},
        sample_rows(so),
    )
