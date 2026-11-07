from __future__ import annotations

from backend.cost_engine.detectors.base import Result, detector, money
from backend.ingest.dataset import MissingData

CAT = "order_channels"


@detector("third_party_fees", CAT, "Third-party delivery commissions", ("third_party_orders",),
          confidence=0.6)
def third_party_fees(ds, bm):
    tp, days = ds.window("third_party_orders", "subtotal", "commission")
    subtotal = float(tp["subtotal"].sum())
    if subtotal <= 0:
        raise MissingData("third-party orders have no subtotal", ("third_party_orders",))
    fees = float(tp["fees"].fillna(0).sum()) if "fees" in tp.columns else 0.0
    paid = float(tp["commission"].sum()) + fees
    rate = paid / subtotal
    direct = bm.v("direct_ordering_cost_pct")
    shift = bm.v("shiftable_order_share")
    annual = ds.annualize(max(0.0, rate - direct) * subtotal * shift, days)
    return Result(
        annual,
        f"Marketplaces take {rate * 100:.1f}% of {money(ds.annualize(subtotal, days))}/yr in orders vs. "
        f"~{direct * 100:.0f}% for direct online ordering; assumes {shift * 100:.0f}% of those orders could move.",
        {"commission_rate": rate, "annual_subtotal": ds.annualize(subtotal, days),
         "annual_fees_paid": ds.annualize(paid, days)},
    )
