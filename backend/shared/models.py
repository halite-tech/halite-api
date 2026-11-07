"""Result types shared by the Cost and Time engines."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Skipped:
    detector: str
    category: str
    label: str
    reason: str
    missing_domains: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"detector": self.detector, "category": self.category, "label": self.label,
                "reason": self.reason, "missing_domains": self.missing_domains}


def weighted_confidence(values: list[float], confidences: list[float]) -> float:
    total = sum(values)
    if total > 0:
        return sum(v * c for v, c in zip(values, confidences)) / total
    return sum(confidences) / len(confidences) if confidences else 0.0


def normalize_weights(scores: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, s) for s in scores.values())
    if total <= 0:
        return {k: 0.0 for k in scores}
    return {k: max(0.0, s) / total for k, s in scores.items()}
