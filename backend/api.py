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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.chat_stream import session as chat_sessions
from backend.chat_stream.openrouter import OpenRouterError, api_key
from backend.chat_stream.service import run_turn
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

# --------------------------------------------------------------------------- grill-me chat
class ChatSessionIn(BaseModel):
    dataset_id: str | None = Field(None, description="analysed dataset (or 'demo') to ground the grilling in")


class ChatMessageIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


def _chat_context(dataset_id: str | None) -> str | None:
    if not dataset_id:
        return None
    if dataset_id == "demo":
        analysis = _demo_analysis()
    elif dataset_id in ANALYSES:
        analysis = ANALYSES[dataset_id]
    else:
        raise HTTPException(404, "unknown or un-analyzed dataset_id")
    rep = build_report(analysis)
    lines = [f"Restaurant: {rep['restaurant']['name']}"]
    lines += [f"- {p['summary']}" for p in rep["top_problems"]]
    return "\n".join(lines)


def _chat_session_or_404(session_id: str) -> chat_sessions.ChatSession:
    session = chat_sessions.get_session(session_id)
    if session is None:
        raise HTTPException(404, "unknown session_id")
    return session


@app.post("/chat/sessions")
def create_chat_session(body: ChatSessionIn | None = None):
    session = chat_sessions.create_session(_chat_context(body.dataset_id if body else None))
    return session.to_dict()


@app.get("/chat/sessions/{session_id}")
def get_chat_session(session_id: str):
    """Full transcript, current problem/clarity, and the latest tool recommendations (poll this)."""
    return _chat_session_or_404(session_id).to_dict()


@app.delete("/chat/sessions/{session_id}")
def delete_chat_session(session_id: str):
    if not chat_sessions.delete_session(session_id):
        raise HTTPException(404, "unknown session_id")
    return {"ok": True}


@app.post("/chat/sessions/{session_id}/messages")
async def chat_message(session_id: str, body: ChatMessageIn):
    """Send an owner message; replies as a server-sent-event stream.

    Events: state (problem/clarity/action), tools (research kicked off), token*, done | error.
    """
    session = _chat_session_or_404(session_id)
    try:
        api_key()
    except OpenRouterError as exc:
        raise HTTPException(500, str(exc))
    if session.lock.locked():
        raise HTTPException(409, "previous message is still being answered")

    async def sse():
        async with session.lock:
            async for evt in run_turn(session, body.message.strip()):
                yield f"event: {evt['event']}\ndata: {json.dumps(evt['data'])}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
