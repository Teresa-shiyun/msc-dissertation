from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Optional

from .metrics_client import MetricSamples


Action = Literal["scale_up", "scale_down", "hold"]


@dataclass(frozen=True)
class Bounds:
    min_replicas: int = 1
    max_replicas: int = 10


@dataclass(frozen=True)
class Decision:
    action: Action
    from_replicas: int
    to_replicas: int
    rule_matched: str
    inputs: MetricSamples = field(default_factory=lambda: MetricSamples(None, None, None, None, None, None))
    extra: dict = field(default_factory=dict)


def _gt(value: Optional[float], threshold: float) -> bool:
    return value is not None and value > threshold


def _lt(value: Optional[float], threshold: float) -> bool:
    return value is not None and value < threshold


def _clamp(target: int, replicas: int, bounds: Bounds, rule: str, inputs: MetricSamples) -> Decision:
    clamped = max(bounds.min_replicas, min(bounds.max_replicas, target))
    if clamped == replicas:
        return Decision("hold", replicas, replicas, f"{rule}_clamped", inputs)
    action: Action = "scale_up" if clamped > replicas else "scale_down"
    return Decision(action, replicas, clamped, rule, inputs)


def decide(samples: MetricSamples, replicas: int, bounds: Bounds = Bounds()) -> Decision:
    if _gt(samples.lat95_30s_ms, 500.0) or _gt(samples.cpu_30s, 0.80):
        target = replicas + math.ceil(max(replicas, 1) * 0.5)
        return _clamp(target, replicas, bounds, "R1_emergency", samples)

    if _gt(samples.cpu_60s, 0.65):
        return _clamp(replicas + 1, replicas, bounds, "R2_cpu_high", samples)

    if _lt(samples.cpu_5m, 0.25) and _lt(samples.lat95_5m_ms, 100.0):
        return _clamp(replicas - 1, replicas, bounds, "R3_idle", samples)

    return Decision("hold", replicas, replicas, "no_match", samples)
