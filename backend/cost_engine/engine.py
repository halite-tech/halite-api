"""Cost engine: run every detector across benchmark scenarios, then aggregate."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.cost_engine.aggregate import CategoryCost, Finding, aggregate
from backend.cost_engine.detectors import REGISTRY, Detector
from backend.ingest.dataset import Dataset, MissingData
from backend.shared.benchmarks import SCENARIOS, Benchmarks
from backend.shared.metrics import annual_revenue
from backend.shared.models import Skipped, normalize_weights

log = logging.getLogger(__name__)


@dataclass
class CostResult:
    findings: list[Finding]
    skipped: list[Skipped]
    categories: dict[str, CategoryCost]
    annual_revenue: float | None

    def weights(self, scenario: str = "typical") -> dict[str, float]:
        """cost_weight = adjusted cost / sum of adjusted costs."""
        return normalize_weights({
            c: cat.scenario_annual[scenario] * cat.confidence * cat.addressability
            for c, cat in self.categories.items()
        })

    def finding(self, name: str) -> Finding | None:
        return next((f for f in self.findings if f.detector == name), None)


def run_detector(det: Detector, ds: Dataset, bm: Benchmarks) -> Finding | Skipped:
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
        except Exception as exc:  # one bad detector must not sink the report
            log.exception("detector %s failed", det.name)
            if s == "typical":
                return Skipped(det.name, det.category, det.label, f"could not compute: {exc}")
            results[s] = None

    typical = results["typical"]
    scenario_annual = {s: max(0.0, (r or typical).annual) for s, r in results.items()}
    return Finding(
        detector=det.name, category=det.category, label=det.label,
        annual=scenario_annual["typical"],
        low=min(scenario_annual.values()), high=max(scenario_annual.values()),
        scenario_annual=scenario_annual,
        confidence=ds.confidence(*det.requires) * det.intrinsic_confidence,
        evidence=typical.evidence, metrics=typical.metrics, rows=typical.rows,
    )


def run_cost_engine(ds: Dataset, bm: Benchmarks | None = None) -> CostResult:
    bm = bm or Benchmarks.load()
    findings, skipped = [], []
    for det in REGISTRY:
        out = run_detector(det, ds, bm)
        (findings if isinstance(out, Finding) else skipped).append(out)
    return CostResult(findings, skipped, aggregate(findings, bm), annual_revenue(ds))
