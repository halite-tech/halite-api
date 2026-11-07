"""Entry point: raw uploaded files -> quality-scored ``Dataset``."""
from __future__ import annotations

import io
from collections import defaultdict
from pathlib import Path

import pandas as pd

from backend.ingest.dataset import Dataset, Profile
from backend.ingest.mapper import detect_domain
from backend.ingest.normalize import drop_cross_file_duplicates, normalize_table
from backend.ingest.quality import assess
from backend.ingest.schemas import SCHEMAS, FileReport

RawFile = tuple[str, "bytes | pd.DataFrame"]


def read_raw(filename: str, payload) -> list[tuple[str, pd.DataFrame]]:
    """Parse CSV/Excel bytes into one or more string-typed frames."""
    if isinstance(payload, pd.DataFrame):
        return [(filename, payload.astype(object))]
    suffix = Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        sheets = pd.read_excel(io.BytesIO(payload), sheet_name=None, dtype=str)
        return [(f"{filename}:{name}", df) for name, df in sheets.items()]
    return [(filename, pd.read_csv(io.BytesIO(payload), dtype=str, skipinitialspace=True))]


def _period(tables: dict[str, pd.DataFrame]) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    if "sales" in tables and tables["sales"]["timestamp"].notna().any():
        ts = tables["sales"]["timestamp"].dropna()
        return ts.min().normalize(), ts.max().normalize()
    starts, ends = [], []
    for domain, df in tables.items():
        schema = SCHEMAS[domain]
        if schema.sparse or not schema.date_col or schema.date_col not in df.columns:
            continue
        dates = df[schema.date_col].dropna()
        if not dates.empty:
            starts.append(dates.min().normalize())
            ends.append(dates.max().normalize())
    if not starts:
        return None, None
    return pd.Series(starts).median().normalize(), pd.Series(ends).median().normalize()


def build_dataset(files: list[RawFile], profile: Profile | None = None) -> Dataset:
    profile = profile or Profile()
    reports: list[FileReport] = []
    parts: dict[str, list[pd.DataFrame]] = defaultdict(list)

    for filename, payload in files:
        try:
            frames = read_raw(filename, payload)
        except Exception as exc:  # unreadable upload: report it, keep going
            reports.append(FileReport(filename, None, 0, note=f"could not read file: {exc}"))
            continue
        for name, raw in frames:
            raw = raw.dropna(how="all")
            headers = [str(h) for h in raw.columns]
            schema, mapping, score = detect_domain(headers, name)
            if schema is None:
                reports.append(FileReport(name, None, len(raw), unmapped=headers,
                                          note="could not match this file to a known data type"))
                continue
            mapped = raw[list(mapping)].rename(columns=mapping)
            mapped["_row"] = [f"{name}:{i + 2}" for i in range(len(mapped))]
            parts[schema.domain].append(mapped)
            reports.append(FileReport(
                name, schema.domain, len(raw), mapping=mapping,
                unmapped=[h for h in headers if h not in mapping],
                missing_required=[c for c in schema.required if c not in mapping.values()],
                match_score=score,
            ))

    tables: dict[str, pd.DataFrame] = {}
    for domain, frames in parts.items():
        combined = pd.concat(drop_cross_file_duplicates(frames), ignore_index=True)
        tables[domain] = normalize_table(combined, SCHEMAS[domain], profile.timezone)

    start, end = _period(tables)
    period_days = int((end - start).days) + 1 if start is not None else None
    quality = {d: assess(d, df, SCHEMAS[d], period_days) for d, df in tables.items()}
    return Dataset(tables, quality, reports, profile, start, end)


def build_dataset_from_dir(path: str | Path, profile: Profile | None = None) -> Dataset:
    files = [(p.name, p.read_bytes()) for p in sorted(Path(path).iterdir())
             if p.suffix.lower() in (".csv", ".xlsx", ".xls", ".xlsm")]
    return build_dataset(files, profile)
