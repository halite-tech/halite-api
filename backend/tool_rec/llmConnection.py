"""AI-tool recommendations for restaurant problems.

Two OpenRouter calls, because one call cannot do both jobs reliably:

1. Research (Responses API + ``openrouter:web_fetch``): free-form notes about candidate tools.
   Models do NOT honour a strict JSON schema once server tools are attached -- they silently drop
   required keys (``tool_pricing``, ``tool_number``, ``final_recommendation`` ...), which is what
   broke the frontend parsing.
2. Structuring (Chat Completions, no tools, strict ``json_schema``): turns the notes into the exact
   shape the frontend expects.

The result is then normalised in Python so the output *always* has the full shape, no matter what
the model did.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

from backend.shared.debug import debug, debug_trim

RESPONSES_URL = "https://openrouter.ai/api/v1/responses"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"
NOT_AVAILABLE = "Not publicly available"
CATEGORIES = ("platform_integrated", "highest_roi")

TOOL_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_number": {"type": "integer"},
        "tool_name": {"type": "string"},
        "tool_description": {"type": "string"},
        "tool_pricing": {"type": "string"},
        "tool_home_page": {"type": "string"},
        "category": {"type": "string", "enum": list(CATEGORIES)},
    },
    "required": [
        "tool_number",
        "tool_name",
        "tool_description",
        "tool_pricing",
        "tool_home_page",
        "category",
    ],
    "additionalProperties": False,
}

TOOL_REC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "problem": {"type": "string"},
                    "final_recommendation": {"type": "string"},
                    "tools": {
                        "type": "array",
                        "items": TOOL_ITEM_SCHEMA,
                    },
                },
                "required": ["problem", "tools", "final_recommendation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}


def _model() -> str:
    return (os.getenv("TOOL_REC_MODEL") or DEFAULT_MODEL).strip()


def _problem_list(problems: list[str]) -> str:
    return "\n".join(f"- {p}" for p in problems)


def prompt_research(problems: list[str]) -> str:
    return f"""
You are an experienced restaurant owner with 10+ years of operational experience. Identify the best AI tools to solve each problem listed below.

For **EACH problem**, find exactly **4 AI tools**:
- 2 **platform_integrated** tools that fit an existing restaurant POS/ops stack.
- 2 **highest_roi** tools with strong cost/revenue impact.

For every tool, research: homepage URL, pricing, how it solves the problem, integrations, ROI, reliability, and adoptability.
Use credible sources only; write "{NOT_AVAILABLE}" when you cannot verify a field.
The tools must be real, currently available products that are directly relevant to the problem.
ENSURE THE LINKS ARE VALID AND WORKING.

**Problems to research:**
{_problem_list(problems)}

Write concise research notes grouped by problem (same order), ending each problem with a one-paragraph
final recommendation: which single tool you would pick and why.
"""


def prompt_structure(problems: list[str], notes: str) -> str:
    return f"""
Convert the research notes below into JSON that matches the response schema exactly.

Rules:
- `recommendations` has exactly one entry per problem, in the same order as the problems list.
- `problem` repeats the problem text. `final_recommendation` is the owner's pick and why.
- `tools` has exactly 4 entries per problem: 2 with category `platform_integrated`, 2 with `highest_roi`.
  `tool_number` runs 1-4 within each problem.
- `tool_description` is the full narrative for the tool (what it does, how it solves the problem,
  integrations, ROI, reliability, adoptability).
- `tool_pricing` is a short pricing summary; `tool_home_page` is a full URL.
- Use "{NOT_AVAILABLE}" for any field the notes do not support. Never invent facts.

Problems:
{_problem_list(problems)}

Research notes:
{notes}
"""


def _json_from_model_text(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("model output was not a JSON object")
    return parsed


def _openrouter_error(data: Any) -> str | None:
    """Return a user-facing error string, or None when the payload is OK (error may be null)."""
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if not err:
        return None
    if isinstance(err, dict):
        return f"{err.get('code', 'error')}: {err.get('message', err)}"
    return str(err)


def _extract_output_text(data: dict[str, Any]) -> str:
    """Final assistant text from a Responses API payload."""
    output = data.get("output")
    if not isinstance(output, list):
        raise RuntimeError("unexpected OpenRouter response: missing output")
    message = next((item for item in reversed(output) if item.get("type") == "message"), None)
    if not message:
        raise RuntimeError("unexpected OpenRouter response: no message in output")
    content = message.get("content") or []
    text = next((part.get("text") for part in content if part.get("type") == "output_text"), None)
    if not text:
        raise RuntimeError("unexpected OpenRouter response: no output_text")
    return text


def _post(url: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Add it to AISB/.env and restart uvicorn "
            "(python-dotenv loads .env in backend.api on startup)."
        )
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{response.status_code}: non-JSON response from OpenRouter") from exc
    if msg := _openrouter_error(data):
        raise RuntimeError(msg)
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code}: {data}")
    if not isinstance(data, dict):
        raise RuntimeError("unexpected OpenRouter response: not a JSON object")
    return data


def _research(problems: list[str]) -> str:
    """Stage 1: web-assisted research notes. Falls back to model knowledge if web_fetch fails."""
    body: dict[str, Any] = {
        "model": _model(),
        "input": prompt_research(problems),
        "tools": [{"type": "openrouter:web_fetch", "parameters": {"max_uses": 4}}],
    }
    t0 = time.time()
    debug("tools", f"research start model={_model()} problems={len(problems)} web_fetch=on")
    try:
        notes = _extract_output_text(_post(RESPONSES_URL, body, timeout=180))
        debug("tools", f"research done in {time.time() - t0:.1f}s notes_chars={len(notes)}")
        return notes
    except RuntimeError as exc:
        debug("tools", f"research web_fetch failed ({exc}); retrying without web_fetch")
        body.pop("tools")  # web tool unavailable/failed: still answer from the model's own knowledge
        notes = _extract_output_text(_post(RESPONSES_URL, body, timeout=120))
        debug("tools", f"research done in {time.time() - t0:.1f}s (no web) notes_chars={len(notes)}")
        return notes


def _structure(problems: list[str], notes: str) -> dict[str, Any]:
    """Stage 2: strict-schema JSON from the notes (no tools, so the schema is enforced)."""
    body = {
        "model": _model(),
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt_structure(problems, notes)}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "tool_rec", "strict": True, "schema": TOOL_REC_SCHEMA},
        },
    }
    t0 = time.time()
    debug("tools", "structure start (strict json_schema)")
    data = _post(CHAT_URL, body, timeout=120)
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("unexpected OpenRouter response: no message content") from exc
    parsed = _json_from_model_text(text)
    n_tools = sum(len(r.get("tools") or []) for r in parsed.get("recommendations") or [] if isinstance(r, dict))
    debug("tools", f"structure done in {time.time() - t0:.1f}s raw_tool_rows={n_tools}")
    return parsed


def _text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _headline(problem: str) -> str:
    """First line of a (possibly multi-line) problem brief."""
    return next((ln.strip() for ln in problem.splitlines() if ln.strip()), problem.strip())


def normalize_recommendations(raw: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    """Guarantee the frontend shape: one entry per problem, every tool has every field."""
    recs_in = raw.get("recommendations")
    if not isinstance(recs_in, list):
        raise ValueError("missing 'recommendations' list")
    recs_out: list[dict[str, Any]] = []
    for i, problem in enumerate(problems):
        rec = recs_in[i] if i < len(recs_in) and isinstance(recs_in[i], dict) else {}
        tools: list[dict[str, Any]] = []
        for t in rec.get("tools") or []:
            if not isinstance(t, dict) or not _text(t.get("tool_name")):
                continue
            category = t.get("category")
            tools.append({
                "tool_number": len(tools) + 1,
                "tool_name": _text(t.get("tool_name")),
                "tool_description": _text(t.get("tool_description"), NOT_AVAILABLE),
                "tool_pricing": _text(t.get("tool_pricing"), NOT_AVAILABLE),
                "tool_home_page": _text(t.get("tool_home_page"), NOT_AVAILABLE),
                "category": category if category in CATEGORIES else "highest_roi",
            })
        recs_out.append({
            "problem": _text(rec.get("problem"), _headline(problem)),
            "final_recommendation": _text(rec.get("final_recommendation")),
            "tools": tools,
        })
    if not any(r["tools"] for r in recs_out):
        raise ValueError("model returned no usable tools")
    return {"recommendations": recs_out}


def get_tools(problems: list[str]) -> dict[str, Any]:
    """Return ``{"recommendations": [{problem, final_recommendation, tools: [...]}, ...]}``."""
    t0 = time.time()
    debug("tools", f"get_tools start n_problems={len(problems)}")
    for i, p in enumerate(problems):
        debug_trim("tools", f"  problem[{i}]: {p}", 160)
    notes = _research(problems)
    last_error: Exception | None = None
    for attempt in range(2):  # structuring is cheap; retry once on a malformed/empty answer
        try:
            out = normalize_recommendations(_structure(problems, notes), problems)
            for rec in out["recommendations"]:
                debug(
                    "tools",
                    f"  normalised: {len(rec['tools'])} tools | "
                    f"{', '.join(t['tool_name'] for t in rec['tools'])}",
                )
            debug("tools", f"get_tools ok in {time.time() - t0:.1f}s")
            return out
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            debug("tools", f"normalize/structure attempt {attempt + 1} failed: {exc}")
    raise RuntimeError(f"invalid tool JSON from model: {last_error}")
