import pytest

from backend.ingest.loader import build_dataset
from backend.ranker import MIN_CONFIDENCE, analyze, build_report

TOLERANCE = 0.15
PLANTED = ("waste", "overtime", "overstaffing", "turnover", "unfilled_roles", "price_creep")


@pytest.mark.parametrize("detector", PLANTED)
def test_planted_problem_estimates_within_tolerance(analysis, restaurant, detector):
    finding = analysis.cost.finding(detector)
    assert finding is not None, f"{detector} did not run"
    truth = restaurant.truth[detector]
    assert finding.annual == pytest.approx(truth, rel=TOLERANCE)


def test_engine_finds_the_planted_top_three(report, restaurant):
    top = [p["category"] for p in report["top_problems"]]
    assert len(top) == 3
    assert set(top) == set(restaurant.expected_top3)
    assert [p["rank"] for p in report["top_problems"]] == [1, 2, 3]


def test_healthy_areas_stay_out_of_the_top_three(report):
    healthy = {"pos_leakage", "facilities", "order_channels", "guest_marketing", "back_office_admin"}
    assert healthy.isdisjoint(p["category"] for p in report["top_problems"])


def test_weights_are_normalized(analysis):
    assert sum(analysis.cost.weights().values()) == pytest.approx(1.0)
    assert sum(analysis.time.weights().values()) == pytest.approx(1.0)


def test_alpha_shifts_ranking_between_money_and_time(analysis):
    money_only = build_report(analysis, alpha=1.0)
    time_only = build_report(analysis, alpha=0.0)
    cw, tw = analysis.cost.weights(), analysis.time.weights()
    assert money_only["top_problems"][0]["category"] == max(cw, key=cw.get)
    assert time_only["top_problems"][0]["category"] == max(tw, key=tw.get)


def test_every_number_has_evidence_and_a_range(report):
    for problem in report["top_problems"]:
        assert problem["summary"]
        assert problem["tool_tags"]
        lo, hi = problem["cost"]["range"]
        assert lo <= problem["cost"]["annual"] <= hi
        for d in problem["drivers"]:
            assert d["evidence"]


def test_recoverable_cost_applies_addressability(report):
    for p in report["all_problems"]:
        if p["cost"]:
            assert p["cost"]["recoverable"] == pytest.approx(
                p["cost"]["annual"] * p["cost"]["addressability"], abs=0.02)


def test_missing_domain_marks_category_not_assessed(restaurant):
    files = [(n, b) for n, b in restaurant.raw_files()
             if n not in ("utilities.csv", "equipment_log.csv")]
    report = build_report(analyze(build_dataset(files, restaurant.profile)))
    not_assessed = {p["category"]: p for p in report["not_assessed"]}
    assert "facilities" in not_assessed
    domains = {m["domain"] for m in not_assessed["facilities"]["missing"]}
    assert {"utilities", "equipment"} <= domains


def test_low_confidence_category_is_gated_out_of_top_three(restaurant):
    ds = build_dataset(restaurant.raw_files(), restaurant.profile)
    for domain in ("employees", "requisitions"):
        q = ds.quality[domain]
        q.completeness = q.coverage = q.consistency = 0.1
    report = build_report(analyze(ds))
    assert "hiring_retention" not in [p["category"] for p in report["top_problems"]]
    gated = {p["category"]: p for p in report["needs_more_data"]}
    assert "hiring_retention" in gated
    assert gated["hiring_retention"]["confidence"] < MIN_CONFIDENCE


def test_report_is_json_serializable(report):
    import json
    json.dumps(report)
