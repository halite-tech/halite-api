"""Time engine: hours lost per week by problem category and role."""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from backend.ingest.dataset import Dataset, MissingData
from backend.shared.benchmarks import SCENARIOS, Benchmarks
from backend.shared.models import Skipped, normalize_weights, weighted_confidence
from backend.time_engine.detectors import ADMIN, TIME_REGISTRY, TimeDetector

log = logging.getLogger(__name__)
WEEKS_PER_YEAR = 52


@dataclass
class TimeFinding:
    detector: str
    category: str
    label: str
    role: str
    kind: str
    hours_per_week: float
    low: float
    high: float
    scenario_hours: dict[str, float]
    current_hours_per_week: float
    confidence: float
    addressability: float
    priced_in_cost: bool
    opportunity_value_annual: float
    evidence: str
    metrics: dict = field(default_factory=dict)

    def adjusted(self, scenario: str = "typical") -> float:
        return self.scenario_hours[scenario] * self.confidence * self.addressability

    def to_dict(self) -> dict:
        return {
            "detector": self.detector, "label": self.label, "role": self.role, "kind": self.kind,
            "hours_per_week": round(self.hours_per_week, 2),
            "range": [round(self.low, 2), round(self.high, 2)],
            "current_hours_per_week": round(self.current_hours_per_week, 2),
            "confidence": round(self.confidence, 3), "priced_in_cost": self.priced_in_cost,
            "opportunity_value_annual": round(self.opportunity_value_annual, 2),
            "evidence": self.evidence,
        }


@dataclass
class TimeCategory:
    category: str
    hours_per_week: float
    low: float
    high: float
    scenario_hours: dict[str, float]
    by_role: dict[str, float]
    confidence: float
    opportunity_value_annual: float
    drivers: list[dict]
    notes: list[str]
    scenario_adjusted: dict[str, float]


@dataclass
class TimeEngineResult:
    findings: list[TimeFinding]
    skipped: list[Skipped]
    categories: dict[str, TimeCategory]

    def weights(self, scenario: str = "typical") -> dict[str, float]:
        """time_weight = (hours lost x confidence x addressability) / sum."""
        return normalize_weights({c: cat.scenario_adjusted[scenario] for c, cat in self.categories.items()})


def _role_rate(ds: Dataset, role: str) -> float:
    if role == "owner":
        return ds.profile.owner_hourly_rate
    if role == "manager":
        return ds.profile.manager_hourly_rate
    if ds.has("timeclock", "wage"):
        return float(ds.tables["timeclock"]["wage"].mean())
    return 15.0


def run_time_detector(det: TimeDetector, ds: Dataset, bm: Benchmarks) -> TimeFinding | Skipped:
    missing = [d for d in det.requires if d not in ds.tables]
    if missing:
        return Skipped(det.name, det.category, det.label, f"missing data: {', '.join(missing)}", missing)
    results = {}
    for s in SCENARIOS:
        try:
            results[s] = det.fn(ds, bm.with_scenario(s))
        except MissingData as exc:
            if s == "typical":
                return Skipped(det.name, det.category, det.label, str(exc), list(exc.domains))
            results[s] = None
        except Exception as exc:
            log.exception("time detector %s failed", det.name)
            if s == "typical":
                return Skipped(det.name, det.category, det.label, f"could not compute: {exc}")
            results[s] = None

    typical = results["typical"]
    hours = {s: max(0.0, (r or typical).hours_lost_week) for s, r in results.items()}
    # Admin targets already encode what a tool removes, so no further discount.
    addressability = 1.0 if det.kind == ADMIN else bm.addressability(det.category)
    opportunity = 0.0
    if det.kind == ADMIN and not det.priced_in_cost:
        opportunity = hours["typical"] * WEEKS_PER_YEAR * _role_rate(ds, det.role)
    return TimeFinding(
        detector=det.name, category=det.category, label=det.label, role=det.role, kind=det.kind,
        hours_per_week=hours["typical"], low=min(hours.values()), high=max(hours.values()),
        scenario_hours=hours, current_hours_per_week=typical.current_hours_week,
        confidence=ds.confidence(*det.requires) * det.intrinsic_confidence,
        addressability=addressability, priced_in_cost=det.priced_in_cost,
        opportunity_value_annual=opportunity, evidence=typical.evidence, metrics=typical.metrics,
    )


def aggregate_time(findings: list[TimeFinding]) -> dict[str, TimeCategory]:
    by_cat: dict[str, list[TimeFinding]] = defaultdict(list)
    for f in findings:
        by_cat[f.category].append(f)
    out = {}
    for cat, fs in by_cat.items():
        totals = {s: sum(f.scenario_hours[s] for f in fs) for s in SCENARIOS}
        by_role: dict[str, float] = defaultdict(float)
        for f in fs:
            by_role[f.role] += f.hours_per_week
        notes = [f"'{f.label}' is already priced in the cost estimate; shown as hours only."
                 for f in fs if f.priced_in_cost and f.hours_per_week > 0]
        out[cat] = TimeCategory(
            category=cat,
            hours_per_week=totals["typical"],
            low=min(totals.values()), high=max(totals.values()),
            scenario_hours=totals,
            by_role={r: round(float(h), 2) for r, h in by_role.items()},
            confidence=weighted_confidence([f.hours_per_week for f in fs], [f.confidence for f in fs]),
            opportunity_value_annual=sum(f.opportunity_value_annual for f in fs),
            drivers=sorted((f.to_dict() for f in fs), key=lambda d: d["hours_per_week"], reverse=True),
            notes=notes,
            scenario_adjusted={s: sum(f.adjusted(s) for f in fs) for s in SCENARIOS},
        )
    return out


def run_time_engine(ds: Dataset, bm: Benchmarks | None = None) -> TimeEngineResult:
    bm = bm or Benchmarks.load()
    findings, skipped = [], []
    for det in TIME_REGISTRY:
        out = run_time_detector(det, ds, bm)
        (findings if isinstance(out, TimeFinding) else skipped).append(out)
    return TimeEngineResult(findings, skipped, aggregate_time(findings))
