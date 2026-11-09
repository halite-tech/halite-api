import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.api import app
from backend.chat_stream import service


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        fields = {}
        for line in block.split("\n"):
            if ": " in line:
                k, v = line.split(": ", 1)
                fields[k] = v
            elif line.startswith(":"):
                continue
        if "event" not in fields or "data" not in fields:
            continue
        out.append((fields["event"], json.loads(fields["data"])))
    return out


def first_state(events):
    return next(d for e, d in events if e == "state")


@pytest.fixture
def fake_llm(monkeypatch):
    """Scripted analyses in, canned tokens out; records what the tools pipeline receives."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    script: list[dict] = []
    tool_calls: list[list[str]] = []
    seen_prompts: list[list[dict]] = []

    async def fake_complete_json(messages, name, schema, temperature=0.0):
        return script.pop(0)

    async def fake_stream_chat(messages, temperature=0.7):
        seen_prompts.append(messages)
        for tok in ["Why ", "exactly?"]:
            yield tok

    def fake_get_tools(problems):
        tool_calls.append(problems)
        return {"tool_name": "T", "for": problems[0]}

    monkeypatch.setattr(service, "complete_json", fake_complete_json)
    monkeypatch.setattr(service, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(service, "get_tools", fake_get_tools)
    return script, tool_calls, seen_prompts


def analysis(action, problem, clarity=30, understood=False, facts=(), questions=("how much?",),
             explicit_switch=None):
    if explicit_switch is None:
        explicit_switch = action == "new_problem"
    return {"action": action, "explicit_switch": explicit_switch, "problem": problem, "known_facts": list(facts),
            "open_questions": list(questions), "clarity": clarity, "understood": understood}


def wait_for_tools(client, sid, status="ready"):
    for _ in range(50):
        tools = client.get(f"/chat/sessions/{sid}").json()["tools"]
        if tools["status"] == status:
            return tools
        time.sleep(0.05)
    raise AssertionError(f"tools never reached {status}: {tools}")


def send(client, sid, msg):
    r = client.post(f"/chat/sessions/{sid}/messages", json={"message": msg})
    assert r.status_code == 200
    return parse_sse(r.text)


def test_problem_switch_replaces_problem_without_running_tools(fake_llm):
    script, tool_calls, seen_prompts = fake_llm
    with TestClient(app) as client:
        sid = client.post("/chat/sessions").json()["session_id"]

        script.append(analysis("new_problem", "Food waste is high", facts=["waste bin full nightly"]))
        events = send(client, sid, "we throw out so much food")
        names = [e for e, _ in events]
        assert names[0] == "status" and events[0][1]["phase"] == "thinking"
        assert "state" in names and names[-1] == "done"
        assert "tools" not in names
        assert first_state(events)["problem"] == "Food waste is high"
        assert len(tool_calls) == 0

        script.append(analysis("keep", "Food waste is high", facts=["waste bin full nightly", "200 lbs/wk"]))
        events = send(client, sid, "about 200 lbs a week")
        names = [e for e, _ in events]
        assert "tools" not in names and "notice" not in names
        assert "clarity" not in first_state(events)
        assert "understood" not in first_state(events)
        assert len(tool_calls) == 0

        script.append(analysis("new_problem", "Servers no-show constantly", facts=["3 no-shows a month"]))
        events = send(client, sid, "actually forget that, my real problem is no-shows")
        state = first_state(events)
        assert state["action"] == "new_problem"
        assert state["previous_problem"] == "Food waste is high"
        assert state["problem"] == "Servers no-show constantly"
        assert state["known_facts"] == ["3 no-shows a month"]
        assert "tools" not in [e for e, _ in events]

        # the model only sees the new problem's context: old problem and its turns are gone
        sent = seen_prompts[-1]
        assert "Food waste" not in json.dumps(sent)
        assert [m["role"] for m in sent] == ["system", "user"]

        script.append(analysis("keep", "Servers no-show constantly", facts=["3 no-shows a month"]))
        send(client, sid, "more detail")
        script.append(analysis("keep", "Servers no-show constantly", facts=["3 no-shows a month", "Fridays worst"]))
        events = send(client, sid, "Fridays are the worst")
        assert "tools" not in [e for e, _ in events]
        assert len(tool_calls) == 0

        session = client.get(f"/chat/sessions/{sid}").json()
        assert session["problem"] == "Servers no-show constantly"
        assert session["problem_version"] == 2
        assert any(m.get("type") == "problem_changed" for m in session["messages"])


def test_each_message_updates_problem_without_researching_tools(fake_llm):
    script, tool_calls, _ = fake_llm
    with TestClient(app) as client:
        sid = client.post("/chat/sessions").json()["session_id"]
        script.append(analysis("new_problem", "Slow ticket times"))
        events = send(client, sid, "tickets take 40 min")
        names = [e for e, _ in events]
        assert "tools" not in names and "notice" not in names
        assert len(tool_calls) == 0


def test_new_problem_is_re_evaluated_on_every_message(fake_llm):
    script, tool_calls, _ = fake_llm
    with TestClient(app) as client:
        sid = client.post("/chat/sessions").json()["session_id"]
        script.append(analysis("new_problem", "Traffic is down 50%", facts=["500 customers before"]))
        send(client, sid, "traffic is down")
        for i, msg in enumerate(["we have 60 seats", "we use Toast"]):
            script.append(analysis("new_problem", "Owner describes seats/POS", facts=[f"detail {i}"],
                                   explicit_switch=False))
            events = send(client, sid, msg)
            assert first_state(events)["action"] == "new_problem"
            assert first_state(events)["problem"] == "Owner describes seats/POS"
        assert "tools" not in [e for e, _ in events]
        assert len(tool_calls) == 0


def test_turn_starts_with_thinking_status(fake_llm):
    script, _, _ = fake_llm
    with TestClient(app) as client:
        sid = client.post("/chat/sessions").json()["session_id"]
        script.append(analysis("new_problem", "Traffic is down"))
        r = client.post(f"/chat/sessions/{sid}/messages", json={"message": "traffic is down"})
        assert r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.text)
        assert events[0] == ("status", {"phase": "thinking", "message": "Thinking about your answer…"})
        assert events[-1][0] == "done"


def test_chat_errors(fake_llm, monkeypatch):
    with TestClient(app) as client:
        assert client.post("/chat/sessions/nope/messages", json={"message": "hi"}).status_code == 404
        assert client.get("/chat/sessions/nope").status_code == 404
        assert client.post("/chat/sessions", json={"dataset_id": "nope"}).status_code == 404
        sid = client.post("/chat/sessions").json()["session_id"]
        assert client.post(f"/chat/sessions/{sid}/messages", json={"message": ""}).status_code == 422
        monkeypatch.delenv("OPENROUTER_API_KEY")
        assert client.post(f"/chat/sessions/{sid}/messages", json={"message": "hi"}).status_code == 500
        assert client.delete(f"/chat/sessions/{sid}").status_code == 200
        assert client.get(f"/chat/sessions/{sid}").status_code == 404


def test_session_can_be_grounded_in_demo(fake_llm):
    with TestClient(app) as client:
        session = client.post("/chat/sessions", json={"dataset_id": "demo"}).json()
        assert session["messages"][0]["role"] == "assistant"  # opening question
