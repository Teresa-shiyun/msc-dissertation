from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import yaml

from .decision_engine import Bounds, Decision, decide
from .k8s_executor import K8sExecutor
from .log import EventLog
from .metrics_client import PrometheusClient, WorkloadMetrics
from .predictive_engine import (
    PredictiveState,
    decide_predictive,
    decide_predictive_guarded,
    load_model,
)


log = logging.getLogger("orchestration_agent")


DEFAULT_PROM_URL = os.getenv(
    "PROMETHEUS_URL",
    "http://kube-prom-stack-prometheus.monitoring.svc:9090",
)


def _load_policy(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_bounds(policy: dict) -> Bounds:
    b = policy.get("bounds", {})
    return Bounds(
        min_replicas=int(b.get("min_replicas", 1)),
        max_replicas=int(b.get("max_replicas", 10)),
    )


def _run_loop(
    policy: dict,
    policy_path: Path,
    prometheus_url: str,
    interval: float,
    dry_run: bool,
    log_file: Optional[str],
) -> None:
    target = policy["target"]
    namespace = target["namespace"]
    deployment = target["deployment"]
    pod_prefix = target.get("pod_prefix", deployment)
    tier = target.get("tier", "cloud")

    bounds = _build_bounds(policy)
    cool = policy.get("cooldowns", {})
    cool_up_s = float(cool.get("scale_up_seconds", 60))
    cool_down_s = float(cool.get("scale_down_seconds", 180))
    mode = policy.get("mode", "threshold")
    prediction_enabled = bool(policy.get("prediction_enabled_in_decision", True))
    arm_id = policy.get("arm_id", policy.get("name", "thresholds_v1"))
    predictive_model = None
    predictive_state = None
    if mode in ("predictive", "predictive_guarded"):
        model_path = Path(policy["model_path"])
        if not model_path.is_absolute():
            model_path = policy_path.parent / model_path
        predictive_model = load_model(model_path)
        predictive_state = PredictiveState()
    elif mode != "threshold":
        raise ValueError(f"unknown policy mode: {mode}")

    prom = PrometheusClient(prometheus_url)
    metrics = WorkloadMetrics(
        client=prom,
        namespace=namespace,
        deployment_pod_prefix=pod_prefix,
        tier=tier,
    )
    executor = K8sExecutor(namespace=namespace, deployment=deployment, dry_run=dry_run)
    events = EventLog(log_file)

    next_scaleup_ok = 0.0
    next_scaledown_ok = 0.0
    loop_epoch_start = time.time()

    stop = {"flag": False}
    def _handle_signal(signum, frame):
        log.info("received signal %s, will stop after current tick", signum)
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info(
        "agent started: policy=%s namespace=%s deployment=%s tier=%s interval=%.1fs dry_run=%s",
        policy.get("name", "thresholds_v1"), namespace, deployment, tier, interval, dry_run,
    )

    while not stop["flag"]:
        loop_start = time.monotonic()
        try:
            replicas = executor.get_replicas()
        except Exception as e:
            log.warning("could not read replicas: %s", e)
            time.sleep(interval)
            continue

        samples = metrics.sample()
        if mode == "predictive":
            assert predictive_model is not None
            assert predictive_state is not None
            decision: Decision = decide_predictive(
                samples,
                replicas,
                model=predictive_model,
                state=predictive_state,
                bounds=bounds,
            )
        elif mode == "predictive_guarded":
            assert predictive_model is not None
            assert predictive_state is not None
            decision = decide_predictive_guarded(
                samples,
                replicas,
                model=predictive_model,
                state=predictive_state,
                bounds=bounds,
                prediction_enabled=prediction_enabled,
            )
        else:
            decision = decide(samples, replicas, bounds)

        suppressed = False
        applied = False
        cooldown_until = None
        now = time.time()

        cooldown_eligible: Optional[bool] = None
        cooldown_remaining_s: Optional[float] = None
        if decision.action == "scale_up":
            cooldown_eligible = now >= next_scaleup_ok
            cooldown_remaining_s = max(0.0, next_scaleup_ok - now)
        elif decision.action == "scale_down":
            cooldown_eligible = now >= next_scaledown_ok
            cooldown_remaining_s = max(0.0, next_scaledown_ok - now)

        if decision.action == "scale_up":
            if now < next_scaleup_ok:
                suppressed = True
            else:
                try:
                    executor.set_replicas(decision.to_replicas)
                    applied = True
                    next_scaleup_ok = now + cool_up_s
                    cooldown_until = next_scaleup_ok
                except Exception as e:
                    log.error("scale_up patch failed: %s", e)
        elif decision.action == "scale_down":
            if now < next_scaledown_ok:
                suppressed = True
            else:
                try:
                    executor.set_replicas(decision.to_replicas)
                    applied = True
                    next_scaledown_ok = now + cool_down_s
                    cooldown_until = next_scaledown_ok
                except Exception as e:
                    log.error("scale_down patch failed: %s", e)

        event_fields = {
            "policy": policy.get("name", "thresholds_v1"),
            "arm_id": arm_id,
            "elapsed_seconds": round(now - loop_epoch_start, 1),
            "rule_matched": decision.rule_matched,
            "action": ("suppressed_by_cooldown" if suppressed else decision.action),
            "engine_action": decision.action,
            "current_replicas": decision.from_replicas,
            "from_replicas": decision.from_replicas,
            "to_replicas": (decision.to_replicas if applied else decision.from_replicas),
            "pre_cooldown_desired": decision.to_replicas,
            "replicas_status": decision.from_replicas,
            "cooldown_eligible": cooldown_eligible,
            "cooldown_remaining_seconds": (
                round(cooldown_remaining_s, 1) if cooldown_remaining_s is not None else None
            ),
            "inputs": decision.inputs,
            "applied": applied,
            "cooldown_until_epoch": cooldown_until,
            "dry_run": dry_run,
        }
        event_fields.update(decision.extra)
        events.emit(**event_fields)

        elapsed = time.monotonic() - loop_start
        sleep_for = max(0.0, interval - elapsed)
        if sleep_for > 0 and not stop["flag"]:
            time.sleep(sleep_for)

    events.close()
    log.info("agent stopped")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for cmd in ("run", "dry-run"):
        sp = sub.add_parser(cmd)
        sp.add_argument("--policy", required=True, type=Path,
                        help="Path to policy YAML, e.g. policies/thresholds_v1.yaml")
        sp.add_argument("--prometheus-url", default=DEFAULT_PROM_URL,
                        help="Prometheus base URL; defaults to in-cluster service")
        sp.add_argument("--interval", type=float, default=10.0,
                        help="Seconds between decisions (default: 10)")
        sp.add_argument("--log-file", default=None,
                        help="Optional path to also append JSONL events")
        sp.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    policy = _load_policy(args.policy)
    _run_loop(
        policy=policy,
        policy_path=args.policy,
        prometheus_url=args.prometheus_url,
        interval=args.interval,
        dry_run=(args.cmd == "dry-run"),
        log_file=args.log_file,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
