"""FastAPI app for the comparison site.

Run:  uvicorn backend.api:app --reload
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.chat_stream import session as chat_sessions
from backend.auth import current_user_id
from backend.chat_stream.openrouter import OpenRouterError, api_key
from backend.chat_stream.service import problem_brief, run_turn
from backend.ingest.dataset import Dataset, Profile
from backend.ingest.loader import build_dataset
from backend.ranker import DEFAULT_ALPHA, Analysis, analyze, build_report
from backend.shared.industries import DEFAULT_INDUSTRY, INDUSTRY_LABELS
from backend.shared.taxonomy import CATEGORIES
from backend.synthetic.generate import generate
from backend.shared.debug import debug, debug_trim
from backend.tool_rec.llmConnection import get_tools

def _patch_uvicorn_keep_alive(seconds: int = 300) -> None:
    """Tunnels reuse sockets to this process. Uvicorn's default 5s idle timeout closes them
    while the owner is typing; the next POST is written to a dead socket and never arrives.
    Patch the protocol so keep-alive outlasts a typical pause between chat messages.
    """
    def _wrap(cls) -> None:
        orig = cls.__init__

        def init(self, *args, **kwargs):
            orig(self, *args, **kwargs)
            self.timeout_keep_alive = seconds

        cls.__init__ = init  # type: ignore[method-assign]

    try:
        from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
        _wrap(HttpToolsProtocol)
    except Exception:
        pass
    try:
        from uvicorn.protocols.http.h11_impl import H11Protocol
        _wrap(H11Protocol)
    except Exception:
        pass


_patch_uvicorn_keep_alive()

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _sse_chunk(evt: dict) -> str:
    """Format one SSE event. Pad `status` so proxies flush (thinking indicator must arrive immediately)."""
    block = f"event: {evt['event']}\ndata: {json.dumps(evt['data'])}\n\n"
    if evt.get("event") == "status":
        block += ": " + (" " * 2048) + "\n\n"
    return block


app = FastAPI(title="AISB Restaurant Problem Engines", version="0.1.0")
configured_origins = os.getenv("FRONTEND_ORIGINS", "")
frontend_origins = [
    origin.strip().rstrip("/")
    for origin in configured_origins.split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        *frontend_origins,
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store; fine for a prototype, swap for a database in production.
DATASETS: dict[str, Dataset] = {}
DATASET_OWNERS: dict[str, str] = {}
DATASET_RAW_FILES: dict[str, list[tuple[str, bytes]]] = {}
ANALYSES: dict[str, Analysis] = {}

Alpha = Query(DEFAULT_ALPHA, ge=0.0, le=1.0, description="1.0 = rank by money only, 0.0 = by time only")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/taxonomy")
def taxonomy():
    return [c.to_dict() for c in CATEGORIES.values()]


@app.get("/industries")
def industries():
    return [{"id": id_, "label": label} for id_, label in INDUSTRY_LABELS.items()]


def _iso(ts) -> str | None:
    return None if ts is None else ts.isoformat()


def _dataset_summary(dataset_id: str, ds: Dataset) -> dict:
    """What the frontend needs to restore ingest UI without re-uploading."""
    return {
        "dataset_id": dataset_id,
        "name": ds.profile.name,
        "analyzed": dataset_id in ANALYSES or dataset_id == "demo",
        "domains": sorted(ds.tables),
        "files": [r.to_dict() for r in ds.files],
        "data_quality": {d: q.to_dict() for d, q in ds.quality.items()},
        "profile": ds.profile.to_dict(),
        "period_start": _iso(ds.period_start),
        "period_end": _iso(ds.period_end),
        "row_counts": {d: int(len(df)) for d, df in ds.tables.items()},
    }


def _owned_dataset(dataset_id: str, user_id: str) -> Dataset:
    ds = DATASETS.get(dataset_id)
    if ds is None or DATASET_OWNERS.get(dataset_id) != user_id:
        raise HTTPException(404, "unknown dataset_id")
    return ds


def _user_dataset(user_id: str) -> Dataset:
    dataset_ids = [dataset_id for dataset_id, owner_id in DATASET_OWNERS.items() if owner_id == user_id]
    if not dataset_ids:
        raise HTTPException(404, "no files uploaded")
    files_by_name: dict[str, bytes] = {}
    profile = None
    for dataset_id in dataset_ids:
        files_by_name.update(DATASET_RAW_FILES.get(dataset_id, []))
        profile = DATASETS[dataset_id].profile
    files = list(files_by_name.items())
    if not files:
        raise HTTPException(404, "no files uploaded")
    ds = build_dataset(files, profile)
    return ds


def _user_report(user_id: str, alpha: float, industry: str = DEFAULT_INDUSTRY) -> dict:
    ds = _user_dataset(user_id)
    ANALYSES[user_id] = analyze(ds, industry=industry)
    return build_report(ANALYSES[user_id], alpha)


def _user_file_summary(user_id: str) -> dict:
    dataset_ids = [dataset_id for dataset_id, owner_id in DATASET_OWNERS.items() if owner_id == user_id]
    files_by_name: dict[str, tuple[int, float]] = {}
    profile = {}
    for dataset_id in dataset_ids:
        ds = DATASETS[dataset_id]
        profile = ds.profile.to_dict()
        for filename, payload in DATASET_RAW_FILES.get(dataset_id, []):
            files_by_name[filename] = (len(payload), time.time() * 1000)
    return {
        "dataset_id": "context",
        "name": profile.get("name", "Business context"),
        "files": [
            {"name": filename, "filename": filename, "size": size, "last_modified": uploaded_at}
            for filename, (size, uploaded_at) in files_by_name.items()
        ],
        "profile": profile,
    }


@app.get("/context")
def get_context(user_id: str = Depends(current_user_id)):
    return _user_file_summary(user_id)


@app.post("/context/files")
async def upload_context(
    files: list[UploadFile] = File(...),
    profile: str | None = Form(None),
    user_id: str = Depends(current_user_id),
):
    try:
        profile_data = json.loads(profile) if profile else {}
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(400, f"profile must be a JSON object: {exc}")
    raw = [(f.filename or "upload.csv", await f.read()) for f in files]
    dataset_ids = [dataset_id for dataset_id, owner_id in DATASET_OWNERS.items() if owner_id == user_id]
    dataset_id = dataset_ids[0] if dataset_ids else uuid.uuid4().hex[:12]
    existing = dict(DATASET_RAW_FILES.get(dataset_id, []))
    existing.update(raw)
    DATASET_RAW_FILES[dataset_id] = list(existing.items())
    DATASETS[dataset_id] = build_dataset(DATASET_RAW_FILES[dataset_id], Profile.from_dict(profile_data or None))
    DATASET_OWNERS[dataset_id] = user_id
    ANALYSES.pop(dataset_id, None)
    return _user_file_summary(user_id)


@app.delete("/context/files")
def delete_context_files(filenames: list[str], user_id: str = Depends(current_user_id)):
    if not filenames:
        raise HTTPException(400, "filenames must contain at least one filename")
    dataset_ids = [dataset_id for dataset_id, owner_id in DATASET_OWNERS.items() if owner_id == user_id]
    for dataset_id in dataset_ids:
        remaining = [(filename, payload) for filename, payload in DATASET_RAW_FILES.get(dataset_id, []) if filename not in filenames]
        DATASET_RAW_FILES[dataset_id] = remaining
        DATASETS[dataset_id] = build_dataset(remaining, DATASETS[dataset_id].profile)
        ANALYSES.pop(dataset_id, None)
    return _user_file_summary(user_id)


@app.post("/context/analyze")
def analyze_context(
    alpha: float = Alpha,
    industry: str = Query(DEFAULT_INDUSTRY),
    user_id: str = Depends(current_user_id),
):
    return _user_report(user_id, alpha, industry)


@app.post("/context/report")
async def context_report(
    files: list[UploadFile] = File(...),
    alpha: float = Form(DEFAULT_ALPHA),
    industry: str = Form(DEFAULT_INDUSTRY),
):
    raw = [(f.filename or "upload.csv", await f.read()) for f in files]
    if not raw:
        raise HTTPException(422, "at least one context file is required")
    dataset = build_dataset(raw)
    if not dataset.tables:
        raise HTTPException(422, "none of the context files matched a known data type")
    return build_report(analyze(dataset, industry=industry), alpha)


class ToolProblemIn(BaseModel):
    problem: str = Field(..., min_length=1, max_length=4000)


def _tool_stream(problems: list[str]) -> StreamingResponse:
    async def events():
        def chunk(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        updates = (
            ("preparing", "Reading your business problem…", 10),
            ("researching", "Searching vendor sites and product capabilities…", 35),
            ("pricing", "Checking pricing, integrations, and restaurant fit…", 60),
            ("ranking", "Comparing the strongest options…", 82),
        )
        yield chunk("progress", {"phase": updates[0][0], "message": updates[0][1], "progress": updates[0][2]})
        task = asyncio.create_task(asyncio.to_thread(get_tools, problems))
        index = 1
        while not task.done():
            await asyncio.sleep(2)
            if task.done():
                break
            phase, message, progress = updates[index % len(updates)]
            index += 1
            yield chunk("progress", {"phase": phase, "message": message, "progress": progress})
        try:
            result = await task
        except Exception as exc:
            debug("api/tools", f"stream failed: {exc}")
            yield chunk("error", {"message": f"Error getting tools: {exc}"})
            return
        yield chunk("progress", {"phase": "complete", "message": "Recommendations ready.", "progress": 100})
        yield chunk("done", result)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/context/tools/problem/stream")
async def context_problem_tools_stream(body: ToolProblemIn):
    return _tool_stream([body.problem.strip()])


@app.get("/context/tools")
def context_tools(alpha: float = Alpha, user_id: str = Depends(current_user_id)):
    report = _user_report(user_id, alpha)
    problems = _problem_briefs_for_tools(report)
    if not problems:
        raise HTTPException(422, "no confident top problems to research")
    return get_tools(problems)


@app.post("/context/tools/stream")
async def context_tools_stream(
    files: list[UploadFile] | None = File(None),
    alpha: float = Form(DEFAULT_ALPHA),
    problem: str | None = Form(None),
):
    if problem and problem.strip():
        problems = [problem.strip()]
    else:
        raw = [(f.filename or "upload.csv", await f.read()) for f in (files or [])]
        if not raw:
            raise HTTPException(422, "at least one context file is required")
        dataset = build_dataset(raw)
        if not dataset.tables:
            raise HTTPException(422, "none of the context files matched a known data type")
        report = build_report(analyze(dataset), alpha)
        problems = _problem_briefs_for_tools(report)
    if not problems:
        raise HTTPException(422, "no confident top problems to research")

    async def events():
        def chunk(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"
        updates = (
            ("preparing", "Reading your top business problems…", 10),
            ("researching", "Searching vendor sites and product capabilities…", 35),
            ("pricing", "Checking pricing, integrations, and restaurant fit…", 60),
            ("ranking", "Comparing the strongest options…", 82),
        )
        yield chunk("progress", {"phase": updates[0][0], "message": updates[0][1], "progress": updates[0][2]})
        task = asyncio.create_task(asyncio.to_thread(get_tools, problems))
        index = 1
        while not task.done():
            await asyncio.sleep(2)
            if task.done():
                break
            phase, message, progress = updates[index % len(updates)]
            index += 1
            yield chunk("progress", {"phase": phase, "message": message, "progress": progress})
        try:
            result = await task
        except Exception as exc:
            yield chunk("error", {"message": f"Error getting tools: {exc}"})
            return
        yield chunk("progress", {"phase": "complete", "message": "Recommendations ready.", "progress": 100})
        yield chunk("done", result)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/datasets")
def list_datasets(user_id: str = Depends(current_user_id)):
    """List the current user's in-process uploads, plus whether analyze has run.

    Uploads are compatibility support for API clients; the current web app stores context
    in browser IndexedDB and posts files directly for each analysis. `demo` is listed only
    after `/demo` (or tools/chat with dataset_id=demo) has built the synthetic analysis.
    """
    items = [_dataset_summary(i, ds) for i, ds in DATASETS.items() if DATASET_OWNERS.get(i) == user_id]
    if _demo_analysis.cache_info().currsize:
        items.append(_dataset_summary("demo", _demo_analysis().dataset))
    return {"datasets": items}


@app.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, user_id: str = Depends(current_user_id)):
    if dataset_id == "demo":
        return _dataset_summary("demo", _demo_analysis().dataset)
    ds = _owned_dataset(dataset_id, user_id)
    return _dataset_summary(dataset_id, ds)


@app.post("/uploads")
async def upload(
    files: list[UploadFile] = File(...),
    profile: str | None = Form(None),
    dataset_id: str | None = Form(None),
    user_id: str = Depends(current_user_id),
):
    try:
        prof = Profile.from_dict(json.loads(profile) if profile else None)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(400, f"profile must be a JSON object: {exc}")
    raw = [(f.filename or "upload.csv", await f.read()) for f in files]
    incoming = build_dataset(raw, prof)
    if not incoming.tables:
        raise HTTPException(422, {"error": "none of the files matched a known data type",
                                  "files": [r.to_dict() for r in incoming.files]})

    if dataset_id:
        ds = _owned_dataset(dataset_id, user_id)
        DATASET_RAW_FILES[dataset_id] = raw
        DATASETS[dataset_id] = build_dataset(raw, ds.profile)
        ANALYSES.pop(dataset_id, None)
    else:
        dataset_id = uuid.uuid4().hex[:12]
        DATASETS[dataset_id] = build_dataset(raw, prof)
        DATASET_OWNERS[dataset_id] = user_id
        DATASET_RAW_FILES[dataset_id] = raw
    ds = DATASETS[dataset_id]
    return {
        "dataset_id": dataset_id,
        "domains": sorted(ds.tables),
        "files": [r.to_dict() for r in ds.files],
        "data_quality": {d: q.to_dict() for d, q in ds.quality.items()},
    }


@app.delete("/datasets/{dataset_id}/files")
def delete_dataset_file(dataset_id: str, filename: str = Query(..., min_length=1), user_id: str = Depends(current_user_id)):
    ds = _owned_dataset(dataset_id, user_id)

    report_index = next((i for i, report in enumerate(ds.files) if report.filename == filename), None)
    if report_index is None:
        raise HTTPException(404, "unknown file")

    remaining = [(name, payload) for name, payload in DATASET_RAW_FILES[dataset_id] if name != filename]
    if len(remaining) == len(DATASET_RAW_FILES[dataset_id]):
        raise HTTPException(404, "unknown file")
    profile = ds.profile
    DATASET_RAW_FILES[dataset_id] = remaining
    DATASETS[dataset_id] = build_dataset(remaining, profile)
    ANALYSES.pop(dataset_id, None)
    return _dataset_summary(dataset_id, ds)


@app.delete("/datasets/{dataset_id}/files/bulk")
def delete_dataset_files(dataset_id: str, filenames: list[str], user_id: str = Depends(current_user_id)):
    ds = _owned_dataset(dataset_id, user_id)
    requested = list(dict.fromkeys(filename for filename in filenames if filename.strip()))
    if not requested:
        raise HTTPException(400, "filenames must contain at least one filename")
    known = {report.filename for report in ds.files}
    unknown = [filename for filename in requested if filename not in known]
    if unknown:
        raise HTTPException(404, f"unknown file: {unknown[0]}")
    DATASET_RAW_FILES[dataset_id] = [
        (filename, payload) for filename, payload in DATASET_RAW_FILES[dataset_id]
        if filename not in requested
    ]
    DATASETS[dataset_id] = build_dataset(DATASET_RAW_FILES[dataset_id], ds.profile)
    ANALYSES.pop(dataset_id, None)
    return _dataset_summary(dataset_id, DATASETS[dataset_id])


@app.post("/analyze/{dataset_id}")
def run_analysis(dataset_id: str, alpha: float = Alpha, user_id: str = Depends(current_user_id)):
    ds = _owned_dataset(dataset_id, user_id)
    ANALYSES[dataset_id] = analyze(ds)
    return build_report(ANALYSES[dataset_id], alpha)


@app.get("/report/{dataset_id}")
def report(dataset_id: str, alpha: float = Alpha, user_id: str = Depends(current_user_id)):
    if dataset_id != "demo":
        _owned_dataset(dataset_id, user_id)
        if dataset_id not in ANALYSES:
            ANALYSES[dataset_id] = analyze(DATASETS[dataset_id])
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


def _analysis_for_dataset(dataset_id: str, user_id: str | None = None) -> Analysis:
    if dataset_id == "demo":
        return _demo_analysis()
    if dataset_id in ANALYSES and (user_id is None or DATASET_OWNERS.get(dataset_id) == user_id):
        return ANALYSES[dataset_id]
    if dataset_id in DATASETS and (user_id is None or DATASET_OWNERS.get(dataset_id) == user_id):
        raise HTTPException(409, "dataset not analyzed yet; POST /analyze/{dataset_id} first")
    raise HTTPException(404, "unknown dataset_id")


def _problem_briefs_for_tools(report: dict, selected_problem: str | None = None) -> list[str]:
    briefs: list[str] = []
    if selected_problem and selected_problem.strip():
        selected = selected_problem.strip()
        for candidate in report.get("top_problems") or []:
            label = (candidate.get("label") or candidate.get("category") or "").strip()
            if label.lower() == selected.lower():
                summary = (candidate.get("summary") or "").strip()
                return [f"{label}: {summary}" if summary else label]
        return [selected]

    candidates = report.get("top_problems") or []
    candidates = candidates[:2]
    for p in candidates:
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
    user_id: str = Depends(current_user_id),
):
    _owned_dataset(dataset_id, user_id)
    return _tool_recommendations(dataset_id, alpha, log_label="GET /tools")


@app.get("/tools/stream")
async def stream_tools(
    dataset_id: str = Query(..., description="Analyzed upload id, or 'demo' for synthetic data"),
    alpha: float = Alpha,
    problem: str | None = Query(None, description="Optional problem label/category to research"),
    user_id: str = Depends(current_user_id),
):
    """Stream progress while researching tools for an analyzed dataset."""
    report = build_report(_analysis_for_dataset(dataset_id, user_id), alpha)
    problems = _problem_briefs_for_tools(report, problem)
    if not problems:
        raise HTTPException(422, "no confident top problems to research; POST /analyze after upload")

    async def events():
        def chunk(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        updates = (
            ("preparing", "Reading your top business problems…", 10),
            ("researching", "Searching vendor sites and product capabilities…", 35),
            ("pricing", "Checking pricing, integrations, and restaurant fit…", 60),
            ("ranking", "Comparing the strongest options…", 82),
        )
        for phase, message, progress in updates[:1]:
            yield chunk("progress", {"phase": phase, "message": message, "progress": progress})
        task = asyncio.create_task(asyncio.to_thread(get_tools, problems))
        index = 1
        while not task.done():
            await asyncio.sleep(2)
            if task.done():
                break
            phase, message, progress = updates[index % len(updates)]
            index += 1
            yield chunk("progress", {"phase": phase, "message": message, "progress": progress})
        try:
            result = await task
        except Exception as exc:
            debug("api/tools", f"stream failed: {exc}")
            yield chunk("error", {"message": f"Error getting tools: {exc}"})
            return
        yield chunk("progress", {"phase": "complete", "message": "Recommendations ready.", "progress": 100})
        yield chunk("done", result)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


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
    industry: str = Field(DEFAULT_INDUSTRY, min_length=1, max_length=64)


class ChatMessageIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


def _chat_context(dataset_id: str | None, user_id: str, industry: str = DEFAULT_INDUSTRY) -> str | None:
    if not dataset_id:
        if not any(owner_id == user_id for owner_id in DATASET_OWNERS.values()):
            return None
        analysis = analyze(_user_dataset(user_id), industry=industry)
        rep = build_report(analysis)
        return "\n".join([f"Restaurant: {rep['restaurant']['name']}"] + [f"- {p['summary']}" for p in rep["top_problems"]])
    if dataset_id != "demo" and dataset_id not in ANALYSES and dataset_id in DATASETS:
        _owned_dataset(dataset_id, user_id)
        ANALYSES[dataset_id] = analyze(DATASETS[dataset_id])
    analysis = _analysis_for_dataset(dataset_id, user_id)
    rep = build_report(analysis)
    lines = [f"Restaurant: {rep['restaurant']['name']}"]
    lines += [f"- {p['summary']}" for p in rep["top_problems"]]
    return "\n".join(lines)


def _chat_session_or_404(session_id: str, user_id: str) -> chat_sessions.ChatSession:
    session = chat_sessions.get_session(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(404, "unknown session_id")
    return session


@app.post("/chat/sessions")
def create_chat_session(body: ChatSessionIn | None = None, user_id: str = Depends(current_user_id)):
    dataset_id = body.dataset_id if body and body.dataset_id else None
    industry = body.industry if body else DEFAULT_INDUSTRY
    session = chat_sessions.create_session(_chat_context(dataset_id, user_id, industry), owner_id=user_id)
    debug(
        "api/chat",
        f"POST /chat/sessions -> session_id={session.id} dataset_id={dataset_id!r} "
        f"context={'yes' if session.context else 'no'}",
    )
    return session.to_dict()


@app.get("/chat/sessions/{session_id}")
def get_chat_session(session_id: str, user_id: str = Depends(current_user_id)):
    """Full transcript, current problem, and the latest tool recommendations (poll this)."""
    session = _chat_session_or_404(session_id, user_id)
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
def delete_chat_session(session_id: str, user_id: str = Depends(current_user_id)):
    _chat_session_or_404(session_id, user_id)
    if not chat_sessions.delete_session(session_id):
        raise HTTPException(404, "unknown session_id")
    return {"ok": True}


@app.post("/chat/sessions/{session_id}/messages")
async def chat_message(session_id: str, body: ChatMessageIn, user_id: str = Depends(current_user_id)):
    """Send an owner message; replies as a server-sent-event stream.

    Events: status (thinking / researching / aggregating), state, tools, notice,
    token*, done | error. Always streamed so the UI can show a thinking ticker immediately.
    """
    session = _chat_session_or_404(session_id, user_id)
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
                yield _sse_chunk(evt)

    return StreamingResponse(sse(), media_type="text/event-stream", headers=SSE_HEADERS)
