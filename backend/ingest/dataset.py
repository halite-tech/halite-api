"""The normalized, quality-scored dataset that every detector reads from."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from backend.ingest.quality import DomainQuality
from backend.ingest.schemas import SCHEMAS, FileReport

DAYS_PER_YEAR = 365


class MissingData(Exception):
    """A detector cannot run because a domain, column, or profile field is missing."""

    def __init__(self, message: str, domains: tuple[str, ...] = ()):
        super().__init__(message)
        self.domains = domains


@dataclass
class Profile:
    name: str = "My Restaurant"
    seats: int | None = None
    sqft: float | None = None
    owner_hourly_rate: float = 45.0
    manager_hourly_rate: float = 28.0
    timezone: str | None = None

    @classmethod
    def from_dict(cls, data: dict | None) -> "Profile":
        if not data:
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Dataset:
    tables: dict[str, pd.DataFrame]
    quality: dict[str, DomainQuality]
    files: list[FileReport]
    profile: Profile
    period_start: pd.Timestamp | None
    period_end: pd.Timestamp | None
    cache: dict = field(default_factory=dict)

    @property
    def period_days(self) -> int:
        if self.period_start is None or self.period_end is None:
            return DAYS_PER_YEAR
        return int((self.period_end.normalize() - self.period_start.normalize()).days) + 1

    def has(self, domain: str, *cols: str) -> bool:
        df = self.tables.get(domain)
        if df is None or df.empty:
            return False
        return all(c in df.columns and df[c].notna().any() for c in cols)

    def require(self, domain: str, *cols: str) -> pd.DataFrame:
        df = self.tables.get(domain)
        if df is None or df.empty:
            raise MissingData(f"no {domain} data uploaded", (domain,))
        missing = [c for c in cols if c not in df.columns or df[c].isna().all()]
        if missing:
            raise MissingData(f"{domain} data is missing columns: {', '.join(missing)}", (domain,))
        return df

    def confidence(self, *domains: str) -> float:
        scores = [self.quality[d].score for d in domains if d in self.quality]
        return min(scores) if scores else 0.0

    def window(self, domain: str, *cols: str) -> tuple[pd.DataFrame, int]:
        """Rows used for annualization and the number of days they represent.

        With 12+ months of data the most recent 365 days are used so the
        estimate covers one full seasonal cycle; otherwise the whole span is
        linearly scaled. Sparse event logs use the overall analysis period.
        """
        df = self.require(domain, *cols)
        schema = SCHEMAS[domain]
        date_col = schema.date_col
        if not date_col or date_col not in df.columns or df[date_col].isna().all():
            return df, self.period_days
        dates = df[date_col].dt.normalize()
        end = dates.max()
        if not schema.sparse and (end - dates.min()).days + 1 >= DAYS_PER_YEAR:
            start = end - pd.Timedelta(days=DAYS_PER_YEAR - 1)
            return df[dates >= start], DAYS_PER_YEAR
        if schema.sparse:
            if self.period_start is not None:
                mask = (dates >= self.period_start.normalize()) & (dates <= self.period_end.normalize())
                return df[mask], self.period_days
            return df, self.period_days
        return df, int((end - dates.min()).days) + 1

    @staticmethod
    def annualize(value: float, days: int) -> float:
        return float(value) * DAYS_PER_YEAR / max(int(days), 1)
