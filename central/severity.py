"""Severity classification and the thread-safe threshold store."""
from __future__ import annotations

import threading

import config

LEVEL_NAMES = ("NORMAL", "ELEVATED", "WARNING", "CRITICAL")
COLOR_NAMES = ("GREEN", "YELLOW", "ORANGE", "RED")
LEVEL_COLORS = ("#2ecc71", "#f4c430", "#ff8c1a", "#df2531")


class ThresholdError(ValueError):
    pass


def validate_thresholds(metric: str, values) -> tuple[float, float, float]:
    try:
        y, o, r = (float(str(v).strip()) for v in values)
    except (TypeError, ValueError):
        raise ThresholdError(f"{metric}: Yellow, Orange and Red must be numbers.")
    if not (0 < y < o < r <= 100):
        raise ThresholdError(f"{metric}: must satisfy 0 < Yellow < Orange < Red ≤ 100 "
                             f"(got {y:g} / {o:g} / {r:g}).")
    return (y, o, r)


def classify(value, th) -> int:
    """0 = GREEN/NORMAL, 1 = YELLOW/ELEVATED, 2 = ORANGE/WARNING, 3 = RED/CRITICAL."""
    if value is None:
        return 0
    y, o, r = th
    if value >= r:
        return 3
    if value >= o:
        return 2
    if value >= y:
        return 1
    return 0


def classify_with_hysteresis(value, th, previous: int, hysteresis: float) -> int:
    """Like classify(), but a level only clears once the value is `hysteresis` % below it.
    Prevents alert/alarm flapping when a value hovers right on a threshold."""
    level = classify(value, th)
    if 0 < previous and level < previous and value >= th[previous - 1] - hysteresis:
        return previous
    return level


class ThresholdStore:
    """Shared between the GUI thread and the polling thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = {m: tuple(config.DEFAULT_THRESHOLDS[m]) for m in config.METRICS}

    def get(self, metric: str):
        with self._lock:
            return self._values[metric]

    def all(self) -> dict:
        with self._lock:
            return dict(self._values)

    def update(self, new: dict) -> dict:
        merged = self.all()
        merged.update(new)
        validated = {m: validate_thresholds(m, merged[m]) for m in config.METRICS}
        with self._lock:
            self._values = validated
        return validated

    def reset(self) -> dict:
        return self.update(config.DEFAULT_THRESHOLDS)

    def describe(self) -> str:
        return "  •  ".join(f"{m} {y:g}/{o:g}/{r:g}" for m, (y, o, r) in self.all().items())
