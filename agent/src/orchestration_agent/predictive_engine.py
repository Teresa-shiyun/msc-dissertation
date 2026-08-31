from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .decision_engine import Bounds, Decision
from .metrics_client import MetricSamples


@dataclass(frozen=True)
class PredictiveModel:
    feature_names: list[str]
    feature_mean: dict[str, float]
    feature_scale: dict[str, float]
    coefficients: dict[str, float]
    intercept: float
    capacity_rps_per_pod: float
    scale_margin: float = 1.10
    latency_guard_ms: float = 150.0
    cpu_guard: float = 0.80
    reactive_guard_margin: float = 1.20
    spike_guard_delta_rps: float = 100.0
    spike_guard_add_replicas: int = 1
    latency_guard_add_replicas: int = 1
    scale_down_lat95_guard_ms: Optional[float] = None
    forecast_source: str = "ridge"


@dataclass
class PredictiveState:
    total_rps_history: list[float] = field(default_factory=list)
    cpu_history: list[float] = field(default_factory=list)
    lat95_history: list[float] = field(default_factory=list)
    max_history: int = 6

    def update(self, *, total_rps: float, cpu: float, lat95_ms: float) -> None:
        self.total_rps_history.append(total_rps)
        self.cpu_history.append(cpu)
        self.lat95_history.append(lat95_ms)
        self.total_rps_history = self.total_rps_history[-self.max_history:]
        self.cpu_history = self.cpu_history[-self.max_history:]
        self.lat95_history = self.lat95_history[-self.max_history:]

    @staticmethod
    def _delta(xs: list[float], fallback: float) -> float:
        if len(xs) < 2:
            return 0.0
        return xs[-1] - xs[0] if len(xs) >= 4 else xs[-1] - xs[-2]

    def deltas(self) -> dict[str, float]:
        return {
            "d_total_rps": self._delta(self.total_rps_history, 0.0),
            "d_cpu_60s": self._delta(self.cpu_history, 0.0),
            "d_lat95_ms": self._delta(self.lat95_history, 0.0),
        }


def load_model(path: Path) -> PredictiveModel:
    data = json.loads(path.read_text(encoding="utf-8"))
    return PredictiveModel(
        feature_names=list(data["feature_names"]),
        feature_mean={k: float(v) for k, v in data["feature_mean"].items()},
        feature_scale={k: float(v) for k, v in data["feature_scale"].items()},
        coefficients={k: float(v) for k, v in data["coefficients"].items()},
        intercept=float(data["intercept"]),
        capacity_rps_per_pod=float(data["capacity_rps_per_pod"]),
        scale_margin=float(data.get("scale_margin", 1.10)),
        latency_guard_ms=float(data.get("latency_guard_ms", 150.0)),
        cpu_guard=float(data.get("cpu_guard", 0.80)),
        reactive_guard_margin=float(data.get("reactive_guard_margin", 1.20)),
        spike_guard_delta_rps=float(data.get("spike_guard_delta_rps", 100.0)),
        spike_guard_add_replicas=int(data.get("spike_guard_add_replicas", 1)),
        latency_guard_add_replicas=int(data.get("latency_guard_add_replicas", 1)),
        scale_down_lat95_guard_ms=(
            float(data["scale_down_lat95_guard_ms"])
            if data.get("scale_down_lat95_guard_ms") is not None
            else None
        ),
        forecast_source=str(data.get("forecast_source", "ridge")),
    )


def _or(value: Optional[float], fallback: float) -> float:
    return fallback if value is None else float(value)


def _predict(model: PredictiveModel, features: dict[str, float]) -> float:
    y = model.intercept
    for name in model.feature_names:
        scale = model.feature_scale.get(name, 1.0) or 1.0
        z = (features.get(name, 0.0) - model.feature_mean.get(name, 0.0)) / scale
        y += model.coefficients.get(name, 0.0) * z
    return max(0.0, y)


def _forecast_total_rps(model: PredictiveModel, features: dict[str, float], total_rps: float) -> float:
    if model.forecast_source == "persistence":
        return max(0.0, total_rps)
    return _predict(model, features)


def decide_predictive(
    samples: MetricSamples,
    replicas: int,
    *,
    model: PredictiveModel,
    state: PredictiveState,
    bounds: Bounds,
) -> Decision:
    safe_replicas = max(replicas, 1)
    cpu_60s = _or(samples.cpu_60s, _or(samples.cpu_30s, 0.0))
    lat95 = _or(samples.lat95_30s_ms, _or(samples.lat95_5m_ms, 0.0))
    rps_per_pod = _or(samples.rps_per_pod, 0.0)
    total_rps = max(0.0, rps_per_pod * safe_replicas)

    state.update(total_rps=total_rps, cpu=cpu_60s, lat95_ms=lat95)
    deltas = state.deltas()

    features = {
        "replicas": float(safe_replicas),
        "total_rps": total_rps,
        "rps_per_pod": rps_per_pod,
        "cpu_60s": cpu_60s,
        "lat95_ms": lat95,
        **deltas,
    }
    predicted_total_rps = _predict(model, features)
    predicted_replicas = math.ceil((predicted_total_rps * model.scale_margin) / model.capacity_rps_per_pod)
    predicted_replicas = max(bounds.min_replicas, min(bounds.max_replicas, predicted_replicas))
    desired = predicted_replicas

    hotness_triggered = cpu_60s > model.cpu_guard or lat95 > model.latency_guard_ms
    if hotness_triggered:
        desired = max(desired, min(bounds.max_replicas, safe_replicas + 1))

    winning_candidate = "hotness_safeguard" if (hotness_triggered and desired > predicted_replicas) else "predict"

    extra = {
        "predicted_rps": round(predicted_total_rps, 2),
        "predicted_replicas": predicted_replicas,
        "current_total_rps": round(total_rps, 2),
        "reactive_guard_replicas": None,
        "latency_guard_replicas": None,
        "cpu_guard_replicas": None,
        "spike_guard_replicas": None,
        "hotness_safeguard_triggered": hotness_triggered,
        "guards_enabled": False,
        "prediction_enabled_in_decision": True,
        "pre_cooldown_desired": desired,
        "winning_candidate": winning_candidate,
        "reason": winning_candidate,
    }

    if desired > safe_replicas:
        return Decision("scale_up", replicas, desired, "P1_predictive_scale_up", samples, extra)

    can_scale_down = (
        desired < safe_replicas
        and _or(samples.cpu_5m, cpu_60s) < 0.45
        and _or(samples.lat95_5m_ms, lat95) < model.latency_guard_ms
    )
    if can_scale_down:
        return Decision("scale_down", replicas, max(bounds.min_replicas, safe_replicas - 1), "P2_predictive_scale_down", samples, extra)

    return Decision("hold", replicas, replicas, "P0_predictive_hold", samples, extra)


def decide_predictive_guarded(
    samples: MetricSamples,
    replicas: int,
    *,
    model: PredictiveModel,
    state: PredictiveState,
    bounds: Bounds,
    prediction_enabled: bool = True,
) -> Decision:
    safe_replicas = max(replicas, 1)
    cpu_60s = _or(samples.cpu_60s, _or(samples.cpu_30s, 0.0))
    lat95 = _or(samples.lat95_30s_ms, _or(samples.lat95_5m_ms, 0.0))
    rps_per_pod = _or(samples.rps_per_pod, 0.0)
    total_rps = max(0.0, rps_per_pod * safe_replicas)

    state.update(total_rps=total_rps, cpu=cpu_60s, lat95_ms=lat95)
    deltas = state.deltas()

    features = {
        "replicas": float(safe_replicas),
        "total_rps": total_rps,
        "rps_per_pod": rps_per_pod,
        "cpu_60s": cpu_60s,
        "lat95_ms": lat95,
        **deltas,
    }
    predicted_total_rps = _forecast_total_rps(model, features, total_rps)
    cap = model.capacity_rps_per_pod

    predicted_replicas = math.ceil((predicted_total_rps * model.scale_margin) / cap)

    reactive_replicas = math.ceil((total_rps * model.reactive_guard_margin) / cap)

    latency_guard_triggered = lat95 > model.latency_guard_ms
    latency_guard_replicas = (
        safe_replicas + model.latency_guard_add_replicas if latency_guard_triggered else 0
    )

    cpu_guard_triggered = cpu_60s > model.cpu_guard
    cpu_guard_replicas = (safe_replicas + 1) if cpu_guard_triggered else 0

    d_total_rps = deltas["d_total_rps"]
    spike_guard_triggered = d_total_rps > model.spike_guard_delta_rps
    spike_guard_replicas = (
        safe_replicas + model.spike_guard_add_replicas if spike_guard_triggered else 0
    )

    candidates = [
        reactive_replicas,
        latency_guard_replicas,
        cpu_guard_replicas,
        spike_guard_replicas,
    ]
    if prediction_enabled:
        candidates.append(predicted_replicas)
    guarded_desired = max(candidates)
    desired = max(bounds.min_replicas, min(bounds.max_replicas, guarded_desired))

    triggers = []
    if prediction_enabled and reactive_replicas >= predicted_replicas and reactive_replicas > 0:
        triggers.append("reactive")
    elif not prediction_enabled and reactive_replicas > 0:
        triggers.append("reactive")
    if latency_guard_triggered:
        triggers.append("latency")
    if cpu_guard_triggered:
        triggers.append("cpu")
    if spike_guard_triggered:
        triggers.append("spike")
    reason = ("predict_only" if not triggers else "+".join(triggers))

    _named = [("reactive", reactive_replicas), ("latency", latency_guard_replicas),
              ("cpu", cpu_guard_replicas), ("spike", spike_guard_replicas)]
    if prediction_enabled:
        _named.append(("predict", predicted_replicas))
    winning_candidate = max(_named, key=lambda kv: kv[1])[0] if any(v for _, v in _named) else "floor"

    extra = {
        "predicted_rps": round(predicted_total_rps, 2),
        "predicted_replicas": predicted_replicas,
        "current_total_rps": round(total_rps, 2),
        "reactive_guard_replicas": reactive_replicas,
        "latency_guard_replicas": latency_guard_replicas,
        "cpu_guard_replicas": cpu_guard_replicas,
        "spike_guard_replicas": spike_guard_replicas,
        "latency_guard_triggered": latency_guard_triggered,
        "cpu_guard_triggered": cpu_guard_triggered,
        "spike_guard_triggered": spike_guard_triggered,
        "hotness_safeguard_triggered": None,
        "guards_enabled": True,
        "prediction_enabled_in_decision": prediction_enabled,
        "guarded_desired_replicas": guarded_desired,
        "pre_cooldown_desired": desired,
        "winning_candidate": winning_candidate,
        "reason": reason,
    }

    if desired > safe_replicas:
        return Decision("scale_up", replicas, desired, "PG1_guarded_scale_up", samples, extra)

    scale_down_lat = (
        model.scale_down_lat95_guard_ms
        if model.scale_down_lat95_guard_ms is not None
        else model.latency_guard_ms
    )
    can_scale_down = (
        desired < safe_replicas
        and _or(samples.cpu_5m, cpu_60s) < 0.45
        and _or(samples.lat95_5m_ms, lat95) < scale_down_lat
    )
    if can_scale_down:
        return Decision(
            "scale_down", replicas, max(bounds.min_replicas, safe_replicas - 1),
            "PG2_guarded_scale_down", samples, extra,
        )

    return Decision("hold", replicas, replicas, "PG0_guarded_hold", samples, extra)
