import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.api import app
from backend.chat_stream import service


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


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


def test_problem_switch_replaces_problem_and_reruns_tools(fake_llm):
    script, tool_calls, seen_prompts = fake_llm
    with TestClient(app) as client:
        sid = client.post("/chat/sessions").json()["session_id"]

        script.append(analysis("new_problem", "Food waste is high", facts=["waste bin full nightly"]))
        events = send(client, sid, "we throw out so much food")
        assert [e for e, _ in events] == ["state", "token", "token", "done"]  # grill first; no tools yet
        assert events[0][1]["problem"] == "Food waste is high"
        assert len(tool_calls) == 0

        script.append(analysis("keep", "Food waste is high", clarity=40, facts=["waste bin full nightly"]))
        send(client, sid, "about 200 lbs a week")
        script.append(analysis("keep", "Food waste is high", clarity=60, facts=["waste bin full nightly", "200 lbs/wk"]))
        events = send(client, sid, "mostly produce")
        names = [e for e, _ in events]
        assert "tools" in names and "notice" in names  # 3rd owner message -> tooling
        assert events[0][1]["clarity"] == 100
        assert events[0][1]["understood"] is True
        assert any(d.get("text") == service.LOOKING_FOR_TOOLS_MSG for e, d in events if e == "notice")
        assert names.index("notice") < names.index("token")
        wait_for_tools(client, sid)
        assert len(tool_calls) == 1
        assert "Food waste is high" in tool_calls[-1][0]
        assert "200 lbs/wk" in tool_calls[-1][0]

        script.append(analysis("new_problem", "Servers no-show constantly", facts=["3 no-shows a month"]))
        events = send(client, sid, "actually forget that, my real problem is no-shows")
        state = events[0][1]
        assert state["action"] == "new_problem"
        assert state["previous_problem"] == "Food waste is high"
        assert state["problem"] == "Servers no-show constantly"
        assert state["known_facts"] == ["3 no-shows a month"]
        assert "tools" not in [e for e, _ in events]  # new problem: grill before tools

        # the model only sees the new problem's context: old problem and its turns are gone
        sent = seen_prompts[-1]
        assert "Food waste" not in json.dumps(sent)
        assert [m["role"] for m in sent] == ["system", "user"]

        script.append(analysis("keep", "Servers no-show constantly", facts=["3 no-shows a month"]))
        send(client, sid, "more detail")
        script.append(analysis("keep", "Servers no-show constantly", facts=["3 no-shows a month", "Fridays worst"]))
        events = send(client, sid, "Fridays are the worst")
        assert "tools" in [e for e, _ in events]
        tools = wait_for_tools(client, sid)
        assert "Servers no-show" in tools["result"]["for"]
        assert len(tool_calls) == 2

        session = client.get(f"/chat/sessions/{sid}").json()
        assert session["problem"] == "Servers no-show constantly"
        assert session["problem_version"] == 2
        assert any(m.get("type") == "problem_changed" for m in session["messages"])


def test_understood_needs_minimum_rounds_then_refreshes_tools(fake_llm):
    script, tool_calls, _ = fake_llm
    with TestClient(app) as client:
        sid = client.post("/chat/sessions").json()["session_id"]
        script.append(analysis("new_problem", "Slow ticket times", clarity=95, understood=True))
        events = send(client, sid, "tickets take 40 min")
        assert events[0][1]["understood"] is False  # model said done after one answer; guardrail refuses
        assert events[0][1]["clarity"] == 84
        assert "tools" not in [e for e, _ in events]

        script.append(analysis("keep", "Slow ticket times", clarity=95, understood=True))
        assert send(client, sid, "answer 2")[0][1]["understood"] is False
        script.append(analysis("keep", "Slow ticket times", clarity=95, understood=True))
        events = send(client, sid, "answer 3")
        assert events[0][1]["understood"] is True
        assert events[0][1]["clarity"] == 100
        names = [e for e, _ in events]
        assert names[1] == "tools" and "notice" in names  # understood after 3 rounds (same as message cap)
        assert names.index("notice") < next(i for i, e in enumerate(names) if e == "token")
        wait_for_tools(client, sid)
        assert len(tool_calls) == 1


def test_spurious_new_problem_does_not_reset_context_or_turn_count(fake_llm):
    """Regression: the extractor mislabelled background answers as new_problem, so the message
    cap was never reached and the chat grilled forever."""
    script, tool_calls, _ = fake_llm
    with TestClient(app) as client:
        sid = client.post("/chat/sessions").json()["session_id"]
        script.append(analysis("new_problem", "Traffic is down 50%", facts=["500 customers before"]))
        send(client, sid, "traffic is down")
        for i, msg in enumerate(["we have 60 seats", "we use Toast"]):
            script.append(analysis("new_problem", "Owner describes seats/POS", facts=[f"detail {i}"],
                                   explicit_switch=False))
            events = send(client, sid, msg)
            assert events[0][1]["action"] == "refine"
            assert events[0][1]["problem"] == "Traffic is down 50%"
        assert "tools" in [e for e, _ in events]  # 3rd owner message on the same problem
        wait_for_tools(client, sid)
        assert len(tool_calls) == 1
        brief = tool_calls[0][0]
        assert "Traffic is down 50%" in brief
        for fact in ["500 customers before", "detail 0", "detail 1"]:
            assert fact in brief


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
