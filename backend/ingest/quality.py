"""Per-domain data quality score in [0, 1]; it becomes detector confidence."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from backend.ingest.schemas import Schema

FULL_COVERAGE_DAYS = 90
WEIGHTS = {"completeness": 0.5, "coverage": 0.3, "consistency": 0.2}


@dataclass
class DomainQuality:
    domain: str
    rows: int
    completeness: float
    coverage: float
    consistency: float
    span_days: int | None

    @property
    def score(self) -> float:
        return (
            WEIGHTS["completeness"] * self.completeness
            + WEIGHTS["coverage"] * self.coverage
            + WEIGHTS["consistency"] * self.consistency
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update({k: round(v, 3) for k, v in d.items() if isinstance(v, float)})
        d["score"] = round(self.score, 3)
        return d


def _checks(domain: str, df: pd.DataFrame) -> pd.Series:
    """Row-level consistency checks; True means the row passes."""
    ok = pd.Series(True, index=df.index)

    def col(name):
        return df[name] if name in df.columns else None

    if domain == "purchases":
        if col("qty_ordered") is not None:
            ok &= ~(df["qty_received"] > 2 * df["qty_ordered"])
        ok &= df["unit_price"].fillna(1) > 0
    elif domain == "sales":
        ok &= df["unit_price"].fillna(0) >= 0
        if col("ticket_open") is not None and col("ticket_close") is not None:
            ok &= ~(df["ticket_close"] < df["ticket_open"])
    elif domain == "timeclock":
        hours = (df["clock_out"] - df["clock_in"]).dt.total_seconds() / 3600
        ok &= hours.between(0, 16) | hours.isna()
        ok &= df["wage"].between(5, 150) | df["wage"].isna()
    elif domain == "inventory":
        ok &= df["on_hand_qty"].fillna(0) >= 0
    elif domain == "employees":
        if col("termination_date") is not None:
            ok &= ~(df["termination_date"] < df["hire_date"])
    elif domain == "cash":
        ok &= df["expected"].fillna(1) >= 0
    elif domain == "reviews":
        ok &= df["rating"].between(0, 5) | df["rating"].isna()
    return ok


def assess(domain: str, df: pd.DataFrame, schema: Schema, period_days: int | None) -> DomainQuality:
    n = len(df)
    if n == 0:
        return DomainQuality(domain, 0, 0.0, 0.0, 0.0, None)

    present = [df[c].notna().mean() if c in df.columns else 0.0 for c in schema.required]
    completeness = float(sum(present) / len(schema.required))

    span = None
    if schema.date_col and schema.date_col in df.columns and df[schema.date_col].notna().any():
        dates = df[schema.date_col].dropna().dt.normalize()
        span = int((dates.max() - dates.min()).days) + 1
    if schema.sparse or span is None:
        coverage_days = period_days if period_days else FULL_COVERAGE_DAYS
    else:
        coverage_days = span
    coverage = min(1.0, coverage_days / FULL_COVERAGE_DAYS)

    try:
        consistency = float(_checks(domain, df).mean())
    except (KeyError, TypeError):
        consistency = 0.5
    return DomainQuality(domain, n, completeness, coverage, consistency, span)
