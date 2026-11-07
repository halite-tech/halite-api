"""Type coercion, unit conversion, and de-duplication for mapped tables."""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.ingest.schemas import COLUMN_TYPES, UNIT_CONVERSIONS, Schema

_TRUE = {"true", "t", "yes", "y", "1", "x", "void", "comp", "voided", "comped"}


def _to_number(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return s.astype(float)
    cleaned = (
        s.astype(str)
        .str.replace(r"[\$,%\s]", "", regex=True)
        .str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    )
    return pd.to_numeric(cleaned, errors="coerce")


def _to_datetime(s: pd.Series, timezone: str | None) -> pd.Series:
    out = pd.to_datetime(s, errors="coerce", format="mixed")
    if getattr(out.dt, "tz", None) is not None:
        out = out.dt.tz_convert(timezone or "UTC").dt.tz_localize(None)
    return out


def _to_bool(s: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(s):
        return s
    return s.fillna("").astype(str).str.strip().str.lower().isin(_TRUE)


def coerce_types(df: pd.DataFrame, timezone: str | None = None) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        kind = COLUMN_TYPES.get(col)
        if kind == "number":
            df[col] = _to_number(df[col])
        elif kind == "datetime":
            df[col] = _to_datetime(df[col], timezone)
        elif kind == "bool":
            df[col] = _to_bool(df[col])
        elif kind == "str":
            df[col] = df[col].where(df[col].isna(), df[col].astype(str).str.strip())
    return df


def convert_units(df: pd.DataFrame, schema: Schema) -> pd.DataFrame:
    """Convert quantities to base units (lb, l, each) and rescale per-unit prices."""
    if "unit" not in df.columns or not schema.qty_cols:
        return df
    df = df.copy()
    units = df["unit"].fillna("").astype(str).str.strip().str.lower()
    factor = units.map(lambda u: UNIT_CONVERSIONS.get(u, (u, 1.0))[1]).astype(float)
    base = units.map(lambda u: UNIT_CONVERSIONS.get(u, (u, 1.0))[0])
    for col in schema.qty_cols:
        if col in df.columns:
            df[col] = df[col] * factor
    for col in schema.per_unit_cols:
        if col in df.columns:
            df[col] = df[col] / factor.replace(0, np.nan)
    df["unit"] = base.replace("", np.nan)
    return df


def drop_cross_file_duplicates(frames: list[pd.DataFrame]) -> list[pd.DataFrame]:
    """Drop rows that already appeared in an earlier file of the same domain.

    Repeated rows inside one file are kept: two identical pizzas on one order
    or two identical reservations are real, separate events.
    """
    seen: set[int] = set()
    out = []
    for df in frames:
        data_cols = sorted(c for c in df.columns if c != "_row")
        hashes = pd.util.hash_pandas_object(
            df[data_cols].astype(str), index=False).to_numpy()
        keep = ~pd.Series(hashes).isin(seen).to_numpy()
        out.append(df[keep])
        seen.update(hashes.tolist())
    return out


def normalize_table(df: pd.DataFrame, schema: Schema, timezone: str | None = None) -> pd.DataFrame:
    df = coerce_types(df, timezone)
    df = convert_units(df, schema)
    return df.reset_index(drop=True)
