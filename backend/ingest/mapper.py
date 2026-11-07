"""Detect which domain a file belongs to and map its headers to canonical columns.

Mapping order: exact synonym match, then fuzzy match (difflib), then an
optional LLM fallback that is only used when ``AISB_LLM_MAPPING=1`` and
``OPENAI_API_KEY`` are set. The LLM only ever proposes column names; it never
touches values.
"""
from __future__ import annotations

import difflib
import json
import os
import re

from backend.ingest.schemas import SCHEMAS, SYNONYMS, Schema

ACCEPT_THRESHOLD = 0.6


def normalize_header(header: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(header).lower())
    return re.sub(r"\s+", " ", text).strip()


def _candidates(col: str) -> set[str]:
    return {normalize_header(col)} | set(SYNONYMS.get(col, ()))


def map_headers(headers: list[str], schema: Schema) -> dict[str, str]:
    """Return {original_header: canonical_column} for one schema."""
    norm = {h: normalize_header(h) for h in headers}
    mapping: dict[str, str] = {}
    used: set[str] = set()

    for col in schema.columns:
        cands = _candidates(col)
        for h in headers:
            if h not in mapping and norm[h] in cands:
                mapping[h] = col
                used.add(col)
                break

    for h in headers:
        if h in mapping:
            continue
        best, best_ratio = None, 0.0
        for col in schema.columns:
            if col in used:
                continue
            for cand in _candidates(col):
                ratio = difflib.SequenceMatcher(None, norm[h], cand).ratio()
                if ratio > best_ratio:
                    best, best_ratio = col, ratio
        if best and best_ratio >= 0.85:
            mapping[h] = best
            used.add(best)
    return mapping


def _score(mapping: dict[str, str], schema: Schema, filename: str) -> float:
    mapped = set(mapping.values())
    req = sum(c in mapped for c in schema.required) / len(schema.required)
    opt = sum(c in mapped for c in schema.optional)
    fname = filename.lower()
    hint = any(h in fname for h in schema.filename_hints)
    return req * 10 + opt * 0.5 + (1.5 if hint else 0.0)


def detect_domain(headers: list[str], filename: str) -> tuple[Schema | None, dict[str, str], float]:
    best: tuple[Schema | None, dict[str, str], float] = (None, {}, 0.0)
    for schema in SCHEMAS.values():
        mapping = map_headers(headers, schema)
        mapped = set(mapping.values())
        req_frac = sum(c in mapped for c in schema.required) / len(schema.required)
        if req_frac < ACCEPT_THRESHOLD:
            continue
        score = _score(mapping, schema, filename)
        if score > best[2]:
            best = (schema, mapping, score)

    schema, mapping, score = best
    if schema is not None and _llm_enabled():
        unmapped = [h for h in headers if h not in mapping]
        missing = [c for c in schema.columns if c not in mapping.values()]
        if unmapped and missing:
            mapping.update(llm_map_columns(unmapped, missing, schema.domain))
    return schema, mapping, score


def _llm_enabled() -> bool:
    return os.environ.get("AISB_LLM_MAPPING") == "1" and bool(os.environ.get("OPENAI_API_KEY"))


def llm_map_columns(headers: list[str], columns: list[str], domain: str) -> dict[str, str]:
    """Ask an LLM to map leftover headers onto still-missing canonical columns."""
    import httpx

    prompt = (
        f"Restaurant '{domain}' export. Map each header to one of these canonical columns "
        f"or null if none fit. Canonical: {columns}. Headers: {headers}. "
        'Reply with JSON only: {"header": "canonical_or_null"}.'
    )
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={
                "model": os.environ.get("AISB_LLM_MODEL", "gpt-4o-mini"),
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=20,
        )
        resp.raise_for_status()
        proposed = json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception:
        return {}
    allowed = set(columns)
    out: dict[str, str] = {}
    for header, col in proposed.items():
        if header in headers and col in allowed and col not in out.values():
            out[header] = col
    return out
