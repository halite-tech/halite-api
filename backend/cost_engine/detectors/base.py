"""Detector registry. A detector measures one way a restaurant loses money.

Each detector function receives the dataset and a scenario-specific
``Benchmarks`` and returns a ``Result`` with the *annual excess cost*: actual
cost minus the cost at an achievable target. It raises ``MissingData`` when it
cannot run. The engine calls it once per scenario to build the low-high range.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from backend.ingest.dataset import Dataset
from backend.shared.benchmarks import Benchmarks


@dataclass
class Result:
    annual: float
    evidence: str
    metrics: dict = field(default_factory=dict)
    rows: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Detector:
    name: str
    category: str
    label: str
    requires: tuple[str, ...]
    intrinsic_confidence: float
    fn: Callable[[Dataset, Benchmarks], Result]


REGISTRY: list[Detector] = []


def detector(name: str, category: str, label: str, requires: tuple[str, ...],
             confidence: float = 1.0):
    def wrap(fn):
        REGISTRY.append(Detector(name, category, label, requires, confidence, fn))
        return fn
    return wrap


def sample_rows(df, n: int = 5) -> list[str]:
    if df is None or "_row" not in df.columns:
        return []
    return df["_row"].head(n).tolist()


def money(x: float) -> str:
    return f"${x:,.0f}"
