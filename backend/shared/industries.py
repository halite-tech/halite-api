"""Industry options and category weighting profiles for cost analysis."""
from __future__ import annotations

from typing import Final

DEFAULT_INDUSTRY: Final = "restaurant"

INDUSTRY_LABELS: Final[dict[str, str]] = {
    "restaurant": "Restaurant",
    "coffee_shop": "Coffee shop",
    "retail": "Retail",
    "healthcare": "Healthcare",
    "hospitality": "Hospitality",
    "hvac": "HVAC services",
    "landscaping": "Landscaping",
    "professional_services": "Professional services",
    "manufacturing": "Manufacturing",
}

# Multipliers adjust the relative importance of cost categories while preserving
# the underlying detector dollars and evidence.
INDUSTRY_WEIGHTS: Final[dict[str, dict[str, float]]] = {
    "restaurant": {},
    "coffee_shop": {
        "labor_scheduling": 1.25,
        "inventory_waste": 1.2,
        "purchasing": 1.15,
        "revenue_leakage": 1.1,
    },
    "retail": {
        "inventory_waste": 1.35,
        "revenue_leakage": 1.2,
        "labor_scheduling": 1.1,
        "purchasing": 1.15,
    },
    "healthcare": {
        "labor_scheduling": 1.3,
        "purchasing": 1.25,
        "facilities": 1.2,
        "revenue_leakage": 1.15,
    },
    "hospitality": {
        "labor_scheduling": 1.25,
        "revenue_leakage": 1.2,
        "facilities": 1.15,
        "inventory_waste": 1.1,
    },
    "hvac": {
        "labor_scheduling": 1.3,
        "purchasing": 1.25,
        "revenue_leakage": 1.2,
        "facilities": 1.1,
    },
    "landscaping": {
        "labor_scheduling": 1.3,
        "purchasing": 1.2,
        "facilities": 1.15,
        "revenue_leakage": 1.1,
    },
    "professional_services": {
        "labor_scheduling": 1.3,
        "revenue_leakage": 1.25,
        "purchasing": 0.8,
    },
    "manufacturing": {
        "inventory_waste": 1.3,
        "purchasing": 1.35,
        "facilities": 1.2,
        "labor_scheduling": 1.15,
    },
}


def normalize_industry(industry: str | None) -> str:
    """Return a supported industry, falling back to the restaurant baseline."""
    value = (industry or DEFAULT_INDUSTRY).strip().lower()
    return value if value in INDUSTRY_LABELS else DEFAULT_INDUSTRY


def category_multiplier(industry: str, category: str) -> float:
    return INDUSTRY_WEIGHTS.get(normalize_industry(industry), {}).get(category, 1.0)
