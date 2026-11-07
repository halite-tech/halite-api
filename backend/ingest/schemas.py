"""Canonical schemas for every data domain the engines understand.

Uploaded files are mapped onto these schemas. Column names are global: the
same canonical column (e.g. ``quantity``) means the same thing in every domain
that uses it, and its type is defined once in ``COLUMN_TYPES``.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Schema:
    domain: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    date_col: str | None = None
    # Sparse event logs (one row per incident) are annualized over the whole
    # analysis period, not over the span between their first and last rows.
    sparse: bool = False
    filename_hints: tuple[str, ...] = ()
    # Quantity columns converted to a base unit, and per-unit money columns
    # that must be scaled inversely when the unit changes.
    qty_cols: tuple[str, ...] = ()
    per_unit_cols: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return self.required + self.optional


COLUMN_TYPES: dict[str, str] = {
    # sales
    "order_id": "str", "timestamp": "datetime", "item": "str", "quantity": "number",
    "unit_price": "number", "discount": "number", "is_void": "bool", "is_comp": "bool",
    "channel": "str", "server": "str", "ticket_open": "datetime", "ticket_close": "datetime",
    "guests": "number", "kitchen_minutes": "number",
    # recipes / inventory / purchasing
    "sku": "str", "qty_per_item": "number", "unit": "str", "menu_price": "number",
    "count_date": "datetime", "on_hand_qty": "number", "unit_cost": "number",
    "invoice_id": "str", "invoice_date": "datetime", "vendor": "str",
    "qty_ordered": "number", "qty_received": "number", "qty_billed": "number",
    "is_emergency": "bool", "waste_date": "datetime", "reason": "str", "waste_cost": "number",
    "date": "datetime", "hours_unavailable": "number",
    # logistics / channels
    "promised_at": "datetime", "delivered_at": "datetime", "delivery_id": "str",
    "order_date": "datetime", "platform": "str", "subtotal": "number",
    "commission": "number", "fees": "number",
    # labor / HR
    "employee_id": "str", "role": "str", "wage": "number", "clock_in": "datetime",
    "clock_out": "datetime", "scheduled_start": "datetime", "scheduled_end": "datetime",
    "hire_date": "datetime", "termination_date": "datetime", "req_id": "str",
    "opened_date": "datetime", "filled_date": "datetime", "applicants": "number",
    "interviews": "number",
    # guests / marketing / facilities / cash
    "party_size": "number", "status": "str", "rating": "number", "text": "str",
    "responded": "bool", "month": "datetime", "kind": "str", "amount": "number",
    "equipment": "str", "downtime_hours": "number", "repair_cost": "number",
    "channel_name": "str", "spend": "number", "attributed_orders": "number",
    "attributed_revenue": "number", "expected": "number", "counted": "number",
}

# Normalized header text (lowercase, alphanumerics separated by single spaces).
SYNONYMS: dict[str, tuple[str, ...]] = {
    "order_id": ("order", "order id", "order number", "order no", "check", "check id",
                 "check number", "ticket", "ticket id", "transaction id"),
    "timestamp": ("order time", "timestamp", "datetime", "date time", "order date", "time",
                  "date", "sale time", "transaction time"),
    "item": ("item", "menu item", "item name", "product", "dish", "menu item name"),
    "quantity": ("qty", "quantity", "units", "count", "qty sold", "qty wasted",
                 "amount wasted", "wasted qty", "quantity wasted"),
    "unit_price": ("price", "unit price", "item price", "price each", "cost per unit",
                   "price per unit"),
    "discount": ("discount", "discount amount", "promo", "discounts"),
    "is_void": ("void", "voided", "is void"),
    "is_comp": ("comp", "comped", "is comp"),
    "channel": ("order type", "channel", "dining option", "service type"),
    "server": ("server", "employee", "staff", "cashier"),
    "ticket_open": ("check opened", "opened", "open time", "ticket open", "opened at"),
    "ticket_close": ("check closed", "closed", "close time", "ticket close", "closed at",
                     "paid at"),
    "guests": ("guests", "covers", "guest count", "party size"),
    "kitchen_minutes": ("kitchen minutes", "ticket time", "prep time", "kds minutes",
                        "cook time"),
    "sku": ("sku", "ingredient sku", "item code", "product code", "ingredient",
            "ingredient id"),
    "qty_per_item": ("amount", "qty per item", "quantity per item", "portion",
                     "portion size", "recipe qty"),
    "unit": ("unit", "uom", "unit of measure"),
    "menu_price": ("menu price", "selling price"),
    "count_date": ("count date", "date counted", "inventory date", "date"),
    "on_hand_qty": ("on hand", "on hand qty", "qty on hand", "quantity on hand", "count",
                    "counted qty"),
    "unit_cost": ("unit cost", "cost", "avg cost", "cost per unit"),
    "invoice_id": ("invoice", "invoice id", "invoice number", "invoice no", "po",
                   "po number"),
    "invoice_date": ("invoice date", "date", "delivery date", "received date"),
    "vendor": ("vendor", "supplier", "distributor"),
    "qty_ordered": ("qty ordered", "ordered", "quantity ordered", "order qty"),
    "qty_received": ("qty received", "received", "quantity received", "qty delivered"),
    "qty_billed": ("qty billed", "billed", "quantity billed", "invoiced qty"),
    "is_emergency": ("emergency", "is emergency", "rush order"),
    "waste_date": ("date", "waste date", "date wasted"),
    "reason": ("reason", "waste reason", "notes"),
    "waste_cost": ("waste cost", "cost", "value"),
    "date": ("date", "day", "reservation date", "review date", "event date", "month"),
    "hours_unavailable": ("hours 86d", "hours unavailable", "hours out", "duration hours",
                          "86 hours"),
    "promised_at": ("promised time", "promised at", "scheduled delivery", "expected arrival",
                    "eta"),
    "delivered_at": ("delivered time", "delivered at", "actual delivery", "arrival time",
                     "arrived"),
    "delivery_id": ("delivery id", "delivery number"),
    "order_date": ("date", "order date", "order time"),
    "platform": ("platform", "provider", "marketplace"),
    "subtotal": ("subtotal", "order subtotal", "gross sales", "order total"),
    "commission": ("commission", "commission fee", "marketplace fee"),
    "fees": ("fees", "other fees", "processing fee"),
    "employee_id": ("employee id", "employee", "emp id", "staff id", "employee number"),
    "role": ("role", "job", "position", "job title"),
    "wage": ("hourly rate", "wage", "pay rate", "rate", "hourly wage"),
    "clock_in": ("clock in", "in", "start", "time in", "punch in"),
    "clock_out": ("clock out", "out", "end", "time out", "punch out"),
    "scheduled_start": ("scheduled start", "shift start"),
    "scheduled_end": ("scheduled end", "shift end"),
    "hire_date": ("hire date", "start date", "date hired"),
    "termination_date": ("termination date", "end date", "term date", "separation date",
                         "date terminated"),
    "req_id": ("req id", "requisition id", "job id", "posting id", "requisition"),
    "opened_date": ("date opened", "opened", "open date", "posted date"),
    "filled_date": ("date filled", "filled", "fill date", "closed date"),
    "applicants": ("applicants", "applications", "num applicants"),
    "interviews": ("interviews", "num interviews"),
    "party_size": ("party size", "covers", "guests", "party"),
    "status": ("status", "outcome", "reservation status"),
    "rating": ("stars", "rating", "score"),
    "text": ("review text", "text", "comment", "review"),
    "responded": ("responded", "owner responded", "has response", "replied"),
    "month": ("bill month", "month", "period", "billing period"),
    "kind": ("utility", "type", "kind", "service"),
    "amount": ("amount", "total", "bill amount", "cost"),
    "equipment": ("equipment", "asset", "machine"),
    "downtime_hours": ("downtime hours", "downtime", "hours down"),
    "repair_cost": ("repair cost", "repair", "cost"),
    "channel_name": ("channel", "campaign channel", "source", "platform"),
    "spend": ("spend", "cost", "budget", "amount spent"),
    "attributed_orders": ("attributed orders", "orders", "conversions"),
    "attributed_revenue": ("attributed revenue", "revenue", "sales"),
    "expected": ("expected cash", "expected", "system cash", "expected amount"),
    "counted": ("counted cash", "counted", "actual cash", "actual"),
}

SCHEMAS: dict[str, Schema] = {s.domain: s for s in [
    Schema("sales", ("order_id", "timestamp", "item", "quantity", "unit_price"),
           ("discount", "is_void", "is_comp", "channel", "server", "ticket_open",
            "ticket_close", "guests", "kitchen_minutes"),
           date_col="timestamp", filename_hints=("sales", "pos", "transaction", "orders")),
    Schema("recipes", ("item", "sku", "qty_per_item"), ("unit", "menu_price"),
           filename_hints=("recipe", "menu", "bom"), qty_cols=("qty_per_item",)),
    Schema("inventory", ("count_date", "sku", "on_hand_qty"), ("unit_cost", "unit"),
           date_col="count_date", filename_hints=("inventory", "count", "stock"),
           qty_cols=("on_hand_qty",), per_unit_cols=("unit_cost",)),
    Schema("purchases", ("invoice_id", "invoice_date", "vendor", "sku", "qty_received",
                         "unit_price"),
           ("qty_ordered", "qty_billed", "is_emergency", "unit"),
           date_col="invoice_date", filename_hints=("purchase", "invoice", "po", "supply"),
           qty_cols=("qty_ordered", "qty_received", "qty_billed"),
           per_unit_cols=("unit_price",)),
    Schema("waste", ("waste_date", "sku", "quantity"), ("reason", "unit", "waste_cost"),
           date_col="waste_date", filename_hints=("waste", "spoil"), qty_cols=("quantity",)),
    Schema("stockouts", ("date", "item", "hours_unavailable"), date_col="date", sparse=True,
           filename_hints=("86", "stockout", "out of stock")),
    Schema("vendor_deliveries", ("vendor", "promised_at", "delivered_at"), ("delivery_id",),
           date_col="promised_at", filename_hints=("deliver", "logistic", "receiving")),
    Schema("third_party_orders", ("order_date", "platform", "subtotal", "commission"),
           ("fees", "order_id"), date_col="order_date",
           filename_hints=("third", "doordash", "uber", "grubhub")),
    Schema("timeclock", ("employee_id", "clock_in", "clock_out", "wage"),
           ("role", "scheduled_start", "scheduled_end"), date_col="clock_in",
           filename_hints=("time", "clock", "labor", "payroll", "punch")),
    Schema("employees", ("employee_id", "hire_date"), ("termination_date", "role", "wage"),
           date_col="hire_date", sparse=True,
           filename_hints=("employee", "roster", "hr", "staff")),
    Schema("requisitions", ("req_id", "opened_date"),
           ("filled_date", "role", "applicants", "interviews"), date_col="opened_date",
           sparse=True, filename_hints=("requisition", "hiring", "job", "recruit")),
    Schema("reservations", ("date", "party_size", "status"), date_col="date",
           filename_hints=("reserv", "booking", "opentable", "resy")),
    Schema("reviews", ("date", "rating"), ("text", "responded"), date_col="date",
           filename_hints=("review", "yelp", "google")),
    Schema("utilities", ("month", "amount"), ("kind",), date_col="month",
           filename_hints=("utilit", "energy", "electric")),
    Schema("equipment", ("date", "equipment", "downtime_hours"), ("repair_cost",),
           date_col="date", sparse=True,
           filename_hints=("equipment", "maintenance", "repair")),
    Schema("marketing", ("date", "channel_name", "spend"),
           ("attributed_orders", "attributed_revenue"), date_col="date",
           filename_hints=("marketing", "ads", "campaign")),
    Schema("cash", ("date", "expected", "counted"), date_col="date",
           filename_hints=("cash", "drawer", "till")),
]}

# Unit -> (base unit, multiplier to convert a quantity into the base unit).
UNIT_CONVERSIONS: dict[str, tuple[str, float]] = {
    "lb": ("lb", 1.0), "lbs": ("lb", 1.0), "pound": ("lb", 1.0), "pounds": ("lb", 1.0),
    "oz": ("lb", 1 / 16), "ounce": ("lb", 1 / 16), "ounces": ("lb", 1 / 16),
    "g": ("lb", 1 / 453.592), "gram": ("lb", 1 / 453.592), "grams": ("lb", 1 / 453.592),
    "kg": ("lb", 2.20462),
    "l": ("l", 1.0), "liter": ("l", 1.0), "litre": ("l", 1.0), "liters": ("l", 1.0),
    "ml": ("l", 0.001), "gal": ("l", 3.78541), "gallon": ("l", 3.78541),
    "qt": ("l", 0.946353), "quart": ("l", 0.946353), "floz": ("l", 0.0295735),
    "fl oz": ("l", 0.0295735),
    "each": ("each", 1.0), "ea": ("each", 1.0), "unit": ("each", 1.0), "pc": ("each", 1.0),
}


@dataclass
class FileReport:
    filename: str
    domain: str | None
    rows: int
    mapping: dict[str, str] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    match_score: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "filename": self.filename, "domain": self.domain, "rows": self.rows,
            "mapping": self.mapping, "unmapped": self.unmapped,
            "missing_required": self.missing_required,
            "match_score": round(self.match_score, 2), "note": self.note,
        }
