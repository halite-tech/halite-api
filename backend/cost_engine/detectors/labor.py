from __future__ import annotations

import math

import pandas as pd

from backend.cost_engine.detectors.base import Result, detector, money, sample_rows
from backend.ingest.dataset import Dataset, MissingData
from backend.shared.benchmarks import Benchmarks
from backend.shared.metrics import avg_check, hourly_labor, hourly_sales, sales_lines

CAT = "labor_scheduling"
DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKS_PER_YEAR = 52


def _hour_label(h: int) -> str:
    return f"{(h % 12) or 12}{'am' if h < 12 else 'pm'}"


def staffing_slots(ds: Dataset, bm: Benchmarks) -> pd.DataFrame:
    """Average staffed vs. required headcount for each weekday-hour slot.

    Required headcount = max(min staff, ceil(average sales / target SPLH)).
    Every slot occurs once a week, so excess headcount = excess hours per week.
    """
    lab = hourly_labor(ds)
    if lab.empty:
        raise MissingData("timeclock has no valid punches", ("timeclock",))
    by_hour = lab.assign(cost=lab["hours"] * lab["wage"]).groupby("hour").agg(
        staffed=("hours", "sum"), cost=("cost", "sum"))
    by_hour["sales"] = hourly_sales(ds).reindex(by_hour.index).fillna(0.0)
    by_hour["dow"] = by_hour.index.day_of_week
    by_hour["hod"] = by_hour.index.hour
    slots = by_hour.groupby(["dow", "hod"]).agg(
        staffed=("staffed", "mean"), sales=("sales", "mean"),
        cost=("cost", "sum"), hours=("staffed", "sum"))
    slots["wage"] = slots["cost"] / slots["hours"]
    splh = bm.v("target_splh")
    min_staff = bm.v("min_staff_per_hour")
    slots["required"] = slots["sales"].map(lambda s: max(min_staff, math.ceil(s / splh - 1e-9)))
    slots["excess"] = (slots["staffed"] - slots["required"]).clip(lower=0)
    return slots


@detector("overstaffing", CAT, "Overstaffing vs. demand", ("sales", "timeclock"))
def overstaffing(ds, bm):
    slots = staffing_slots(ds, bm)
    weekly_cost = float((slots["excess"] * slots["wage"]).sum())
    excess_hours_week = float(slots["excess"].sum())
    annual = weekly_cost * WEEKS_PER_YEAR

    worst = (slots["excess"] * slots["wage"]).sort_values(ascending=False)
    worst = worst[worst > 0].head(4)
    spots = ", ".join(
        f"{DOW[d]} {_hour_label(h)} (+{slots.loc[(d, h), 'excess']:.1f} staff)" for d, h in worst.index)
    share = excess_hours_week / slots["staffed"].sum() * 100 if slots["staffed"].sum() else 0
    return Result(
        annual,
        f"{excess_hours_week:.0f} labor hours a week ({share:.0f}%) are scheduled beyond what sales "
        f"need at {money(bm.v('target_splh'))} sales per labor hour. Worst slots: {spots or 'none'}.",
        {"excess_hours_per_week": excess_hours_week,
         "excess_hours_annual": excess_hours_week * WEEKS_PER_YEAR,
         "worst_slots": [f"{DOW[d]} {_hour_label(h)}" for d, h in worst.index]},
    )


@detector("understaffing", CAT, "Slow service from understaffing", ("sales",), confidence=0.5)
def understaffing(ds, bm):
    df, days = ds.window("sales", "order_id", "kitchen_minutes")
    lines = sales_lines(ds).loc[df.index]
    orders = lines.groupby("order_id").agg(kitchen=("kitchen_minutes", "max"), net=("net", "sum"))
    slow = orders["kitchen"] > bm.v("slow_ticket_minutes")
    lost = float(slow.sum()) * avg_check(ds) * bm.v("walkaway_rate")
    annual = ds.annualize(lost, days)
    return Result(
        annual,
        f"{int(slow.sum())} tickets ({slow.mean() * 100:.1f}%) took longer than "
        f"{bm.v('slow_ticket_minutes'):.0f} minutes in the kitchen.",
        {"slow_tickets": int(slow.sum()), "slow_share": float(slow.mean())},
    )


@detector("overtime", CAT, "Overtime premium", ("timeclock",))
def overtime(ds, bm):
    tc, days = ds.window("timeclock", "employee_id", "clock_in", "clock_out", "wage")
    tc = tc.dropna(subset=["clock_in", "clock_out"])
    hours = (tc["clock_out"] - tc["clock_in"]).dt.total_seconds() / 3600
    week = tc["clock_in"].dt.normalize() - pd.to_timedelta(tc["clock_in"].dt.day_of_week, unit="D")
    g = pd.DataFrame({"emp": tc["employee_id"], "week": week, "h": hours, "w": tc["wage"]}).groupby(
        ["emp", "week"]).agg(h=("h", "sum"), w=("w", "mean"))
    ot = (g["h"] - bm.v("overtime_weekly_threshold")).clip(lower=0)
    premium = ot * g["w"] * bm.v("overtime_premium")
    annual = ds.annualize(premium.sum(), days)

    ot_total = float(ot.sum())
    by_emp = ot.groupby(level="emp").sum().sort_values(ascending=False)
    heavy = by_emp[by_emp > 0]
    weeks_with_ot = g[ot > 0]
    weekend = tc["clock_in"].dt.day_of_week >= 4
    ot_emps = tc["employee_id"].isin(heavy.index)
    weekend_share = float(hours[ot_emps & weekend].sum() / hours[ot_emps].sum() * 100) if ot_emps.any() else 0
    return Result(
        annual,
        f"{len(heavy)} employees logged {ot_total:.0f} overtime hours over the period, averaging "
        f"{weeks_with_ot['h'].mean() if len(weeks_with_ot) else 0:.0f} hrs in overtime weeks; "
        f"{weekend_share:.0f}% of their hours fall Fri-Sun.",
        {"ot_hours_annual": ds.annualize(ot_total, days),
         "avg_ot_wage": float((ot * g["w"]).sum() / ot_total) if ot_total else 0.0,
         "ot_week_hour_share": float(weeks_with_ot["h"].sum() / g["h"].sum()) if g["h"].sum() else 0.0,
         "employees_with_ot": len(heavy)},
        sample_rows(tc[ot_emps]),
    )
