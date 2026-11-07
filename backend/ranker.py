"""Combine both engines into one pain score per category and build the report.

pain_score = alpha * cost_weight + (1 - alpha) * time_weight
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from backend.cost_engine.engine import CostResult, run_cost_engine
from backend.ingest.dataset import Dataset
from backend.shared.benchmarks import SCENARIOS, Benchmarks
from backend.shared.models import weighted_confidence
from backend.shared.taxonomy import CATEGORIES, DOMAIN_LABELS
from backend.time_engine.engine import TimeEngineResult, run_time_engine

DEFAULT_ALPHA = 0.7
MIN_CONFIDENCE = 0.3
TOP_N = 3


@dataclass
class Analysis:
    dataset: Dataset
    cost: CostResult
    time: TimeEngineResult


def analyze(ds: Dataset, bm: Benchmarks | None = None) -> Analysis:
    bm = bm or Benchmarks.load()
    return Analysis(ds, run_cost_engine(ds, bm), run_time_engine(ds, bm))


def pain_scores(a: Analysis, alpha: float, scenario: str = "typical") -> dict[str, float]:
    cw, tw = a.cost.weights(scenario), a.time.weights(scenario)
    return {c: alpha * cw.get(c, 0.0) + (1 - alpha) * tw.get(c, 0.0) for c in set(cw) | set(tw)}


def _confidence(a: Analysis, cat: str, alpha: float, cw: dict, tw: dict) -> float:
    weights, confs = [], []
    if cat in a.cost.categories:
        weights.append(alpha * cw.get(cat, 0.0))
        confs.append(a.cost.categories[cat].confidence)
    if cat in a.time.categories:
        weights.append((1 - alpha) * tw.get(cat, 0.0))
        confs.append(a.time.categories[cat].confidence)
    return weighted_confidence(weights, confs)


def _missing_for(a: Analysis, cat: str) -> list[dict]:
    seen, out = set(), []
    for s in a.cost.skipped + a.time.skipped:
        if s.category != cat:
            continue
        if s.missing_domains:
            for d in s.missing_domains:
                if d not in seen:
                    seen.add(d)
                    out.append({"domain": d, "file": DOMAIN_LABELS.get(d, d)})
        elif s.reason not in seen:
            seen.add(s.reason)
            out.append({"domain": None, "file": s.reason})
    return out


def _summary(label: str, cost: dict | None, time: dict | None, drivers: list[dict]) -> str:
    parts = []
    if cost and cost["annual"] > 0:
        pct = f" ({cost['pct_revenue']:.1f}% of revenue)" if cost.get("pct_revenue") is not None else ""
        parts.append(f"{label} is costing you about ${cost['annual']:,.0f} a year{pct}")
    if time and time["hours_per_week"] > 0:
        parts.append(f"{'and' if parts else label + ' takes'} about {time['hours_per_week']:.1f} hours a week")
    lead = (" ".join(parts) + ".") if parts else f"No measurable loss found for {label}."
    top = next((d for d in drivers if d.get("counted_annual", d.get("hours_per_week", 0)) > 0), None)
    return f"{lead} Biggest driver: {top['evidence']}" if top else lead


def build_report(a: Analysis, alpha: float = DEFAULT_ALPHA) -> dict:
    alpha = min(1.0, max(0.0, float(alpha)))
    ds = a.dataset
    cw, tw = a.cost.weights(), a.time.weights()
    pains = pain_scores(a, alpha)
    revenue = a.cost.annual_revenue

    problems = []
    for cat_id, pain in pains.items():
        meta = CATEGORIES[cat_id]
        cost_cat, time_cat = a.cost.categories.get(cat_id), a.time.categories.get(cat_id)
        cost = None
        if cost_cat:
            cost = {
                "annual": round(cost_cat.annual, 2),
                "recoverable": round(cost_cat.recoverable, 2),
                "range": [round(cost_cat.low, 2), round(cost_cat.high, 2)],
                "pct_revenue": round(cost_cat.annual / revenue * 100, 2) if revenue else None,
                "addressability": cost_cat.addressability,
                "confidence": round(cost_cat.confidence, 3),
                "weight": round(cw.get(cat_id, 0.0), 4),
                "dedupe_notes": cost_cat.dedupe_notes,
            }
        time = None
        if time_cat:
            time = {
                "hours_per_week": round(time_cat.hours_per_week, 2),
                "range": [round(time_cat.low, 2), round(time_cat.high, 2)],
                "by_role": time_cat.by_role,
                "opportunity_value_annual": round(time_cat.opportunity_value_annual, 2),
                "confidence": round(time_cat.confidence, 3),
                "weight": round(tw.get(cat_id, 0.0), 4),
                "notes": time_cat.notes,
            }
        drivers = ([{"engine": "cost", **d} for d in (cost_cat.drivers if cost_cat else [])]
                   + [{"engine": "time", **d} for d in (time_cat.drivers if time_cat else [])])
        problems.append({
            "category": cat_id,
            "label": meta.label,
            "description": meta.description,
            "pain_score": round(pain, 4),
            "cost": cost,
            "time": time,
            "confidence": round(_confidence(a, cat_id, alpha, cw, tw), 3),
            "drivers": drivers,
            "summary": _summary(meta.label, cost, time, drivers),
            "tool_tags": list(meta.tool_tags),
            "missing": _missing_for(a, cat_id),
        })
    problems.sort(key=lambda p: p["pain_score"], reverse=True)

    eligible = [p for p in problems if p["confidence"] >= MIN_CONFIDENCE and p["pain_score"] > 0]
    top = [{**p, "rank": i + 1} for i, p in enumerate(eligible[:TOP_N])]
    needs_more_data = [
        {"category": p["category"], "label": p["label"], "confidence": p["confidence"],
         "pain_score": p["pain_score"], "missing": _missing_for(a, p["category"])}
        for p in problems if p["confidence"] < MIN_CONFIDENCE and p["pain_score"] > 0
    ]
    not_assessed = [
        {"category": c, "label": CATEGORIES[c].label, "missing": _missing_for(a, c)}
        for c in CATEGORIES if c not in pains
    ]

    rankings = {}
    for s in SCENARIOS:
        sp = pain_scores(a, alpha, s)
        ok = [c for c in sorted(sp, key=sp.get, reverse=True)
              if sp[c] > 0 and _confidence(a, c, alpha, cw, tw) >= MIN_CONFIDENCE]
        rankings[s] = ok[:TOP_N]
    sets = {frozenset(r) for r in rankings.values()}
    sensitivity = {
        "rankings": rankings,
        "top3_stable": len(sets) == 1,
        "order_stable": len({tuple(r) for r in rankings.values()}) == 1,
        "note": "Each scenario sets every benchmark to its low, typical, or high value.",
    }

    total_cost = sum(c.annual for c in a.cost.categories.values())
    return jsonable({
        "restaurant": {
            "name": ds.profile.name,
            "annual_revenue": round(revenue, 2) if revenue else None,
            "period_start": ds.period_start.strftime("%Y-%m-%d") if ds.period_start is not None else None,
            "period_end": ds.period_end.strftime("%Y-%m-%d") if ds.period_end is not None else None,
            "period_days": ds.period_days,
        },
        "alpha": alpha,
        "top_problems": top,
        "all_problems": problems,
        "needs_more_data": needs_more_data,
        "not_assessed": not_assessed,
        "totals": {
            "annual_cost": round(total_cost, 2),
            "recoverable": round(sum(c.recoverable for c in a.cost.categories.values()), 2),
            "pct_revenue": round(total_cost / revenue * 100, 2) if revenue else None,
            "hours_per_week": round(sum(c.hours_per_week for c in a.time.categories.values()), 2),
        },
        "sensitivity": sensitivity,
        "data_quality": {d: q.to_dict() for d, q in ds.quality.items()},
        "files": [f.to_dict() for f in ds.files],
        "skipped": [s.to_dict() for s in a.cost.skipped + a.time.skipped],
    })


def jsonable(obj):
    """Recursively convert numpy scalars and NaN/inf into JSON-safe values."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        obj = obj.item()
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj
