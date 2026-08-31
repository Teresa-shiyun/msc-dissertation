from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Optional

CPU_LIMIT_MILLI_DEFAULT = 1000

def compute_probe_cpu_pct(milli_samples: list[int], cpu_limit_milli: int = CPU_LIMIT_MILLI_DEFAULT) -> float:
    if not milli_samples:
        return float("nan")
    steady = milli_samples[1:] if len(milli_samples) > 1 else milli_samples
    if not steady:
        return float("nan")
    return 100.0 * (sum(steady) / len(steady)) / cpu_limit_milli


def load_raw_cpu_samples_by_probe(csv_path: Path, id_column: str = "rep") -> dict[str, list[int]]:
    by_probe: dict[str, list[int]] = {}
    with Path(csv_path).open("r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                pid = row[id_column]
                milli = int(row["milli_cpu"])
            except (KeyError, ValueError):
                continue
            by_probe.setdefault(pid, []).append(milli)
    return by_probe


def load_cpu_by_probe(csv_path: Path, id_column: str = "rep",
                       cpu_limit_milli: int = CPU_LIMIT_MILLI_DEFAULT) -> dict[str, float]:
    by_probe = load_raw_cpu_samples_by_probe(csv_path, id_column)
    return {pid: compute_probe_cpu_pct(vals, cpu_limit_milli) for pid, vals in by_probe.items()}


def parse_k6_summary(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    met = data["metrics"]
    return {
        "achieved_rps": met["http_reqs"]["rate"],
        "p50_ms": met["http_req_duration"].get("med"),
        "p95_ms": met["http_req_duration"]["p(95)"],
        "p99_ms": None,
        "fail_rate": met["http_req_failed"]["value"],
        "n_reqs": met["http_reqs"]["count"],
    }


def check_probe_validity(
    *,
    k6_exit_ok: bool,
    summary: dict,
    target_rps: float,
    cpu_samples: list[int],
    pod_restarts_before: Optional[int],
    pod_restarts_after: Optional[int],
    tunnel_alive: bool,
    min_achieved_fraction: float = 0.98,
) -> tuple[bool, str]:
    if not k6_exit_ok:
        return False, "k6_exit_nonzero"
    if summary.get("fail_rate", 1.0) != 0.0:
        return False, "fail_rate_nonzero"
    if summary.get("achieved_rps", 0.0) < min_achieved_fraction * target_rps:
        return False, "achieved_rps_below_98pct_target"
    if summary.get("p95_ms") is None:
        return False, "missing_latency_sample"
    if not cpu_samples:
        return False, "missing_cpu_sample"
    if pod_restarts_before is None or pod_restarts_after is None:
        return False, "restart_count_unavailable"
    if pod_restarts_after != pod_restarts_before:
        return False, "pod_restarted_during_probe"
    if not tunnel_alive:
        return False, "port_forward_or_loadgen_interrupted"
    return True, ""


def robust_stats(values: list[float]) -> tuple[float, float, float]:
    centre = statistics.median(values)
    mad = statistics.median([abs(v - centre) for v in values])
    robust_sigma = 1.4826 * mad
    return centre, mad, robust_sigma


def compute_allowance(centre: float, robust_sigma: float, *, pct_of_centre: float, floor: float) -> float:
    return max(2 * robust_sigma, pct_of_centre * centre, floor)


def stability_gate(
    p95_values: list[float], cpu_values: list[float],
    *, max_p95_ratio: float = 4.0, max_cpu_spread_pp: float = 30.0,
) -> tuple[bool, str]:
    if not p95_values or not cpu_values:
        return False, "empty_reference_set"
    p95_ratio = max(p95_values) / min(p95_values) if min(p95_values) > 0 else float("inf")
    cpu_spread = max(cpu_values) - min(cpu_values)
    if p95_ratio > max_p95_ratio:
        return False, f"p95_ratio_{p95_ratio:.2f}_exceeds_{max_p95_ratio}"
    if cpu_spread > max_cpu_spread_pp:
        return False, f"cpu_spread_{cpu_spread:.2f}pp_exceeds_{max_cpu_spread_pp}pp"
    return True, ""
