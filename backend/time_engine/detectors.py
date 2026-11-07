"""Time detectors: hours lost per week, by role.

Two kinds:
- admin: workload estimated from data volume x manual minutes per unit; hours
  lost = the share a typical tool removes (``tool_savings``).
- operational: time over target measured directly from timestamps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from backend.cost_engine.detectors.labor import staffing_slots
from backend.ingest.dataset import Dataset, MissingData
from backend.shared.benchmarks import Benchmarks
from backend.shared.metrics import sales_lines, weeks_in

ADMIN, OPERATIONAL = "admin", "operational"


@dataclass
class TimeResult:
    hours_lost_week: float
    current_hours_week: float
    evidence: str
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TimeDetector:
    name: str
    category: str
    label: str
    role: str
    kind: str
    requires: tuple[str, ...]
    intrinsic_confidence: float
    # Hours already priced in dollars by the Cost engine (e.g. overstaffing).
    priced_in_cost: bool
    fn: Callable[[Dataset, Benchmarks], TimeResult]


TIME_REGISTRY: list[TimeDetector] = []


def time_detector(name, category, label, role, kind, requires, confidence=None, priced_in_cost=False):
    conf = confidence if confidence is not None else (0.7 if kind == ADMIN else 0.9)

    def wrap(fn):
        TIME_REGISTRY.append(TimeDetector(name, category, label, role, kind, tuple(requires), conf,
                                          priced_in_cost, fn))
        return fn
    return wrap


def _admin(current: float, task: str, bm: Benchmarks, evidence: str, metrics: dict) -> TimeResult:
    lost = current * bm.tool_savings(task)
    return TimeResult(lost, current, f"{evidence} About {current:.1f} hrs/week today; a typical tool "
                      f"saves ~{bm.tool_savings(task) * 100:.0f}%.", metrics)


# ----------------------------------------------------------------- admin tasks

@time_detector("scheduling", "labor_scheduling", "Building and editing schedules", "manager", ADMIN, ["timeclock"])
def scheduling(ds, bm):
    tc, days = ds.window("timeclock", "clock_in")
    shifts = len(tc) / weeks_in(days)
    current = shifts * bm.v("schedule_min_per_shift") / 60
    return _admin(current, "scheduling", bm, f"{shifts:.0f} shifts scheduled per week.",
                  {"shifts_per_week": shifts})


@time_detector("inventory_count", "inventory_waste", "Counting inventory", "manager", ADMIN, ["inventory"])
def inventory_count(ds, bm):
    inv, days = ds.window("inventory", "count_date", "sku")
    skus = inv["sku"].nunique()
    counts_week = inv["count_date"].dt.normalize().nunique() / weeks_in(days)
    current = skus * counts_week * bm.v("count_min_per_sku") / 60
    return _admin(current, "inventory_count", bm, f"{skus} SKUs counted {counts_week:.1f}x per week.",
                  {"skus": skus, "counts_per_week": counts_week})


@time_detector("ordering", "purchasing_suppliers", "Placing supplier orders", "manager", ADMIN, ["purchases"])
def ordering(ds, bm):
    p, days = ds.window("purchases", "invoice_id")
    pos = p["invoice_id"].nunique() / weeks_in(days)
    current = pos * bm.v("order_min_per_po") / 60
    return _admin(current, "ordering", bm, f"{pos:.1f} purchase orders per week.", {"orders_per_week": pos})


@time_detector("invoices", "back_office_admin", "Entering and reconciling invoices", "owner", ADMIN, ["purchases"])
def invoices(ds, bm):
    p, days = ds.window("purchases", "invoice_id")
    weeks = weeks_in(days)
    inv_week = p["invoice_id"].nunique() / weeks
    issues = pd.Series(False, index=p.index)
    if "qty_billed" in p.columns:
        issues |= (p["qty_billed"] - p["qty_received"]).abs() > 1e-9
    if "is_emergency" in p.columns:
        issues |= p["is_emergency"].fillna(False).astype(bool)
    disc_week = issues.sum() / weeks
    current = (inv_week * bm.v("invoice_min_per_invoice") + disc_week * bm.v("invoice_discrepancy_min")) / 60
    return _admin(current, "invoices", bm,
                  f"{inv_week:.1f} invoices and {disc_week:.1f} discrepancies to resolve per week.",
                  {"invoices_per_week": inv_week, "discrepancies_per_week": float(disc_week)})


@time_detector("payroll", "back_office_admin", "Running payroll", "owner", ADMIN, ["timeclock"])
def payroll(ds, bm):
    tc, _ = ds.window("timeclock", "employee_id")
    employees = tc["employee_id"].nunique()
    current = employees * bm.v("pay_periods_per_year") / 52 * bm.v("payroll_min_per_employee_period") / 60
    return _admin(current, "payroll", bm, f"{employees} employees on payroll.", {"employees": employees})


@time_detector("hiring", "hiring_retention", "Screening, interviewing, onboarding", "manager", ADMIN,
               ["requisitions"])
def hiring(ds, bm):
    rq, days = ds.window("requisitions", "req_id")
    applicants = rq["applicants"].fillna(0).sum() if "applicants" in rq.columns else 0
    interviews = rq["interviews"].fillna(0).sum() if "interviews" in rq.columns else 0
    if not applicants and not interviews:
        raise MissingData("requisitions need applicant or interview counts", ("requisitions",))
    hires = rq["filled_date"].notna().sum() if "filled_date" in rq.columns else len(rq)
    minutes = (applicants * bm.v("screening_min_per_applicant") + interviews * bm.v("interview_min")
               + hires * bm.v("onboarding_hours_per_hire") * 60)
    current = minutes / 60 / weeks_in(days)
    return _admin(current, "hiring", bm,
                  f"{int(applicants)} applicants, {int(interviews)} interviews, {int(hires)} hires in the period.",
                  {"applicants": int(applicants), "interviews": int(interviews), "hires": int(hires)})


@time_detector("review_responses", "guest_marketing", "Responding to reviews", "owner", ADMIN, ["reviews"])
def review_responses(ds, bm):
    rv, days = ds.window("reviews", "rating")
    per_week = len(rv) / weeks_in(days)
    current = per_week * bm.v("review_response_min") / 60
    responded = float(rv["responded"].mean()) if "responded" in rv.columns else None
    note = f" Only {responded * 100:.0f}% currently get a reply." if responded is not None else ""
    return _admin(current, "reviews", bm, f"{per_week:.1f} new reviews per week.{note}",
                  {"reviews_per_week": per_week, "response_rate": responded})


@time_detector("bookkeeping", "back_office_admin", "Bookkeeping and reporting", "owner", ADMIN, ["sales"])
def bookkeeping(ds, bm):
    s, days = ds.window("sales", "order_id")
    orders_week = s["order_id"].nunique() / weeks_in(days)
    current = bm.v("bookkeeping_base_hours_week") + orders_week / 1000 * bm.v("bookkeeping_min_per_1000_txn") / 60
    return _admin(current, "bookkeeping", bm, f"{orders_week:,.0f} transactions per week.",
                  {"orders_per_week": orders_week})


# ----------------------------------------------------------- operational time

@time_detector("kitchen_delays", "labor_scheduling", "Kitchen ticket time over target", "staff", OPERATIONAL,
               ["sales"])
def kitchen_delays(ds, bm):
    s, days = ds.window("sales", "order_id", "kitchen_minutes")
    per_order = s.groupby("order_id")["kitchen_minutes"].max().dropna()
    target = bm.v("kitchen_target_minutes")
    over = (per_order - target).clip(lower=0)
    hours = over.sum() / 60 / weeks_in(days)
    return TimeResult(hours, hours,
                      f"{(over > 0).mean() * 100:.0f}% of tickets ran past {target:.0f} minutes "
                      f"(median {per_order.median():.0f} min).",
                      {"median_minutes": float(per_order.median()), "share_over": float((over > 0).mean())})


@time_detector("table_turns", "guest_marketing", "Slow table turns", "staff", OPERATIONAL, ["sales"])
def table_turns(ds, bm):
    s, days = ds.window("sales", "order_id", "ticket_open", "ticket_close")
    lines = sales_lines(ds).loc[s.index]
    if "channel" in lines.columns:
        lines = lines[lines["channel"].fillna("").str.lower().str.contains("dine")]
    orders = lines.groupby("order_id").agg(o=("ticket_open", "min"), c=("ticket_close", "max"))
    minutes = ((orders["c"] - orders["o"]).dt.total_seconds() / 60).dropna()
    if minutes.empty:
        raise MissingData("no dine-in checks with open and close times", ("sales",))
    target = bm.v("table_turn_target_minutes")
    over = (minutes - target).clip(lower=0)
    hours = over.sum() / 60 / weeks_in(days)
    return TimeResult(hours, hours,
                      f"Dine-in checks stay open a median {minutes.median():.0f} minutes; "
                      f"{(over > 0).mean() * 100:.0f}% exceed {target:.0f} minutes.",
                      {"median_minutes": float(minutes.median()), "share_over": float((over > 0).mean())})


@time_detector("vendor_followups", "purchasing_suppliers", "Chasing late deliveries", "manager", OPERATIONAL,
               ["vendor_deliveries"])
def vendor_followups(ds, bm):
    d, days = ds.window("vendor_deliveries", "promised_at", "delivered_at")
    late_hours = (d["delivered_at"] - d["promised_at"]).dt.total_seconds() / 3600
    late = late_hours > bm.v("late_delivery_grace_hours")
    hours = late.sum() * bm.v("late_delivery_followup_min") / 60 / weeks_in(days)
    return TimeResult(hours, hours,
                      f"{late.sum()} late deliveries (avg {late_hours[late].mean() if late.any() else 0:.1f} hrs late) "
                      "each need a follow-up and a workaround.",
                      {"late_deliveries": int(late.sum())})


@time_detector("idle_labor", "labor_scheduling", "Idle staff hours", "staff", OPERATIONAL,
               ["sales", "timeclock"], priced_in_cost=True)
def idle_labor(ds, bm):
    slots = staffing_slots(ds, bm)
    hours = float(slots["excess"].sum())
    return TimeResult(hours, hours,
                      f"{hours:.0f} staff hours a week are scheduled beyond demand.",
                      {"excess_hours_per_week": hours})
