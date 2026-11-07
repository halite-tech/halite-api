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
