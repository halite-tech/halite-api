from __future__ import annotations

from backend.cost_engine.detectors.base import Result, detector, money, sample_rows
from backend.ingest.dataset import MissingData
from backend.shared.metrics import hourly_sales

CAT = "facilities"


@detector("utilities", CAT, "Utility spend above benchmark", ("utilities",))
def utilities(ds, bm):
    u = ds.require("utilities", "month", "amount")
    if not ds.profile.sqft:
        raise MissingData("restaurant square footage (set it in the profile)")
    months = u["month"].dt.to_period("M").nunique()
    annual_spend = float(u["amount"].sum()) * 12 / max(months, 1)
    per_sqft = annual_spend / ds.profile.sqft
    target = bm.v("utilities_per_sqft_year")
    annual = max(0.0, annual_spend - target * ds.profile.sqft)
    return Result(
        annual,
        f"Utilities run {money(annual_spend)}/yr ({money(per_sqft)} per sq ft) vs. a "
        f"{money(target)} per sq ft benchmark.",
        {"annual_spend": annual_spend, "per_sqft": per_sqft},
    )


@detector("equipment_downtime", CAT, "Equipment downtime", ("equipment", "sales"))
def equipment_downtime(ds, bm):
    e, days = ds.window("equipment", "date", "equipment", "downtime_hours")
    sales = hourly_sales(ds)
    avg_hourly = float(sales.mean()) if len(sales) else 0.0
    hours = float(e["downtime_hours"].fillna(0).sum())
    lost = hours * avg_hourly * bm.v("downtime_lost_sales_share")
    annual = ds.annualize(lost, days)
    worst = e.sort_values("downtime_hours", ascending=False)["equipment"].head(2).tolist()
    return Result(
        annual,
        f"{len(e)} breakdowns caused {hours:.0f} hours of downtime (worst: {', '.join(worst)}).",
        {"downtime_hours": hours, "events": len(e)},
        sample_rows(e),
    )
