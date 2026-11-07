from __future__ import annotations

import numpy as np
import pandas as pd

from backend.cost_engine.detectors.base import Result, detector, money, sample_rows
from backend.ingest.dataset import MissingData

CAT = "hiring_retention"


def _period(ds):
    if ds.period_start is None:
        raise MissingData("cannot determine the analysis period (upload sales data)", ("sales",))
    return ds.period_start.normalize(), ds.period_end.normalize()


@detector("turnover", CAT, "Staff turnover", ("employees",))
def turnover(ds, bm):
    emp = ds.require("employees", "employee_id", "hire_date")
    start, end = _period(ds)
    days = pd.date_range(start, end, freq="D").to_numpy()
    hire = emp["hire_date"].to_numpy()[:, None]
    term = (emp["termination_date"] if "termination_date" in emp.columns
            else pd.Series(pd.NaT, index=emp.index)).to_numpy()[:, None]
    active = (hire <= days) & (np.isnat(term) | (term > days))
    avg_headcount = float(active.sum(axis=0).mean())
    if avg_headcount <= 0:
        raise MissingData("no active employees in the analysis period", ("employees",))

    term_s = emp.get("termination_date", pd.Series(pd.NaT, index=emp.index))
    separated = emp[(term_s >= start) & (term_s <= end)]
    annual_seps = ds.annualize(len(separated), ds.period_days)
    rate = annual_seps / avg_headcount
    target = bm.v("target_turnover_rate")

    wage = None
    if "wage" in separated.columns and separated["wage"].notna().any():
        wage = float(separated["wage"].mean())
    elif ds.has("timeclock", "employee_id", "wage"):
        tc = ds.tables["timeclock"]
        w = tc[tc["employee_id"].isin(separated["employee_id"])]["wage"]
        wage = float(w.mean()) if w.notna().any() else float(tc["wage"].mean())
    wage = wage if wage and not np.isnan(wage) else bm.v("fallback_hourly_wage")

    replacement = (bm.v("recruiting_cost_per_hire") + bm.v("training_hours_per_hire") * wage
                   + bm.v("ramp_productivity_loss_per_hire"))
    excess_seps = max(0.0, (rate - target) * avg_headcount)
    annual = excess_seps * replacement
    roles = separated["role"].value_counts().head(2).index.tolist() if "role" in separated.columns else []
    return Result(
        annual,
        f"Annual turnover is {rate * 100:.0f}% vs. a {target * 100:.0f}% target "
        f"({len(separated)} departures in {ds.period_days} days, mostly {', '.join(roles) or 'staff'}). "
        f"Each replacement costs about {money(replacement)}.",
        {"turnover_rate": rate, "avg_headcount": avg_headcount, "separations": len(separated),
         "replacement_cost": replacement, "excess_separations_annual": excess_seps},
        sample_rows(separated),
    )


@detector("unfilled_roles", CAT, "Roles left unfilled", ("requisitions",))
def unfilled_roles(ds, bm):
    rq = ds.require("requisitions", "req_id", "opened_date")
    start, end = _period(ds)
    rq = rq[(rq["opened_date"] >= start) & (rq["opened_date"] <= end)]
    filled = rq["filled_date"] if "filled_date" in rq.columns else pd.Series(pd.NaT, index=rq.index)
    days_open = (filled.fillna(end) - rq["opened_date"]).dt.days.clip(lower=0)
    target = bm.v("target_days_to_fill")
    excess_days = float((days_open - target).clip(lower=0).sum())
    annual = ds.annualize(excess_days * bm.v("coverage_cost_per_open_day"), ds.period_days)
    return Result(
        annual,
        f"Roles took {days_open.mean():.0f} days to fill on average vs. a {target:.0f}-day target "
        f"({int(filled.isna().sum())} still open).",
        {"avg_days_to_fill": float(days_open.mean()) if len(days_open) else 0.0,
         "requisitions": len(rq), "excess_open_days": excess_days},
        sample_rows(rq),
    )
