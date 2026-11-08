import json

import pytest
from fastapi.testclient import TestClient

from backend.api import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_demo_returns_top_three(client):
    r = client.get("/demo")
    assert r.status_code == 200
    body = r.json()
    assert len(body["top_problems"]) == 3
    assert body["restaurant"]["name"] == "Mario's Trattoria"


def test_demo_alpha_is_validated(client):
    assert client.get("/demo?alpha=1.5").status_code == 422


def test_upload_analyze_report_flow(client, restaurant):
    files = [("files", (name, payload, "text/csv")) for name, payload in restaurant.raw_files()]
    up = client.post("/uploads", files=files, data={"profile": json.dumps({"name": "Test", "sqft": 3000})})
    assert up.status_code == 200
    dataset_id = up.json()["dataset_id"]
    assert len(up.json()["domains"]) == 17

    assert client.get(f"/report/{dataset_id}").status_code == 409
    analyzed = client.post(f"/analyze/{dataset_id}")
    assert analyzed.status_code == 200
    report = client.get(f"/report/{dataset_id}?alpha=0.3")
    assert report.status_code == 200
    assert report.json()["alpha"] == 0.3


def test_unrecognized_upload_is_rejected(client):
    r = client.post("/uploads", files=[("files", ("x.csv", b"a,b\n1,2\n", "text/csv"))])
    assert r.status_code == 422


def test_unknown_dataset_is_404(client):
    assert client.get("/report/nope").status_code == 404
    assert client.post("/analyze/nope").status_code == 404


def test_taxonomy_lists_categories_with_tool_tags(client):
    cats = client.get("/taxonomy").json()
    assert len(cats) == 9
    assert all(c["tool_tags"] for c in cats)


def test_tools_requires_dataset_id(client):
    assert client.get("/tools").status_code == 422


def _fake_openrouter(monkeypatch, structured: dict):
    """Route the research (Responses API) and structuring (chat completions) calls."""
    from backend.tool_rec import llmConnection

    calls: list[tuple[str, dict]] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, json))
        if url == llmConnection.RESPONSES_URL:  # error: null is how OpenRouter reports success
            return FakeResponse({"error": None, "output": [
                {"type": "openrouter:web_fetch", "url": "https://example.com", "content": "x"},
                {"type": "message", "content": [{"type": "output_text", "text": "research notes"}]},
            ]})
        return FakeResponse({"error": None, "choices": [
            {"message": {"content": __import__("json").dumps(structured)}}]})

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llmConnection.requests, "post", fake_post)
    return calls


def test_get_tools_two_stage_returns_full_shape(monkeypatch):
    from backend.tool_rec import llmConnection

    def tool(n, name, cat):
        return {"tool_number": n, "tool_name": name, "tool_description": "d", "tool_pricing": "free",
                "tool_home_page": "https://example.com", "category": cat}

    calls = _fake_openrouter(monkeypatch, {"recommendations": [{
        "problem": "low traffic", "final_recommendation": "Use SEO",
        "tools": [tool(1, "Example", "highest_roi"), tool(2, "B", "platform_integrated"),
                  tool(3, "C", "highest_roi"), tool(4, "D", "platform_integrated")],
    }]})
    out = llmConnection.get_tools(["low traffic"])
    assert out["recommendations"][0]["tools"][0]["tool_name"] == "Example"
    assert out["recommendations"][0]["final_recommendation"] == "Use SEO"
    research, structure = calls
    assert "tools" in research[1] and "text" not in research[1]  # web research, no schema
    assert "tools" not in structure[1]  # schema is only enforced when no server tools are attached
    assert structure[1]["response_format"]["json_schema"]["strict"] is True


def test_get_tools_normalizes_incomplete_model_output(monkeypatch):
    """Missing keys (the real-world failure) must be filled so the frontend can always parse."""
    from backend.tool_rec import llmConnection

    _fake_openrouter(monkeypatch, {"recommendations": [{
        "tools": [{"tool_name": "Toast", "tool_description": "POS", "category": "weird"},
                  {"tool_description": "no name, dropped"}],
    }]})
    out = llmConnection.get_tools(["Slow turnover\nKey details from the owner:\n- 60 seats"])
    rec = out["recommendations"][0]
    assert rec["problem"] == "Slow turnover"
    assert rec["final_recommendation"] == ""
    assert rec["tools"] == [{
        "tool_number": 1, "tool_name": "Toast", "tool_description": "POS",
        "tool_pricing": "Not publicly available", "tool_home_page": "Not publicly available",
        "category": "highest_roi",
    }]


def test_get_tools_rejects_empty_result(monkeypatch):
    import pytest
    from backend.tool_rec import llmConnection

    _fake_openrouter(monkeypatch, {"recommendations": [{"problem": "x", "final_recommendation": "", "tools": []}]})
    with pytest.raises(RuntimeError):
        llmConnection.get_tools(["x"])


def test_tools_uses_analyzed_upload_not_demo(client, restaurant, monkeypatch):
    captured: list[list[str]] = []

    def fake_get_tools(problems):
        captured.append(problems)
        return {
            "recommendations": [
                {
                    "problem": problems[0],
                    "final_recommendation": "Test",
                    "tools": [
                        {
                            "tool_number": 1,
                            "tool_name": "Test",
                            "tool_description": "x",
                            "tool_pricing": "free",
                            "tool_home_page": "https://example.com",
                            "category": "highest_roi",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr("backend.api.get_tools", fake_get_tools)

    files = [("files", (name, payload, "text/csv")) for name, payload in restaurant.raw_files()]
    up = client.post("/uploads", files=files, data={"profile": json.dumps({"name": "Tools Eatery", "sqft": 3000})})
    dataset_id = up.json()["dataset_id"]
    client.post(f"/analyze/{dataset_id}")

    r = client.get(f"/tools?dataset_id={dataset_id}")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert body["recommendations"][0]["tools"][0]["tool_name"] == "Test"
    assert captured
    briefs = captured[0]
    assert len(briefs) == 3
    assert all(isinstance(b, str) for b in briefs)
    assert client.get(f"/report/{dataset_id}").json()["top_problems"][0]["summary"] in briefs[0]
