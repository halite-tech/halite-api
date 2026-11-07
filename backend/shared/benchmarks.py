from __future__ import annotations

from pathlib import Path

import yaml

SCENARIOS = ("low", "typical", "high")
DEFAULT_PATH = Path(__file__).with_name("benchmarks.yaml")


class Benchmarks:
    """Benchmark lookups for one scenario (low / typical / high)."""

    def __init__(self, data: dict, scenario: str = "typical"):
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}")
        self.data = data
        self.scenario = scenario

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> "Benchmarks":
        return cls(yaml.safe_load(Path(path).read_text()))

    def with_scenario(self, scenario: str) -> "Benchmarks":
        return Benchmarks(self.data, scenario)

    def v(self, key: str) -> float:
        entry = self.data["values"][key]
        if isinstance(entry, dict):
            return float(entry[self.scenario])
        return float(entry)

    def addressability(self, category: str) -> float:
        return float(self.data["addressability"].get(category, 0.0))

    def tool_savings(self, task: str) -> float:
        return float(self.data["tool_savings"][task])
