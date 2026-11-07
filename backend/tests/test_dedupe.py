import pytest

from backend.cost_engine.aggregate import Finding, aggregate, dedupe, labor_overlap
from backend.shared.benchmarks import Benchmarks


def make(detector, category, annual, metrics=None):
    return Finding(detector=detector, category=category, label=detector, annual=annual,
                   low=annual, high=annual,
                   scenario_annual={"low": annual, "typical": annual, "high": annual},
                   confidence=1.0, evidence="", metrics=metrics or {})


def test_waste_and_food_variance_count_once_keeping_the_larger():
    fs = [make("food_cost_variance", "inventory_waste", 30_000), make("waste", "inventory_waste", 20_000),
          make("overstock", "inventory_waste", 1_000)]
    values, notes = dedupe("inventory_waste", fs, "typical")
    assert values == {"food_cost_variance": 30_000, "waste": 0.0, "overstock": 1_000}
    assert len(notes) == 1
    assert aggregate(fs, Benchmarks.load())["inventory_waste"].annual == 31_000


def test_waste_kept_when_larger_than_variance():
    fs = [make("food_cost_variance", "inventory_waste", 5_000), make("waste", "inventory_waste", 12_000)]
    values, _ = dedupe("inventory_waste", fs, "typical")
    assert values == {"food_cost_variance": 0.0, "waste": 12_000}


def test_overtime_overlap_with_overstaffing_is_removed():
    over = make("overstaffing", "labor_scheduling", 15_000, {"excess_hours_annual": 1_000})
    ot = make("overtime", "labor_scheduling", 9_000,
              {"ot_hours_annual": 1_000, "ot_week_hour_share": 0.5})
    # 1,000 excess hours * 50% worked in OT weeks = 500 overlapping hours at $9/hr premium.
    assert labor_overlap(over, ot, "typical") == pytest.approx(4_500)
    values, notes = dedupe("labor_scheduling", [over, ot], "typical")
    assert values["overtime"] == pytest.approx(4_500)
    assert values["overstaffing"] == 15_000
    assert notes


def test_overlap_never_exceeds_overtime():
    over = make("overstaffing", "labor_scheduling", 50_000, {"excess_hours_annual": 10_000})
    ot = make("overtime", "labor_scheduling", 2_000, {"ot_hours_annual": 200, "ot_week_hour_share": 1.0})
    assert labor_overlap(over, ot, "typical") == pytest.approx(2_000)


def test_unrelated_categories_are_untouched():
    fs = [make("turnover", "hiring_retention", 10_000), make("unfilled_roles", "hiring_retention", 3_000)]
    values, notes = dedupe("hiring_retention", fs, "typical")
    assert sum(values.values()) == 13_000
    assert notes == []
