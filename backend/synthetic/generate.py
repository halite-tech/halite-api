"""Synthetic demo restaurant ("Mario's Trattoria") with planted problems.

Six months of data across every domain. The restaurant is healthy except for
the planted problems below, whose true annual cost is computed here from the
planted parameters so the engine's estimates can be checked against them:

- waste at 9% of food purchases (benchmark target 4%)
- weekend overtime: five core staff work ~52 hours a week
- overstaffing: +2 staff Tue-Thu 2pm-5pm
- ~120% annual turnover among part-time staff, with ~20 days to fill each role
- a 12% price increase on 3 SKUs from day 60 onward

Usage:  python -m backend.synthetic.generate --out backend/synthetic/data
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from backend.ingest.dataset import Profile
from backend.shared.benchmarks import Benchmarks

START = pd.Timestamp("2026-04-01")
DAYS = 182
OPEN_HOURS = tuple(range(11, 22))
HOUR_SHAPE = {11: .6, 12: 1.0, 13: .8, 14: .4, 15: .3, 16: .4, 17: .8, 18: 1.0, 19: 1.0, 20: .7, 21: .4}
DOW_MULT = (.85, .8, .85, .95, 1.25, 1.35, 1.1)  # Mon..Sun
TARGET_DAILY_REVENUE = 4000.0

# sku -> (base unit, base price per unit, vendor)
SKUS: dict[str, tuple[str, float, str]] = {
    "FLOUR": ("lb", 0.60, "Sysco"),
    "PASTA": ("lb", 1.40, "Sysco"),
    "MOZZ": ("lb", 4.50, "Sysco"),
    "PARM": ("lb", 9.50, "Sysco"),
    "OLIVE_OIL": ("l", 9.00, "Sysco"),
    "CREAM": ("l", 4.00, "Sysco"),
    "BEEF": ("lb", 6.50, "Prime Meats"),
    "CHICKEN": ("lb", 3.20, "Prime Meats"),
    "SHRIMP": ("lb", 11.00, "Ocean Co"),
    "TOMATO": ("lb", 1.80, "Fresh Farms"),
    "LETTUCE": ("lb", 2.20, "Fresh Farms"),
    "BASIL": ("lb", 14.00, "Fresh Farms"),
    "BREAD": ("each", 0.50, "Bakery"),
}
PURCHASE_UNITS = {"OLIVE_OIL": ("gal", 3.78541)}  # bought by the gallon, counted in liters
VENDOR_DAYS = {"Sysco": (0, 3), "Prime Meats": (1, 4), "Ocean Co": (1, 4),
               "Fresh Farms": (0, 2, 4), "Bakery": (0, 2, 4, 5)}
VENDOR_PAR_DAYS = {"Sysco": 6, "Prime Meats": 6, "Ocean Co": 6, "Fresh Farms": 5, "Bakery": 4}
VENDOR_HOUR = {"Bakery": 7}

RECIPE_UNIT_TO_BASE = {"oz": 1 / 16, "ml": 0.001, "each": 1.0}
# item -> (popularity, target food cost %, [(sku, qty, recipe unit)])
RECIPES: dict[str, tuple[float, float, list[tuple[str, float, str]]]] = {
    "Margherita Pizza": (.20, .27, [("FLOUR", 10, "oz"), ("TOMATO", 6, "oz"), ("MOZZ", 8, "oz"),
                                    ("BASIL", .3, "oz"), ("OLIVE_OIL", 20, "ml")]),
    "Spaghetti Bolognese": (.15, .27, [("PASTA", 6, "oz"), ("BEEF", 5, "oz"), ("TOMATO", 5, "oz"),
                                       ("PARM", .5, "oz"), ("OLIVE_OIL", 10, "ml")]),
    "Chicken Parmesan": (.15, .27, [("CHICKEN", 8, "oz"), ("MOZZ", 3, "oz"), ("TOMATO", 4, "oz"),
                                    ("PASTA", 4, "oz"), ("PARM", .5, "oz")]),
    "Caesar Salad": (.12, .27, [("LETTUCE", 6, "oz"), ("PARM", 1, "oz"), ("BREAD", 1, "each"),
                                ("OLIVE_OIL", 15, "ml")]),
    "Fettuccine Alfredo": (.10, .27, [("PASTA", 6, "oz"), ("CREAM", 150, "ml"), ("PARM", 1.5, "oz")]),
    "Seafood Linguine": (.08, .38, [("PASTA", 6, "oz"), ("SHRIMP", 6, "oz"), ("TOMATO", 4, "oz"),
                                    ("OLIVE_OIL", 20, "ml"), ("BASIL", .2, "oz")]),
    "Garlic Bread": (.12, .27, [("BREAD", 2, "each"), ("OLIVE_OIL", 10, "ml"), ("PARM", .3, "oz")]),
    "Beef Lasagna": (.08, .27, [("PASTA", 4, "oz"), ("BEEF", 6, "oz"), ("MOZZ", 4, "oz"),
                                ("TOMATO", 5, "oz"), ("PARM", .5, "oz")]),
}
CHANNELS = ("Dine In", "Takeout", "Third Party")
CHANNEL_P = (.72, .18, .10)

# Planted problems
PRICE_CREEP_SKUS = ("MOZZ", "BEEF", "OLIVE_OIL")
PRICE_CREEP_PCT = 0.12
PRICE_CREEP_START_DAY = 60
WASTE_RATE = 0.09
OVERSTAFF_DOWS = (1, 2, 3)
OVERSTAFF_HOURS = (14, 15, 16)
OVERSTAFF_EXTRA = 2
CORE_WEEKLY_HOURS = 52
PART_TIME_WEEKLY_CAP = 38
MAX_SHIFT_HOURS = 8
N_CORE, N_PART, SEPARATIONS = 5, 20, 15
THIRD_PARTY_COMMISSION = 0.25
SCHEDULE_SPLH = 55  # what the manager schedules to (matches the typical benchmark)

EXPECTED_TOP3 = ("labor_scheduling", "inventory_waste", "hiring_retention")


@dataclass
class SyntheticRestaurant:
    files: dict[str, pd.DataFrame]
    truth: dict[str, float]
    expected_top3: tuple[str, ...]
    profile: Profile
    notes: dict = field(default_factory=dict)

    def raw_files(self) -> list[tuple[str, bytes]]:
        return [(name, df.to_csv(index=False).encode()) for name, df in self.files.items()]

    def write(self, out_dir: str | Path) -> Path:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, df in self.files.items():
            df.to_csv(out / name, index=False)
        return out


def _fmt(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d %H:%M")


def _menu_prices() -> dict[str, float]:
    prices = {}
    for item, (_, fc_target, recipe) in RECIPES.items():
        food_cost = sum(q * RECIPE_UNIT_TO_BASE[u] * SKUS[s][1] for s, q, u in recipe)
        prices[item] = max(4.95, round(food_cost / fc_target) - 0.05)
    return prices


class _Generator:
    def __init__(self, seed: int, bm: Benchmarks):
        self.rng = np.random.default_rng(seed)
        self.bm = bm
        self.dates = [START + pd.Timedelta(days=d) for d in range(DAYS)]
        self.period_end = self.dates[-1]
        self.items = list(RECIPES)
        self.prices = _menu_prices()
        self.sku_list = list(SKUS)
        self.truth: dict[str, float] = {}

    # ------------------------------------------------------------------ sales
    def sales(self):
        rng = self.rng
        pop = np.array([RECIPES[i][0] for i in self.items])
        pop = pop / pop.sum()
        price_arr = np.array([self.prices[i] for i in self.items])
        avg_check = 2.5 * float((pop * price_arr).sum())
        base = TARGET_DAILY_REVENUE / (avg_check * sum(HOUR_SHAPE.values()) * np.mean(DOW_MULT))

        lines, tp_orders = [], []
        hourly_net = np.zeros((DAYS, len(OPEN_HOURS)))
        consumed = np.zeros((DAYS, len(self.items)))
        cash_share = np.zeros(DAYS)
        oid = 0
        for d, date in enumerate(self.dates):
            dow = date.day_of_week
            day = date.to_pydatetime()
            for hi, h in enumerate(OPEN_HOURS):
                n = rng.poisson(base * HOUR_SHAPE[h] * DOW_MULT[dow])
                if n == 0:
                    continue
                chans = rng.choice(3, size=n, p=CHANNEL_P)
                ks = 1 + rng.poisson(1.5, size=n)
                minutes = rng.integers(0, 60, size=n)
                kitchen = np.round(rng.lognormal(math.log(12), 0.3, size=n), 1)
                items = rng.choice(len(self.items), size=int(ks.sum()), p=pop)
                rand = rng.random(size=(int(ks.sum()), 2))
                disc_orders = rng.random(size=n) < 0.02
                durations = np.where(chans == 0, np.maximum(25, rng.normal(55, 12, size=n)),
                                     rng.uniform(10, 20, size=n))
                guests = np.where(chans == 0, 1 + rng.binomial(3, .5, size=n), 1)
                pos = 0
                for o in range(n):
                    oid += 1
                    order_id = f"M{oid:06d}"
                    ts = day + timedelta(hours=h, minutes=int(minutes[o]))
                    ts_s = ts.strftime("%Y-%m-%d %H:%M")
                    close_s = (ts + timedelta(minutes=float(durations[o]))).strftime("%Y-%m-%d %H:%M")
                    channel = CHANNELS[chans[o]]
                    order_net = 0.0
                    for j in range(ks[o]):
                        idx = items[pos]
                        void = rand[pos, 0] < 0.005
                        comp = (not void) and rand[pos, 1] < 0.004
                        pos += 1
                        price = self.prices[self.items[idx]]
                        disc = round(0.10 * price, 2) if (disc_orders[o] and j == 0) else 0.0
                        net = 0.0 if (void or comp) else round(price - disc, 2)
                        order_net += net
                        if not void:
                            consumed[d, idx] += 1
                        lines.append((order_id, ts_s, self.items[idx], 1, price, disc,
                                      "Y" if void else "", "Y" if comp else "", channel,
                                      f"S{(oid % 6) + 1}", ts_s, close_s,
                                      int(guests[o]), float(kitchen[o])))
                    hourly_net[d, hi] += order_net
                    if channel == "Third Party":
                        fee = float(rng.choice([0.0, 0.5, 1.0]))
                        tp_orders.append((order_id, date.strftime("%Y-%m-%d"),
                                          "DoorDash" if rng.random() < .6 else "Uber Eats",
                                          round(order_net, 2),
                                          round(order_net * THIRD_PARTY_COMMISSION, 2), fee))
            cash_share[d] = hourly_net[d].sum() * 0.15

        self.files["sales.csv"] = pd.DataFrame(lines, columns=[
            "Order #", "Order Time", "Menu Item", "Qty", "Price", "Discount", "Void", "Comp",
            "Order Type", "Server", "Check Opened", "Check Closed", "Guests", "Kitchen Minutes"])
        self.files["third_party_orders.csv"] = pd.DataFrame(tp_orders, columns=[
            "Order ID", "Date", "Platform", "Subtotal", "Commission", "Fees"])
        self.hourly_net = hourly_net
        self.consumed = consumed
        self.cash_sales = cash_share

    # ------------------------------------------------------------- inventory
    def inventory(self):
        rng = self.rng
        n_sku = len(self.sku_list)
        bom = np.zeros((len(self.items), n_sku))
        for i, item in enumerate(self.items):
            for sku, q, u in RECIPES[item][2]:
                bom[i, self.sku_list.index(sku)] += q * RECIPE_UNIT_TO_BASE[u]
        usage = self.consumed @ bom
        waste_factor = WASTE_RATE / (1 - WASTE_RATE)
        waste = np.round(usage * waste_factor * rng.uniform(0.7, 1.3, size=usage.shape), 3)
        daily_need = (usage + waste).mean(axis=0)
        par = np.array([daily_need[k] * VENDOR_PAR_DAYS[SKUS[s][2]] for k, s in enumerate(self.sku_list)])
        on_hand = par.copy()

        purchases, deliveries, counts, waste_rows = [], [], [], []
        creep_truth = 0.0
        inv_no = 1000
        reasons = ("Spoiled", "Overproduction", "Dropped", "Expired", "Prep error")
        last_price = {s: SKUS[s][1] for s in self.sku_list}

        def emergency(d, k, qty):
            nonlocal inv_no
            sku = self.sku_list[k]
            unit, base_price, _ = SKUS[sku]
            inv_no += 1
            purchases.append((f"INV{inv_no}", self.dates[d].strftime("%Y-%m-%d"), "Restaurant Depot",
                              sku, qty, qty, qty, f"${base_price * 1.15:.2f}", "Yes", unit))
            return qty

        for d, date in enumerate(self.dates):
            dow = date.day_of_week
            for vendor, days in VENDOR_DAYS.items():
                if dow not in days:
                    continue
                promised = date + pd.Timedelta(hours=VENDOR_HOUR.get(vendor, 9))
                late = rng.random() < 0.08
                delay = rng.uniform(2, 5) * 60 if late else max(-20.0, rng.normal(0, 15))
                deliveries.append((vendor, _fmt(promised), _fmt(promised + pd.Timedelta(minutes=delay))))
                inv_no += 1
                invoice = f"INV{inv_no}"
                for k, sku in enumerate(self.sku_list):
                    unit, base_price, sku_vendor = SKUS[sku]
                    if sku_vendor != vendor:
                        continue
                    need = par[k] - on_hand[k]
                    if need <= 0.01:
                        continue
                    p_unit, factor = PURCHASE_UNITS.get(sku, (unit, 1.0))
                    qty = int(math.ceil(need / factor))
                    short = int(min(qty, rng.integers(1, 3))) if rng.random() < 0.03 else 0
                    received = qty - short
                    creep = sku in PRICE_CREEP_SKUS and d >= PRICE_CREEP_START_DAY
                    price_base = base_price * (1 + PRICE_CREEP_PCT if creep else 1) * rng.uniform(.995, 1.005)
                    price = round(price_base * factor, 2)
                    if creep:
                        creep_truth += PRICE_CREEP_PCT * base_price * factor * qty
                    last_price[sku] = price / factor
                    on_hand[k] += received * factor
                    purchases.append((invoice, date.strftime("%Y-%m-%d"), vendor, sku, qty, received,
                                      qty, f"${price:.2f}", "No", p_unit))
                if late and rng.random() < 0.5:
                    ks = [k for k, s in enumerate(self.sku_list) if SKUS[s][2] == vendor]
                    k = int(rng.choice(ks))
                    on_hand[k] += emergency(d, k, int(math.ceil(daily_need[k])))

            on_hand -= usage[d] + waste[d]
            for k in np.where(on_hand < 0)[0]:
                on_hand[k] += emergency(d, k, int(math.ceil(-on_hand[k] + daily_need[k])))

            for k, sku in enumerate(self.sku_list):
                if waste[d, k] > 0:
                    waste_rows.append((date.strftime("%Y-%m-%d"), sku, waste[d, k],
                                       reasons[int(rng.integers(0, len(reasons)))], SKUS[sku][0]))
            if dow == 6:
                for k, sku in enumerate(self.sku_list):
                    counts.append((date.strftime("%Y-%m-%d"), sku, round(on_hand[k], 3),
                                   round(last_price[sku], 2), SKUS[sku][0]))

        pur = pd.DataFrame(purchases, columns=[
            "Invoice #", "Invoice Date", "Vendor", "SKU", "Qty Ordered", "Qty Received", "Qty Billed",
            "Unit Price", "Emergency", "Unit"])
        self.files["purchases.csv"] = pur
        self.files["inventory_counts.csv"] = pd.DataFrame(counts, columns=[
            "Count Date", "SKU", "On Hand", "Unit Cost", "Unit"])
        self.files["waste_log.csv"] = pd.DataFrame(waste_rows, columns=[
            "Date", "SKU", "Qty Wasted", "Reason", "Unit"])
        self.files["vendor_deliveries.csv"] = pd.DataFrame(deliveries, columns=[
            "Vendor", "Promised Time", "Delivered Time"])
        self.files["recipes.csv"] = pd.DataFrame(
            [(item, s, q, u) for item, (_, _, rec) in RECIPES.items() for s, q, u in rec],
            columns=["Menu Item", "Ingredient SKU", "Amount", "UOM"])

        pur_days = (pd.to_datetime(pur["Invoice Date"]).max() - pd.to_datetime(pur["Invoice Date"]).min()).days + 1
        spend = (pur["Qty Received"] * pur["Unit Price"].str.lstrip("$").astype(float)).sum()
        annual_purchases = spend * 365 / pur_days
        self.truth["price_creep"] = creep_truth * 365 / pur_days
        self.truth["waste"] = (WASTE_RATE - self.bm.v("waste_pct_of_purchases")) * annual_purchases

    # ----------------------------------------------------------------- labor
    def labor(self):
        rng = self.rng
        core_roles = ("Kitchen Lead", "Line Cook", "Line Cook", "Server", "Server")
        pt_roles = ("Server", "Line Cook", "Host", "Dishwasher", "Busser")
        employees: dict[str, dict] = {}
        for i in range(N_CORE):
            employees[f"E{i + 1:03d}"] = dict(
                role=core_roles[i], wage=round(rng.uniform(17, 20), 2), core=True,
                hire=pd.Timestamp("2022-01-01") + pd.Timedelta(days=int(rng.integers(0, 900))), term=None)
        slots: list[list[tuple[str, int, int]]] = []  # per slot: (emp, first_day, end_day_exclusive)
        next_id = N_CORE + 1
        for s in range(N_PART):
            emp = f"E{next_id:03d}"
            next_id += 1
            employees[emp] = dict(role=pt_roles[s % 5], wage=round(rng.uniform(14, 17), 2), core=False,
                                  hire=pd.Timestamp("2025-03-01") + pd.Timedelta(days=int(rng.integers(0, 300))),
                                  term=None)
            slots.append([(emp, -10_000, 10_000)])

        reqs = []
        sep_days = sorted(rng.choice(np.arange(5, 165), size=SEPARATIONS, replace=False))
        for n, t in enumerate(sep_days):
            t = int(t)
            options = [i for i, seg in enumerate(slots) if seg[-1][1] <= t - 14 and seg[-1][2] > t]
            s = int(rng.choice(options))
            emp, first, _ = slots[s][-1]
            slots[s][-1] = (emp, first, t)
            employees[emp]["term"] = self.dates[t]
            gap = int(rng.integers(12, 29))
            new = f"E{next_id:03d}"
            next_id += 1
            hire_day = t + gap
            employees[new] = dict(role=employees[emp]["role"], wage=round(rng.uniform(14, 17), 2),
                                  core=False, hire=START + pd.Timedelta(days=hire_day), term=None)
            slots[s].append((new, hire_day, 10_000))
            reqs.append((f"R{n + 1:03d}", employees[emp]["role"], self.dates[t].strftime("%Y-%m-%d"),
                         (START + pd.Timedelta(days=hire_day)).strftime("%Y-%m-%d") if hire_day < DAYS else "",
                         int(rng.poisson(25)), int(rng.integers(3, 7))))

        def holder(slot, d):
            for emp, first, end in slots[slot]:
                if first <= d < end:
                    return emp
            return None

        # Schedule to realized demand, plus the planted afternoon overstaffing.
        avg = np.zeros((7, len(OPEN_HOURS)))
        for dow in range(7):
            idx = [d for d, dt in enumerate(self.dates) if dt.day_of_week == dow]
            avg[dow] = self.hourly_net[idx].mean(axis=0)
        shifts_by_week: dict[pd.Timestamp, list[tuple[int, int, int]]] = defaultdict(list)
        for d, date in enumerate(self.dates):
            dow = date.day_of_week
            need = []
            for hi, h in enumerate(OPEN_HOURS):
                c = max(2, math.ceil(avg[dow, hi] / SCHEDULE_SPLH - 1e-9))
                if dow in OVERSTAFF_DOWS and h in OVERSTAFF_HOURS:
                    c += OVERSTAFF_EXTRA
                need.append(c)
            for k in range(1, max(need) + 1):
                run_start = None
                for hi in range(len(OPEN_HOURS) + 1):
                    on = hi < len(OPEN_HOURS) and need[hi] >= k
                    if on and run_start is None:
                        run_start = hi
                    elif not on and run_start is not None:
                        a, b = OPEN_HOURS[run_start], OPEN_HOURS[hi - 1] + 1
                        if b - a > MAX_SHIFT_HOURS:
                            mid = a + (b - a) // 2
                            pieces = [(a, mid), (mid, b)]
                        else:
                            pieces = [(a, b)]
                        week = date - pd.Timedelta(days=dow)
                        for p in pieces:
                            shifts_by_week[week].append((d, p[0], p[1]))
                        run_start = None

        core_ids = [e for e, v in employees.items() if v["core"]]
        punches = []
        for week, shifts in sorted(shifts_by_week.items()):
            shifts.sort(key=lambda s: (0 if self.dates[s[0]].day_of_week >= 4 else 1, -(s[2] - s[1]), s[0], s[1]))
            hours = defaultdict(float)
            day_hours = defaultdict(float)
            busy = defaultdict(list)

            def free(emp, d, a, b):
                return all(b <= x or a >= y for x, y in busy[(emp, d)])

            for d, a, b in shifts:
                length = b - a
                cands = [e for e in core_ids if hours[e] + length <= CORE_WEEKLY_HOURS
                         and day_hours[(e, d)] + length <= 12 and free(e, d, a, b)]
                if not cands:
                    pts = [holder(s, d) for s in range(N_PART)]
                    pts = [e for e in pts if e is not None]
                    cands = [e for e in pts if hours[e] + length <= PART_TIME_WEEKLY_CAP
                             and day_hours[(e, d)] + length <= 10 and free(e, d, a, b)]
                    if not cands:
                        cands = [e for e in core_ids + pts if free(e, d, a, b)]
                emp = min(cands, key=lambda e: (hours[e], e))
                hours[emp] += length
                day_hours[(emp, d)] += length
                busy[(emp, d)].append((a, b))
                date = self.dates[d]
                punches.append((emp, employees[emp]["role"], employees[emp]["wage"],
                                _fmt(date + pd.Timedelta(hours=a)), _fmt(date + pd.Timedelta(hours=b))))

        tc = pd.DataFrame(punches, columns=["Employee ID", "Role", "Hourly Rate", "Clock In", "Clock Out"])
        self.files["timeclock.csv"] = tc
        self.files["employees.csv"] = pd.DataFrame(
            [(e, v["role"], v["hire"].strftime("%Y-%m-%d"),
              v["term"].strftime("%Y-%m-%d") if v["term"] is not None else "", v["wage"])
             for e, v in employees.items()],
            columns=["Employee ID", "Role", "Hire Date", "Termination Date", "Hourly Rate"])
        self.files["requisitions.csv"] = pd.DataFrame(reqs, columns=[
            "Req ID", "Role", "Date Opened", "Date Filled", "Applicants", "Interviews"])
        self._labor_truth(tc, employees, reqs)

    def _labor_truth(self, tc: pd.DataFrame, employees: dict, reqs: list):
        bm = self.bm
        cin = pd.to_datetime(tc["Clock In"])
        cout = pd.to_datetime(tc["Clock Out"])
        hrs = (cout - cin).dt.total_seconds() / 3600
        week = (cin.dt.normalize() - pd.to_timedelta(cin.dt.day_of_week, unit="D"))
        weekly = pd.DataFrame({"emp": tc["Employee ID"], "week": week, "h": hrs, "w": tc["Hourly Rate"]})
        g = weekly.groupby(["emp", "week"]).agg(h=("h", "sum"), w=("w", "mean"))
        ot = (g["h"] - bm.v("overtime_weekly_threshold")).clip(lower=0)
        span = (cin.dt.normalize().max() - cin.dt.normalize().min()).days + 1
        self.truth["overtime"] = float((ot * g["w"] * bm.v("overtime_premium")).sum()) * 365 / span

        slot_cost = 0.0
        for dow in OVERSTAFF_DOWS:
            for h in OVERSTAFF_HOURS:
                mask = (cin.dt.day_of_week == dow) & (cin.dt.hour <= h) & (cout.dt.hour > h)
                slot_cost += OVERSTAFF_EXTRA * float(tc.loc[mask, "Hourly Rate"].mean())
        self.truth["overstaffing"] = slot_cost * 52

        period = pd.date_range(START, self.period_end, freq="D")
        active = np.zeros(len(period))
        separated_wages = []
        for v in employees.values():
            on = period >= v["hire"]
            if v["term"] is not None:
                on &= period < v["term"]
                separated_wages.append(v["wage"])
            active += on
        avg_hc = active.mean()
        rate = len(separated_wages) * 365 / DAYS / avg_hc
        replacement = (bm.v("recruiting_cost_per_hire")
                       + bm.v("training_hours_per_hire") * np.mean(separated_wages)
                       + bm.v("ramp_productivity_loss_per_hire"))
        self.truth["turnover"] = max(0.0, rate - bm.v("target_turnover_rate")) * avg_hc * replacement

        excess_days = 0
        for _, _, opened, filled, _, _ in reqs:
            end = pd.Timestamp(filled) if filled else self.period_end
            excess_days += max(0, (end - pd.Timestamp(opened)).days - bm.v("target_days_to_fill"))
        self.truth["unfilled_roles"] = excess_days * bm.v("coverage_cost_per_open_day") * 365 / DAYS
        self.notes["annual_turnover_rate"] = rate

    # ------------------------------------------------------------ the rest
    def guests_and_facilities(self):
        rng = self.rng
        res, reviews, cash, stockouts = [], [], [], []
        texts = {5: "Amazing pasta, will be back!", 4: "Good food, a bit of a wait.",
                 3: "Okay overall, service was slow.", 2: "Cold food and long wait.",
                 1: "Very disappointing visit."}
        for d, date in enumerate(self.dates):
            ds = date.strftime("%Y-%m-%d")
            for _ in range(rng.poisson(28 * DOW_MULT[date.day_of_week])):
                status = rng.choice(["Seated", "No Show", "Cancelled"], p=[.895, .055, .05])
                res.append((ds, int(rng.choice([2, 2, 2, 3, 4, 4, 5, 6])), status))
            for _ in range(rng.poisson(1.5)):
                stars = int(rng.choice([5, 4, 3, 2, 1], p=[.6, .25, .08, .04, .03]))
                reviews.append((ds, stars, texts[stars], bool(rng.random() < .3)))
            expected = round(self.cash_sales[d], 2)
            cash.append((ds, expected, round(expected - rng.normal(2.5, 4), 2)))
        for d in sorted(rng.choice(DAYS, size=8, replace=False)):
            stockouts.append((self.dates[int(d)].strftime("%Y-%m-%d"),
                              self.items[int(rng.integers(0, len(self.items)))],
                              int(rng.integers(1, 5))))

        months = pd.period_range(START, self.period_end, freq="M")
        utilities, marketing = [], []
        for m in months:
            ms = str(m)
            for kind, amt in (("Electric", 1100), ("Gas", 500), ("Water", 250)):
                utilities.append((ms, kind, round(amt * rng.uniform(.93, 1.07), 2)))
            for ch, spend, orders, rev in (("Instagram Ads", 600, 80, 3200), ("Google Ads", 800, 90, 3600),
                                           ("Flyers", 400, 8, 300)):
                k = rng.uniform(.9, 1.1)
                marketing.append((ms, ch, spend, int(orders * k), round(rev * k, 2)))
        equipment = [
            ((START + pd.Timedelta(days=40)).strftime("%Y-%m-%d"), "Walk-in Cooler", 6, 850.0),
            ((START + pd.Timedelta(days=95)).strftime("%Y-%m-%d"), "Pizza Oven", 4, 420.0),
            ((START + pd.Timedelta(days=150)).strftime("%Y-%m-%d"), "Dishwasher", 3, 260.0),
        ]
        self.files["reservations.csv"] = pd.DataFrame(res, columns=["Reservation Date", "Party Size", "Status"])
        self.files["reviews.csv"] = pd.DataFrame(reviews, columns=["Review Date", "Stars", "Review Text", "Responded"])
        self.files["cash_drawer.csv"] = pd.DataFrame(cash, columns=["Date", "Expected Cash", "Counted Cash"])
        self.files["stockouts_86.csv"] = pd.DataFrame(stockouts, columns=["Date", "Item", "Hours 86d"])
        self.files["utilities.csv"] = pd.DataFrame(utilities, columns=["Bill Month", "Utility", "Amount"])
        self.files["marketing.csv"] = pd.DataFrame(marketing, columns=[
            "Month", "Channel", "Spend", "Attributed Orders", "Attributed Revenue"])
        self.files["equipment_log.csv"] = pd.DataFrame(equipment, columns=[
            "Date", "Equipment", "Downtime Hours", "Repair Cost"])

    def run(self) -> SyntheticRestaurant:
        self.files: dict[str, pd.DataFrame] = {}
        self.notes: dict = {}
        self.sales()
        self.inventory()
        self.labor()
        self.guests_and_facilities()
        profile = Profile(name="Mario's Trattoria", seats=80, sqft=3000)
        return SyntheticRestaurant(self.files, self.truth, EXPECTED_TOP3, profile, self.notes)


def generate(seed: int = 42, bm: Benchmarks | None = None) -> SyntheticRestaurant:
    return _Generator(seed, bm or Benchmarks.load()).run()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(Path(__file__).with_name("data")))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    restaurant = generate(args.seed)
    out = restaurant.write(args.out)
    print(f"wrote {len(restaurant.files)} files to {out}")
    for k, v in restaurant.truth.items():
        print(f"  planted {k:15s} ${v:,.0f}/yr")


if __name__ == "__main__":
    main()
