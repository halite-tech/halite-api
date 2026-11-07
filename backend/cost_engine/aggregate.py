"""Roll detector findings up into problem categories without double counting."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from backend.shared.benchmarks import SCENARIOS, Benchmarks
from backend.shared.models import weighted_confidence

# Detectors in a group measure overlapping dollars; only the largest counts.
# Waste is a subset of food cost variance (wasted food is part of actual usage).
MAX_GROUPS: dict[str, list[tuple[str, ...]]] = {
    "inventory_waste": [("food_cost_variance", "waste")],
}


@dataclass
class Finding:
    detector: str
    category: str
    label: str
    annual: float
    low: float
    high: float
    scenario_annual: dict[str, float]
    confidence: float
    evidence: str
    metrics: dict = field(default_factory=dict)
    rows: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "detector": self.detector, "label": self.label, "annual": round(self.annual, 2),
            "range": [round(self.low, 2), round(self.high, 2)],
            "confidence": round(self.confidence, 3), "evidence": self.evidence,
            "metrics": _round(self.metrics), "rows": self.rows,
        }


@dataclass
class CategoryCost:
    category: str
    annual: float
    low: float
    high: float
    scenario_annual: dict[str, float]
    confidence: float
    addressability: float
    drivers: list[dict]
    dedupe_notes: list[str]

    @property
    def recoverable(self) -> float:
        return self.annual * self.addressability

    @property
    def adjusted(self) -> float:
        return self.annual * self.confidence * self.addressability


def labor_overlap(overstaffing: Finding, overtime: Finding, scenario: str) -> float:
    """Overtime premium that disappears once excess hours are cut.

    Only the share of excess hours worked by employees in overtime weeks can
    remove overtime, and never more than the overtime hours themselves.
    """
    ot_hours = overtime.metrics.get("ot_hours_annual", 0.0)
    ot_cost = overtime.scenario_annual[scenario]
    if ot_hours <= 0 or ot_cost <= 0:
        return 0.0
    excess = overstaffing.metrics.get("excess_hours_annual", 0.0)
    share = overtime.metrics.get("ot_week_hour_share", 0.0)
    overlap_hours = min(ot_hours, excess * share)
    return min(ot_cost, overlap_hours * ot_cost / ot_hours)


def dedupe(category: str, findings: list[Finding], scenario: str) -> tuple[dict[str, float], list[str]]:
    values = {f.detector: f.scenario_annual[scenario] for f in findings}
    by_name = {f.detector: f for f in findings}
    notes: list[str] = []
    for group in MAX_GROUPS.get(category, []):
        present = [d for d in group if d in values]
        if len(present) > 1:
            keep = max(present, key=lambda d: values[d])
            for d in present:
                if d != keep and values[d] > 0:
                    notes.append(f"'{by_name[d].label}' overlaps '{by_name[keep].label}'; counted once.")
                    values[d] = 0.0
    if category == "labor_scheduling" and {"overstaffing", "overtime"} <= by_name.keys():
        overlap = labor_overlap(by_name["overstaffing"], by_name["overtime"], scenario)
        if overlap > 0:
            values["overtime"] -= overlap
            notes.append(f"${overlap:,.0f} of overtime premium would disappear by cutting excess hours; "
                         "not counted twice.")
    return values, notes


def aggregate(findings: list[Finding], bm: Benchmarks) -> dict[str, CategoryCost]:
    by_cat: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_cat[f.category].append(f)

    out: dict[str, CategoryCost] = {}
    for cat, fs in by_cat.items():
        totals = {}
        for s in SCENARIOS:
            values, _ = dedupe(cat, fs, s)
            totals[s] = sum(values.values())
        counted, notes = dedupe(cat, fs, "typical")
        drivers = sorted(
            ({**f.to_dict(), "counted_annual": round(counted[f.detector], 2)} for f in fs),
            key=lambda d: d["counted_annual"], reverse=True,
        )
        out[cat] = CategoryCost(
            category=cat,
            annual=totals["typical"],
            low=min(totals.values()),
            high=max(totals.values()),
            scenario_annual=totals,
            confidence=weighted_confidence([f.annual for f in fs], [f.confidence for f in fs]),
            addressability=bm.addressability(cat),
            drivers=drivers,
            dedupe_notes=notes,
        )
    return out


def _round(obj):
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v) for v in obj]
    if hasattr(obj, "item"):
        return _round(obj.item())
    return obj
