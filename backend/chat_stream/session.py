"""In-memory chat session store (swap for Redis/DB in production)."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.chat_stream.prompts import OPENER


@dataclass
class ToolsState:
    status: str = "idle"  # idle | running | ready | error
    problem: str | None = None  # the exact brief the tools were (or are being) researched for
    result: Any = None
    error: str | None = None
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {"status": self.status, "problem": self.problem, "result": self.result,
                "error": self.error, "updated_at": self.updated_at}


@dataclass
class ChatSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    context: str | None = None  # optional background (e.g. the restaurant's analysed report)

    # --- the one problem currently in focus ---
    problem: str | None = None
    problem_version: int = 0  # bumps every time the focus is replaced
    understood: bool = False
    clarity: int = 0
    known_facts: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    focus_user_turns: int = 0  # owner answers given on the current problem

    # model context for the current problem only; wiped when the problem is replaced
    focus_messages: list[dict] = field(default_factory=list)
    # full display log for the UI; never wiped, problem switches are recorded as events
    transcript: list[dict] = field(default_factory=list)

    tools: ToolsState = field(default_factory=ToolsState)
    tools_run: int = 0  # latest tools run id; older runs are discarded when they finish

    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        self.transcript.append({"role": "assistant", "content": OPENER, "ts": time.time()})

    def log(self, role: str, content: str, **extra) -> None:
        self.transcript.append({"role": role, "content": content, "ts": time.time(), **extra})

    def state(self) -> dict:
        return {
            "problem": self.problem,
            "problem_version": self.problem_version,
            "understood": self.understood,
            "clarity": self.clarity,
            "known_facts": self.known_facts,
            "open_questions": self.open_questions,
        }

    def to_dict(self) -> dict:
        return {"session_id": self.id, "created_at": self.created_at, **self.state(),
                "messages": self.transcript, "tools": self.tools.to_dict()}


SESSIONS: dict[str, ChatSession] = {}


def create_session(context: str | None = None) -> ChatSession:
    session = ChatSession(context=context)
    SESSIONS[session.id] = session
    return session


def get_session(session_id: str) -> ChatSession | None:
    return SESSIONS.get(session_id)


def delete_session(session_id: str) -> bool:
    return SESSIONS.pop(session_id, None) is not None
