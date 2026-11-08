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
from backend.chat_stream.service import problem_brief, run_turn
from backend.ingest.dataset import Dataset, Profile
from backend.ingest.loader import build_dataset
from backend.ranker import DEFAULT_ALPHA, Analysis, analyze, build_report
from backend.shared.taxonomy import CATEGORIES
from backend.synthetic.generate import generate
from backend.shared.debug import debug, debug_trim
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


def _analysis_for_dataset(dataset_id: str) -> Analysis:
    if dataset_id == "demo":
        return _demo_analysis()
    if dataset_id in ANALYSES:
        return ANALYSES[dataset_id]
    if dataset_id in DATASETS:
        raise HTTPException(409, "dataset not analyzed yet; POST /analyze/{dataset_id} first")
    raise HTTPException(404, "unknown dataset_id")


def _problem_briefs_for_tools(report: dict) -> list[str]:
    briefs: list[str] = []
    for p in report.get("top_problems") or []:
        label = (p.get("label") or p.get("category") or "Problem").strip()
        summary = (p.get("summary") or "").strip()
        briefs.append(f"{label}: {summary}" if summary else label)
    return briefs


def _tool_recommendations(dataset_id: str, alpha: float, *, log_label: str) -> dict:
    """LLM tool recs from an analysed dataset (upload id or ``demo``)."""
    report = build_report(_analysis_for_dataset(dataset_id), alpha)
    problems = _problem_briefs_for_tools(report)
    if not problems:
        raise HTTPException(422, "no confident top problems to research; POST /analyze after upload")
    debug("api/tools", f"{log_label} dataset_id={dataset_id!r} alpha={alpha} briefs={len(problems)}")
    for i, brief in enumerate(problems):
        debug_trim("api/tools", f"  brief[{i}]: {brief}", 160)
    try:
        return get_tools(problems)
    except Exception as e:
        debug("api/tools", f"failed: {e}")
        raise HTTPException(500, f"Error getting tools: {e}") from e


@app.get("/tools")
def tools(
    dataset_id: str = Query(..., description="Analyzed upload id, or 'demo' for synthetic data"),
    alpha: float = Alpha,
):
    return _tool_recommendations(dataset_id, alpha, log_label="GET /tools")


@app.get("/demo/tools")
def demo_tools(alpha: float = Alpha):
    """AI tool recommendations for the synthetic **demo** restaurant (same data as ``GET /demo``).

    No upload or analyze step — long-running OpenRouter call (30–90+ s).
    Response shape matches ``GET /tools``.
    """
    return _tool_recommendations("demo", alpha, log_label="GET /demo/tools")

# --------------------------------------------------------------------------- grill-me chat
class ChatSessionIn(BaseModel):
    dataset_id: str | None = Field(None, description="analysed dataset (or 'demo') to ground the grilling in")


class ChatMessageIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


def _chat_context(dataset_id: str | None) -> str | None:
    if not dataset_id:
        return None
    analysis = _analysis_for_dataset(dataset_id)
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
    dataset_id = body.dataset_id if body else None
    session = chat_sessions.create_session(_chat_context(dataset_id))
    debug(
        "api/chat",
        f"POST /chat/sessions -> session_id={session.id} dataset_id={dataset_id!r} "
        f"context={'yes' if session.context else 'no'}",
    )
    return session.to_dict()


@app.get("/chat/sessions/{session_id}")
def get_chat_session(session_id: str):
    """Full transcript, current problem/clarity, and the latest tool recommendations (poll this)."""
    session = _chat_session_or_404(session_id)
    t = session.tools
    if t.status in ("ready", "error"):
        recs = (t.result or {}).get("recommendations", []) if isinstance(t.result, dict) else []
        brief = problem_brief(session)
        debug(
            "api/chat",
            f"GET /chat/sessions/{session_id} tools={t.status} "
            f"brief_match={t.problem == brief} recs={len(recs)} "
            f"err={repr(t.error) if t.error else None}",
        )
    return session.to_dict()


@app.delete("/chat/sessions/{session_id}")
def delete_chat_session(session_id: str):
    if not chat_sessions.delete_session(session_id):
        raise HTTPException(404, "unknown session_id")
    return {"ok": True}


@app.post("/chat/sessions/{session_id}/messages")
async def chat_message(session_id: str, body: ChatMessageIn):
    """Send an owner message; replies as a server-sent-event stream.

    Events: state, tools (running then ready/error), notice (interim "Looking for tools…"),
    token* (assistant follow-up after tools finish when research ran this turn), done | error.
    """
    session = _chat_session_or_404(session_id)
    try:
        api_key()
    except OpenRouterError as exc:
        raise HTTPException(500, str(exc))
    if session.lock.locked():
        raise HTTPException(409, "previous message is still being answered")

    debug_trim(
        "api/chat",
        f"POST /chat/sessions/{session_id}/messages problem={session.problem!r} "
        f"tools={session.tools.status} msg={body.message.strip()!r}",
        200,
    )

    async def sse():
        async with session.lock:
            async for evt in run_turn(session, body.message.strip()):
                yield f"event: {evt['event']}\ndata: {json.dumps(evt['data'])}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
