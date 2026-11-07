"""Problem categories. Each one is something an AI tool on the site can solve;
``tool_tags`` is the join key into the tool catalog."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    description: str
    tool_tags: tuple[str, ...]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tool_tags"] = list(self.tool_tags)
        return d


CATEGORIES: dict[str, Category] = {c.id: c for c in [
    Category("inventory_waste", "Inventory and Waste",
             "Food that is wasted, over-ordered, over-portioned, or runs out.",
             ("inventory_management", "waste_tracking", "demand_forecasting")),
    Category("purchasing_suppliers", "Purchasing and Suppliers",
             "Paying more than necessary for ingredients, short deliveries, and emergency buys.",
             ("procurement_automation", "invoice_scanning", "supplier_price_tracking")),
    Category("labor_scheduling", "Labor Scheduling",
             "Schedules that don't match demand: idle staff, overtime, and slow service.",
             ("ai_scheduling", "demand_forecasting", "labor_analytics")),
    Category("hiring_retention", "Hiring and Retention",
             "Staff turnover and the cost of roles sitting unfilled.",
             ("ai_recruiting", "applicant_screening", "employee_engagement")),
    Category("order_channels", "Order Channels and Delivery",
             "Commissions paid to third-party delivery marketplaces.",
             ("online_ordering", "delivery_management")),
    Category("pos_leakage", "POS / Revenue Leakage",
             "Revenue lost to voids, comps, discounts, cash shortages, and underpriced items.",
             ("pos_analytics", "loss_prevention", "menu_engineering")),
    Category("guest_marketing", "Guest Experience and Marketing",
             "No-shows, reputation, slow table turns, and marketing that doesn't pay back.",
             ("reservation_management", "review_management", "marketing_automation")),
    Category("back_office_admin", "Back-Office / Admin",
             "Owner and manager hours spent on invoices, payroll, and bookkeeping.",
             ("bookkeeping_automation", "payroll_automation", "invoice_scanning")),
    Category("facilities", "Facilities",
             "Utility overspend and equipment downtime.",
             ("energy_management", "predictive_maintenance")),
]}

# Friendly names for the "upload this file to unlock" hint.
DOMAIN_LABELS: dict[str, str] = {
    "sales": "POS sales export (line items)",
    "recipes": "recipes / menu ingredient list",
    "inventory": "inventory counts",
    "purchases": "supplier invoices / purchases",
    "waste": "waste log",
    "stockouts": "86'd items / stockout log",
    "vendor_deliveries": "vendor delivery log (promised vs. delivered)",
    "third_party_orders": "third-party delivery statements (DoorDash, Uber Eats, ...)",
    "timeclock": "timeclock / payroll punches",
    "employees": "employee roster with hire and termination dates",
    "requisitions": "hiring requisitions / job postings",
    "reservations": "reservation export",
    "reviews": "online reviews export",
    "utilities": "utility bills",
    "equipment": "equipment maintenance / downtime log",
    "marketing": "marketing spend by channel",
    "cash": "cash drawer reconciliation",
}
