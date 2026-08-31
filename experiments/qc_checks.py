from __future__ import annotations

import csv
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import requests

EXPECTED_HPA_AVERAGE_VALUE = {"H50": "50", "H100": "100", "H115": "115"}
EXPECTED_AGENT_POLICY = {
    "G30": "policies/ablation_g30.yaml",
    "P30": "policies/ablation_p30.yaml",
    "F30": "policies/ablation_f30.yaml",
}
HPA_NAME_FOR_ARM = {
    "H50": "workload-cloud-rps", "H100": "workload-cloud-rps-100", "H115": "workload-cloud-rps-115",
}


def check_metrics_csv_complete(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "metrics_csv_missing"
    if path.stat().st_size == 0:
        return False, "metrics_csv_empty"
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return False, "metrics_csv_no_header"
        first_data_row = next(reader, None)
        if first_data_row is None:
            return False, "metrics_csv_no_data_rows"
    return True, ""


def check_no_pod_violations(violations: list[dict], label: str) -> tuple[bool, str]:
    if not violations:
        return True, ""
    parts = [
        f"{label}_pod_restart(uid={v['uid']},name={v['name']},"
        f"{v['old_restart_count']}->{v['new_restart_count']},pre_existing={v['pre_existing']})"
        for v in violations
    ]
    return False, "; ".join(parts)


def check_hpa_target_matches(actual_average_value: Optional[str], expected_average_value: str) -> tuple[bool, str]:
    if actual_average_value is None:
        return False, "hpa_target_unreadable"
    if actual_average_value != expected_average_value:
        return False, f"hpa_target_mismatch(actual={actual_average_value},expected={expected_average_value})"
    return True, ""


def check_policy_matches(actual_policy_env: Optional[str], expected_policy_path: str) -> tuple[bool, str]:
    if actual_policy_env is None:
        return False, "agent_policy_env_unreadable"
    if actual_policy_env != expected_policy_path:
        return False, f"policy_mismatch(actual={actual_policy_env},expected={expected_policy_path})"
    return True, ""


def check_no_prometheus_gap(timestamps: list[float], *, max_gap_seconds: float = 30.0) -> tuple[bool, str]:
    if not timestamps:
        return False, "no_prometheus_samples"
    ts = sorted(set(timestamps))
    if len(ts) < 2:
        return True, ""
    for a, b in zip(ts, ts[1:]):
        gap = b - a
        if gap > max_gap_seconds:
            return False, f"prometheus_gap_{gap:.1f}s_at_{a:.0f}"
    return True, ""


def run_all_qc(
    *,
    metrics_csv_path: Path,
    arm_id: str,
    is_hpa_arm: bool,
    is_agent_arm: bool,
    workload_pod_violations: list[dict],
    agent_pod_violations: Optional[list[dict]] = None,
    actual_hpa_average_value: Optional[str] = None,
    actual_agent_policy_env: Optional[str] = None,
    prometheus_timestamps: Optional[list[float]] = None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    ok, reason = check_metrics_csv_complete(metrics_csv_path)
    if not ok:
        reasons.append(reason)

    ok, reason = check_no_pod_violations(workload_pod_violations, "workload")
    if not ok:
        reasons.append(reason)

    if is_agent_arm:
        ok, reason = check_no_pod_violations(agent_pod_violations or [], "agent")
        if not ok:
            reasons.append(reason)
        expected_policy = EXPECTED_AGENT_POLICY.get(arm_id)
        if expected_policy is not None:
            ok, reason = check_policy_matches(actual_agent_policy_env, expected_policy)
            if not ok:
                reasons.append(reason)

    if is_hpa_arm:
        expected_target = EXPECTED_HPA_AVERAGE_VALUE.get(arm_id)
        if expected_target is not None:
            ok, reason = check_hpa_target_matches(actual_hpa_average_value, expected_target)
            if not ok:
                reasons.append(reason)

    if prometheus_timestamps is not None:
        ok, reason = check_no_prometheus_gap(prometheus_timestamps)
        if not ok:
            reasons.append(reason)

    return (len(reasons) == 0), reasons


def _kubectl(args: list[str]) -> Optional[str]:
    cp = subprocess.run(["kubectl", *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if cp.returncode != 0:
        return None
    out = cp.stdout.strip()
    return out if out else None


def get_hpa_average_value(namespace: str, hpa_name: str) -> Optional[str]:
    return _kubectl([
        "-n", namespace, "get", "hpa", hpa_name,
        "-o", "jsonpath={.spec.metrics[0].pods.target.averageValue}",
    ])


def get_agent_policy_env(namespace: str = "tools", deployment: str = "orchestration-agent") -> Optional[str]:
    val = _kubectl([
        "-n", namespace, "get", "deployment", deployment,
        "-o", "jsonpath={range .spec.template.spec.containers[0].env[?(@.name==\"AGENT_POLICY\")]}{.value}{end}",
    ])
    return val


def check_prometheus_reachable_for_formal_cells(
    prometheus_url: str,
    *,
    timeout: float = 5.0,
    get_fn: Callable[..., "requests.Response"] = requests.get,
) -> tuple[bool, str]:
    base = prometheus_url.rstrip("/")

    try:
        r = get_fn(f"{base}/-/healthy", timeout=timeout)
    except requests.RequestException as e:
        return False, f"prometheus_unreachable_at_{prometheus_url}:{e.__class__.__name__}"
    if r.status_code != 200:
        return False, f"prometheus_unhealthy_status_{r.status_code}"

    try:
        now = time.time()
        r = get_fn(
            f"{base}/api/v1/query_range",
            params={
                "query": 'kube_deployment_status_replicas{namespace="workload"}',
                "start": now - 30, "end": now, "step": "10s",
            },
            timeout=timeout,
        )
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, ValueError) as e:
        return False, f"prometheus_query_failed:{e.__class__.__name__}"

    if payload.get("status") != "success":
        return False, "prometheus_query_not_success"
    if not payload.get("data", {}).get("result"):
        return False, "prometheus_query_empty_result"

    return True, ""


def load_prometheus_timestamps(metrics_csv_path: Path) -> list[float]:
    if not metrics_csv_path.exists():
        return []
    out = []
    with metrics_csv_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                out.append(float(row["ts"]))
            except (KeyError, ValueError):
                continue
    return out


if __name__ == "__main__":
    print("qc_checks.py is a library module; import it from run_experiment.py.", file=sys.stderr)
    sys.exit(1)
