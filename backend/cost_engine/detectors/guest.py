from __future__ import annotations

from backend.cost_engine.detectors.base import Result, detector, money
from backend.ingest.dataset import MissingData
from backend.shared.metrics import annual_revenue, avg_check, sales_lines

CAT = "guest_marketing"


def _avg_check_per_cover(ds) -> float:
    lines = sales_lines(ds)
    if "guests" in lines.columns and lines["guests"].notna().any():
        orders = lines.groupby("order_id").agg(net=("net", "sum"), guests=("guests", "max"))
        if "channel" in lines.columns:
            ch = lines.groupby("order_id")["channel"].first().str.lower()
            orders = orders[ch.str.contains("dine", na=False)]
        orders = orders[(orders["guests"] > 0) & (orders["net"] > 0)]
        if not orders.empty:
            return float(orders["net"].sum() / orders["guests"].sum())
    return avg_check(ds) / 2


@detector("no_shows", CAT, "Reservation no-shows", ("reservations", "sales"))
def no_shows(ds, bm):
    r, days = ds.window("reservations", "party_size", "status")
    status = r["status"].fillna("").str.lower().str.replace(r"[^a-z]", "", regex=True)
    no_show = status.str.contains("noshow")
    seated = status.str.contains("seat|complete|arriv|finish")
    covers = r["party_size"].fillna(0)
    booked = float(covers[no_show | seated].sum())
    if booked <= 0:
        raise MissingData("reservations have no seated or no-show parties", ("reservations",))
    rate = float(covers[no_show].sum()) / booked
    target = bm.v("target_noshow_rate")
    excess_covers = max(0.0, rate - target) * booked
    check = _avg_check_per_cover(ds)
    annual = ds.annualize(excess_covers * check * bm.v("unfilled_seat_share"), days)
    return Result(
        annual,
        f"{rate * 100:.1f}% of reserved covers no-showed vs. a {target * 100:.0f}% target "
        f"(avg check per cover {money(check)}).",
        {"noshow_rate": rate, "booked_covers": booked},
    )


@detector("reviews", CAT, "Revenue lost to a below-target rating", ("reviews", "sales"), confidence=0.4)
def reviews(ds, bm):
    rv, _ = ds.window("reviews", "rating")
    avg = float(rv["rating"].mean())
    target = bm.v("target_rating")
    revenue = annual_revenue(ds) or 0.0
    gap = max(0.0, target - avg)
    annual = gap * bm.v("revenue_per_star") * revenue
    recent = rv.sort_values("date").tail(max(1, len(rv) // 4))["rating"].mean()
    return Result(
        annual,
        f"Average rating {avg:.2f} vs. a {target:.1f} target (most recent quarter of reviews: "
        f"{recent:.2f}). Low confidence: rating-to-revenue effects come from published studies.",
        {"avg_rating": avg, "recent_rating": float(recent), "reviews": len(rv)},
    )


@detector("marketing_waste", CAT, "Marketing that doesn't pay back", ("marketing",), confidence=0.6)
def marketing_waste(ds, bm):
    m = ds.require("marketing", "date", "channel_name", "spend", "attributed_revenue")
    months = m["date"].dt.to_period("M").nunique()
    by = m.groupby("channel_name").agg(spend=("spend", "sum"), revenue=("attributed_revenue", "sum"))
    by["contribution"] = by["revenue"] * bm.v("marketing_gross_margin") - by["spend"]
    losers = by[by["contribution"] < 0]
    annual = float(-losers["contribution"].sum()) * 12 / max(months, 1)
    detail = ", ".join(f"{ch} (spent {money(r.spend)}, returned {money(r.revenue)})" for ch, r in losers.iterrows())
    return Result(
        annual,
        f"Channels that lose money after food and labor costs: {detail or 'none'}.",
        {"channels": by.round(2).reset_index().to_dict("records"), "months": int(months)},
    )
