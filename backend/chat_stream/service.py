"""One chat turn: track the problem in focus, kick off tool research, stream the grilling reply.

Per user message:
  1. A structured (non-streaming) call decides whether the owner kept, refined, or replaced the
     problem and scores how well it is understood so far.
  2. If the problem was replaced, the old problem and its working context are dropped and the new
     one takes its place. The full tools pipeline (`get_tools`) is re-run for the new problem in
     the background; it runs again once the problem becomes "understood".
  3. The grilling reply is streamed token by token.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from backend.chat_stream.openrouter import complete_json, stream_chat
from backend.chat_stream.prompts import EXTRACTOR_SCHEMA, EXTRACTOR_SYSTEM, GRILL_SYSTEM
from backend.chat_stream.session import ChatSession, ToolsState
from backend.tool_rec.llmConnection import get_tools

MIN_ROUNDS = 3  # owner answers required on a problem before it can count as understood
UNDERSTOOD_CLARITY = 85
MAX_CONTEXT_MESSAGES = 30  # messages of the current problem sent to the model
EXTRACTOR_WINDOW = 12

_BACKGROUND: set[asyncio.Task] = set()  # keep references so tasks aren't garbage collected


def problem_brief(session: ChatSession) -> str:
    """The problem text handed to the tools pipeline."""
    brief = session.problem or ""
    if session.known_facts:
        brief += "\nKey details from the owner:\n" + "\n".join(f"- {f}" for f in session.known_facts)
    return brief


def _system_prompt(session: ChatSession, switched: bool) -> str:
    parts = [GRILL_SYSTEM]
    if session.context:
        parts.append("BACKGROUND ON THIS RESTAURANT (from their analysed data; use it to ask sharper "
                     "questions, but make the owner confirm anything you rely on):\n" + session.context)
    facts = "\n".join(f"- {f}" for f in session.known_facts) or "- (nothing yet)"
    questions = "\n".join(f"- {q}" for q in session.open_questions) or "- (none)"
    status = ("UNDERSTOOD: stop grilling and wrap up as instructed." if session.understood
              else "NOT YET UNDERSTOOD: keep grilling.")
    parts.append(
        "SESSION STATE\n"
        f"Problem in focus: {session.problem}\n"
        f"Owner answers on this problem so far: {session.focus_user_turns}\n"
        f"Clarity: {session.clarity}/100\n"
        f"Status: {status}\n"
        f"Known facts:\n{facts}\n"
        f"Open questions (grill these next):\n{questions}"
        + ("\nThe owner JUST SWITCHED to this problem. Acknowledge the switch in one line and start fresh."
           if switched else "")
    )
    return "\n\n".join(parts)


async def _analyze(session: ChatSession, user_message: str) -> dict:
    recent = session.focus_messages[-EXTRACTOR_WINDOW:]
    convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in recent) or "(no conversation yet)"
    facts = "\n".join(f"- {f}" for f in session.known_facts) or "(none)"
    content = (
        f"CURRENT PROBLEM IN FOCUS: {session.problem or '(none yet)'}\n"
        f"KNOWN FACTS SO FAR:\n{facts}\n\n"
        f"RECENT CONVERSATION ABOUT THIS PROBLEM:\n{convo}\n\n"
        f"OWNER'S LATEST MESSAGE:\n{user_message}"
    )
    return await complete_json(
        [{"role": "system", "content": EXTRACTOR_SYSTEM}, {"role": "user", "content": content}],
        name="grill_state",
        schema=EXTRACTOR_SCHEMA,
    )


def _parse_tools(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


async def _tools_job(session: ChatSession, run: int, brief: str) -> None:
    try:
        result = _parse_tools(await asyncio.to_thread(get_tools, [brief]))
        outcome = ToolsState("ready", brief, result=result, updated_at=time.time())
    except Exception as exc:  # noqa: BLE001 - surface any pipeline failure to the client
        outcome = ToolsState("error", brief, error=f"{type(exc).__name__}: {exc}", updated_at=time.time())
    if session.tools_run == run:  # drop results for a problem that has since been replaced
        session.tools = outcome


def launch_tools(session: ChatSession) -> None:
    session.tools_run += 1
    brief = problem_brief(session)
    session.tools = ToolsState("running", brief, updated_at=time.time())
    task = asyncio.create_task(_tools_job(session, session.tools_run, brief))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _tools_status(session: ChatSession) -> dict:
    t = session.tools.to_dict()
    t.pop("result")  # fetch the payload from GET /chat/sessions/{id}
    return t


def _apply_analysis(session: ChatSession, analysis: dict, user_message: str) -> tuple[str, str | None, bool]:
    """Mutate the session from the analysis. Returns (action, previous_problem, problem_changed)."""
    previous = session.problem
    action = analysis["action"] if previous else "new_problem"
    statement = (analysis["problem"] or "").strip()
    if action == "keep" or not statement:
        statement = previous or user_message.strip()[:300]
    changed = statement != previous

    if action == "new_problem":
        session.problem_version += 1
        session.focus_messages = []  # the old problem and everything said about it is dropped
        session.focus_user_turns = 0
        session.log("event", f"Problem changed to: {statement}", type="problem_changed",
                    problem=statement, previous_problem=previous)

    session.problem = statement
    session.focus_messages.append({"role": "user", "content": user_message})
    session.focus_user_turns += 1
    session.log("user", user_message, problem_version=session.problem_version)

    clarity = max(0, min(100, int(analysis["clarity"])))
    ready = (bool(analysis["understood"]) and clarity >= UNDERSTOOD_CLARITY
             and session.focus_user_turns >= MIN_ROUNDS)
    session.clarity = clarity if ready else min(clarity, UNDERSTOOD_CLARITY - 1)
    session.understood = ready
    session.known_facts = [str(f) for f in analysis["known_facts"]]
    session.open_questions = [str(q) for q in analysis["open_questions"]]
    return action, previous, changed


def _event(name: str, data: dict) -> dict:
    return {"event": name, "data": data}


async def run_turn(session: ChatSession, user_message: str) -> AsyncIterator[dict]:
    """Yield {"event", "data"} dicts: state, tools, token*, done (or error)."""
    was_understood = session.understood
    try:
        analysis = await _analyze(session, user_message)
    except Exception as exc:  # noqa: BLE001
        yield _event("error", {"message": f"could not analyse message: {exc}"})
        return

    action, previous, changed = _apply_analysis(session, analysis, user_message)
    switched = action == "new_problem"
    yield _event("state", {**session.state(), "action": action, "previous_problem": previous})

    if switched or (session.understood and (not was_understood or changed)):
        launch_tools(session)
        yield _event("tools", _tools_status(session))

    messages = [{"role": "system", "content": _system_prompt(session, switched)}]
    messages += session.focus_messages[-MAX_CONTEXT_MESSAGES:]

    parts: list[str] = []
    try:
        async for delta in stream_chat(messages):
            parts.append(delta)
            yield _event("token", {"delta": delta})
    except Exception as exc:  # noqa: BLE001
        yield _event("error", {"message": f"stream failed: {exc}"})
    finally:  # also runs if the client disconnects mid-stream, so partial replies are kept
        text = "".join(parts)
        if text:
            session.focus_messages.append({"role": "assistant", "content": text})
            session.log("assistant", text, problem_version=session.problem_version)

    yield _event("done", {"state": session.state(), "tools": _tools_status(session)})
