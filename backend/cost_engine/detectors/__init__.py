"""Importing this package registers every cost detector in ``REGISTRY``."""
from backend.cost_engine.detectors import (  # noqa: F401
    channels,
    facilities,
    guest,
    hiring,
    inventory,
    labor,
    purchasing,
    revenue,
)
from backend.cost_engine.detectors.base import REGISTRY, Detector, Result

__all__ = ["REGISTRY", "Detector", "Result"]
