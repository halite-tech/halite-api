"""FastAPI app for the comparison site.

Run:  uvicorn backend.api:app --reload
"""
from __future__ import annotations

import json
import uuid
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from fastapi.middleware.cors import CORSMiddleware

from backend.ingest.dataset import Dataset, Profile
from backend.ingest.loader import build_dataset
from backend.ranker import DEFAULT_ALPHA, Analysis, analyze, build_report
from backend.shared.taxonomy import CATEGORIES
from backend.synthetic.generate import generate
from backend.tool_rec.llmConnection import get_tools

app = FastAPI(title="AISB Restaurant Problem Engines", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# In-memory store; fine for a prototype, swap for a database in production.
DATASETS: dict[str, Dataset] = {}
ANALYSES: dict[str, Analysis] = {}

Alpha = Query(DEFAULT_ALPHA, ge=0.0, le=1.0, description="1.0 = rank by money only, 0.0 = by time only")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/taxonomy")
def taxonomy():
    return [c.to_dict() for c in CATEGORIES.values()]


@app.post("/uploads")
async def upload(files: list[UploadFile] = File(...), profile: str | None = Form(None)):
    try:
        prof = Profile.from_dict(json.loads(profile) if profile else None)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(400, f"profile must be a JSON object: {exc}")
    raw = [(f.filename or "upload.csv", await f.read()) for f in files]
    ds = build_dataset(raw, prof)
    if not ds.tables:
        raise HTTPException(422, {"error": "none of the files matched a known data type",
                                  "files": [r.to_dict() for r in ds.files]})
    dataset_id = uuid.uuid4().hex[:12]
    DATASETS[dataset_id] = ds
    return {
        "dataset_id": dataset_id,
        "domains": sorted(ds.tables),
        "files": [r.to_dict() for r in ds.files],
        "data_quality": {d: q.to_dict() for d, q in ds.quality.items()},
    }


@app.post("/analyze/{dataset_id}")
def run_analysis(dataset_id: str, alpha: float = Alpha):
    ds = DATASETS.get(dataset_id)
    if ds is None:
        raise HTTPException(404, "unknown dataset_id")
    ANALYSES[dataset_id] = analyze(ds)
    return build_report(ANALYSES[dataset_id], alpha)


@app.get("/report/{dataset_id}")
def report(dataset_id: str, alpha: float = Alpha):
    if dataset_id not in ANALYSES:
        if dataset_id in DATASETS:
            raise HTTPException(409, "dataset not analyzed yet; POST /analyze/{dataset_id} first")
        raise HTTPException(404, "unknown dataset_id")
    return build_report(ANALYSES[dataset_id], alpha)


@lru_cache(maxsize=1)
def _demo_analysis() -> Analysis:
    restaurant = generate()
    return analyze(build_dataset(restaurant.raw_files(), restaurant.profile))


@app.get("/demo")
def demo(alpha: float = Alpha):
    demo_report = build_report(_demo_analysis(), alpha)
    print("built report")
    return demo_report

@app.get("/tools")
def tools():
    try:
        build_report_json = demo(alpha=DEFAULT_ALPHA)
        tool_recs = get_tools(problems=build_report_json['top_problems'])
    except Exception as e:
        raise HTTPException(500, f"Error getting tools: {e}")
    return tool_recs