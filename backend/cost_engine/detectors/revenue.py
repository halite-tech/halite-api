from __future__ import annotations

from backend.cost_engine.detectors.base import Result, detector, money, sample_rows
from backend.ingest.dataset import MissingData
from backend.shared.metrics import item_costs, sales_lines

CAT = "pos_leakage"


@detector("pos_leakage", CAT, "Voids, comps, and discounts", ("sales",))
def pos_leakage(ds, bm):
    df, days = ds.window("sales")
    lines = sales_lines(ds).loc[df.index]
    gross = float(lines["gross"].sum())
    if gross <= 0:
        raise MissingData("no gross sales", ("sales",))
    voids = float(lines.loc[lines["is_void"], "gross"].sum())
    comps = float(lines.loc[lines["is_comp"], "gross"].sum())
    discounts = float(lines.loc[~(lines["is_void"] | lines["is_comp"]), "discount"].sum())
    leakage = voids + comps + discounts
    target = bm.v("pos_leakage_pct")
    annual = ds.annualize(max(0.0, leakage - target * gross), days)
    return Result(
        annual,
        f"Voids, comps, and discounts are {leakage / gross * 100:.1f}% of gross sales vs. a "
        f"{target * 100:.1f}% target (voids {money(voids)}, comps {money(comps)}, "
        f"discounts {money(discounts)}).",
        {"leakage_pct": leakage / gross, "voids": voids, "comps": comps, "discounts": discounts},
        sample_rows(lines[lines["is_void"] | lines["is_comp"]]),
    )


@detector("menu_mix", CAT, "Underpriced menu items", ("sales", "recipes"), confidence=0.6)
def menu_mix(ds, bm):
    df, days = ds.window("sales")
    lines = sales_lines(ds).loc[df.index]
    paid = lines[lines["net"] > 0]
    revenue = paid.groupby("item")["net"].sum()
    price = paid.groupby("item")["unit_price"].mean()
    food = item_costs(ds).reindex(price.index)
    fc_pct = (food / price).dropna()
    target = bm.v("target_item_food_cost_pct")
    flagged = fc_pct[fc_pct > target]
    lost = float(((flagged - target) * revenue.reindex(flagged.index)).sum())
    annual = ds.annualize(lost, days)
    detail = ", ".join(f"{i} ({p * 100:.0f}% food cost)" for i, p in flagged.sort_values(ascending=False).head(3).items())
    return Result(
        annual,
        f"{len(flagged)} items run above a {target * 100:.0f}% food cost target: {detail or 'none'}.",
        {"flagged_items": flagged.round(3).to_dict()},
    )


@detector("cash_shrink", CAT, "Cash drawer shortages", ("cash",))
def cash_shrink(ds, bm):
    c, days = ds.window("cash", "date", "expected", "counted")
    shortage = float((c["expected"] - c["counted"]).sum())
    allowance = float(c["expected"].sum()) * bm.v("cash_variance_allowance_pct")
    annual = ds.annualize(max(0.0, shortage - allowance), days)
    short_days = int((c["expected"] - c["counted"] > 0).sum())
    return Result(
        annual,
        f"Drawers came up short on {short_days} of {len(c)} days, a net {money(shortage)} over the period.",
        {"net_shortage": shortage, "short_days": short_days},
        sample_rows(c[c["expected"] - c["counted"] > 0]),
    )
