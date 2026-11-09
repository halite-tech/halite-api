import json

import pytest
from fastapi.testclient import TestClient

from backend.api import app
from backend.auth import current_user_id


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

    listed = client.get("/datasets")
    assert listed.status_code == 200
    ids = [d["dataset_id"] for d in listed.json()["datasets"]]
    assert dataset_id in ids
    one = client.get(f"/datasets/{dataset_id}")
    assert one.status_code == 200
    assert one.json()["analyzed"] is False
    assert one.json()["name"] == "Test"
    assert "sales" in one.json()["domains"]

    assert client.get(f"/report/{dataset_id}").status_code == 200
    analyzed = client.post(f"/analyze/{dataset_id}")
    assert analyzed.status_code == 200
    report = client.get(f"/report/{dataset_id}?alpha=0.3")
    assert report.status_code == 200
    assert report.json()["alpha"] == 0.3
    assert client.get(f"/datasets/{dataset_id}").json()["analyzed"] is True

    replacement = [("cash_drawer.csv", next(payload for name, payload in restaurant.raw_files() if name == "cash_drawer.csv"), "text/csv")]
    replaced = client.post(f"/uploads", files=[("files", replacement[0])], data={"dataset_id": dataset_id})
    assert replaced.status_code == 200
    filenames = [file["filename"] for file in client.get(f"/datasets/{dataset_id}").json()["files"]]
    assert filenames.count("cash_drawer.csv") == 1


def test_unrecognized_upload_is_rejected(client):
    r = client.post("/uploads", files=[("files", ("x.csv", b"a,b\n1,2\n", "text/csv"))])
    assert r.status_code == 422


def test_unknown_dataset_is_404(client):
    assert client.get("/report/nope").status_code == 404
    assert client.post("/analyze/nope").status_code == 404
    assert client.get("/datasets/nope").status_code == 404


def test_datasets_are_scoped_to_the_authenticated_user(client, restaurant):
    app.dependency_overrides[current_user_id] = lambda: "business-a"
    try:
        uploaded = client.post(
            "/uploads",
            files=[("files", (name, payload, "text/csv")) for name, payload in restaurant.raw_files()],
        )
        assert uploaded.status_code == 200
        dataset_id = uploaded.json()["dataset_id"]

        app.dependency_overrides[current_user_id] = lambda: "business-b"
        assert all(item["dataset_id"] == "demo" for item in client.get("/datasets").json()["datasets"])
        assert client.get(f"/datasets/{dataset_id}").status_code == 404
        assert client.post(f"/analyze/{dataset_id}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_chat_session_rebuilds_analysis_after_restart(client, restaurant):
    files = [("files", (name, payload, "text/csv")) for name, payload in restaurant.raw_files()]
    up = client.post("/uploads", files=files, data={"profile": json.dumps({"name": "Chat Reload"})})
    assert up.status_code == 200
    dataset_id = up.json()["dataset_id"]
    from backend.api import ANALYSES
    ANALYSES.pop(dataset_id, None)

    session = client.post("/chat/sessions", json={"dataset_id": dataset_id})
    assert session.status_code == 200
    assert dataset_id in ANALYSES


def test_report_lazy_loads_persisted_dataset_after_restart(client, restaurant):
    app.dependency_overrides[current_user_id] = lambda: "reload-user"
    try:
        files = [("files", (name, payload, "text/csv")) for name, payload in restaurant.raw_files()]
        up = client.post("/uploads", files=files)
        assert up.status_code == 200
        dataset_id = up.json()["dataset_id"]
        from backend.api import ANALYSES, DATASETS, DATASET_OWNERS
        ANALYSES.pop(dataset_id, None)
        DATASETS.pop(dataset_id, None)
        DATASET_OWNERS.pop(dataset_id, None)

        report = client.get(f"/report/{dataset_id}")
        assert report.status_code == 200
        assert report.json()["files"]
    finally:
        app.dependency_overrides.clear()


def test_bulk_delete_files(client):
    files = [
        ("files", ("sales.csv", b"Order #,Order Time,Net Sales\n1,2026-01-01,10\n", "text/csv")),
        ("files", ("cash_drawer.csv", b"Date,Expected Cash,Counted Cash\n2026-01-01,10,10\n", "text/csv")),
    ]
    up = client.post("/uploads", files=files)
    assert up.status_code == 200
    dataset_id = up.json()["dataset_id"]
    deleted = client.request("DELETE", f"/datasets/{dataset_id}/files/bulk", json=["sales.csv", "cash_drawer.csv"])
    assert deleted.status_code == 200
    assert deleted.json()["files"] == []


def test_context_deduplicates_files_across_legacy_user_records(client, restaurant):
    app.dependency_overrides[current_user_id] = lambda: "dedupe-user"
    try:
        files = [("files", (name, payload, "text/csv")) for name, payload in restaurant.raw_files()]
        first = client.post("/uploads", files=files)
        assert first.status_code == 200
        second = client.post("/uploads", files=files[:1])
        assert second.status_code == 200

        context = client.get("/context")
        assert context.status_code == 200
        filenames = [file["filename"] for file in context.json()["files"]]
        assert len(filenames) == len(set(filenames)) == len(restaurant.raw_files())
        assert all(file["last_modified"] > 0 for file in context.json()["files"])
        assert all("payload" not in file for file in context.json()["files"])
        deleted = client.request("DELETE", "/context/files", json=filenames)
        assert deleted.status_code == 200
        assert deleted.json()["files"] == []
        repeated = client.request("DELETE", "/context/files", json=filenames)
        assert repeated.status_code == 200
        assert repeated.json()["files"] == []
    finally:
        app.dependency_overrides.clear()


def test_taxonomy_lists_categories_with_tool_tags(client):
    cats = client.get("/taxonomy").json()
    assert len(cats) == 9
    assert all(c["tool_tags"] for c in cats)


def test_tools_requires_dataset_id(client):
    assert client.get("/tools").status_code == 422


def test_selected_tool_problem_can_use_visible_third_problem():
    from backend.api import _problem_briefs_for_tools

    report = {
        "top_problems": [
            {"label": "Labor Scheduling", "category": "labor_scheduling", "summary": "labor"},
            {"label": "Hiring and Retention", "category": "hiring_retention", "summary": "hiring"},
            {"label": "Inventory and Waste", "category": "inventory_waste", "summary": "inventory"},
        ]
    }
    assert _problem_briefs_for_tools(report) == ["Labor Scheduling: labor", "Hiring and Retention: hiring"]
    assert _problem_briefs_for_tools(report, "Inventory and Waste") == ["Inventory and Waste: inventory"]


def test_demo_tools_uses_demo_problems(client, monkeypatch):
    captured: list[list[str]] = []

    def fake_get_tools(problems):
        captured.append(problems)
        return {
            "recommendations": [
                {
                    "problem": problems[0],
                    "final_recommendation": "Demo pick",
                    "tools": [
                        {
                            "tool_number": 1,
                            "tool_name": "DemoTool",
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
    r = client.get("/demo/tools?alpha=0.7")
    assert r.status_code == 200
    body = r.json()
    assert body["recommendations"][0]["tools"][0]["tool_name"] == "DemoTool"
    assert captured
    demo_report = client.get("/demo?alpha=0.7").json()
    top = demo_report["top_problems"][0]
    expected = f"{top['label']}: {top['summary']}"
    assert captured[0][0] == expected


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
        return {
            "tool_number": n,
            "tool_name": name,
            "tool_tagline": "Quick intro.",
            "tool_description": "d",
            "tool_pricing": "free",
            "tool_home_page": "https://example.com",
            "category": cat,
            "solution_tags": ["ai_llm"],
        }

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
        "tool_number": 1,
        "tool_name": "Toast",
        "tool_tagline": "POS",
        "tool_description": "POS",
        "tool_pricing": "Not publicly available",
        "tool_home_page": "Not publicly available",
        "tool_logo_url": "Not publicly available",
        "category": "highest_roi",
        "solution_tags": ["other"],
        "solution_types": [{
            "id": "other",
            "label": "Restaurant ops",
            "description": "General operations software",
        }],
    }]


def test_logo_url_from_homepage_is_not_synthesized(monkeypatch):
    from backend.tool_rec import llmConnection

    _fake_openrouter(monkeypatch, {"recommendations": [{
        "problem": "x",
        "final_recommendation": "Toast",
        "tools": [{
            "tool_number": 1,
            "tool_name": "Toast",
            "tool_tagline": "t",
            "tool_description": "d",
            "tool_pricing": "free",
            "tool_home_page": "https://pos.toasttab.com/pricing",
            "category": "platform_integrated",
            "solution_tags": ["pos", "payments"],
        }],
    }]})
    out = llmConnection.get_tools(["x"])
    tool = out["recommendations"][0]["tools"][0]
    assert tool["tool_logo_url"] == "Not publicly available"
    assert tool["solution_types"][0]["label"] == "POS"
    assert tool["solution_types"][1]["label"] == "Payments"


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
    assert len(briefs) == 2
    assert all(isinstance(b, str) for b in briefs)
    assert client.get(f"/report/{dataset_id}").json()["top_problems"][0]["summary"] in briefs[0]
