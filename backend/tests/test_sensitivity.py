from backend.shared.benchmarks import SCENARIOS


def test_sensitivity_reports_a_ranking_per_scenario(report):
    s = report["sensitivity"]
    assert set(s["rankings"]) == set(SCENARIOS)
    assert s["rankings"]["typical"] == [p["category"] for p in report["top_problems"]]


def test_planted_top_three_is_stable_across_benchmark_scenarios(report, restaurant):
    s = report["sensitivity"]
    assert s["top3_stable"]
    for ranking in s["rankings"].values():
        assert set(ranking) == set(restaurant.expected_top3)


def test_every_finding_range_brackets_the_typical_value(analysis):
    for f in analysis.cost.findings:
        assert f.low <= f.annual <= f.high, f.detector
    for f in analysis.time.findings:
        assert f.low <= f.hours_per_week <= f.high, f.detector
