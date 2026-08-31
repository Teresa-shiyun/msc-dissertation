from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import requests


@dataclass(frozen=True)
class Query:
    metric: str
    expr: str
    keep_labels: tuple

    def emit_labels(self, series_labels: dict) -> str:
        parts = []
        for k in self.keep_labels:
            v = series_labels.get(k)
            if v is not None and v != "":
                parts.append(f"{k}={v}")
        return ",".join(parts)


QUERIES: list[Query] = [
    Query(
        metric="replicas_status",
        expr='kube_deployment_status_replicas{namespace="workload"}',
        keep_labels=("deployment",),
    ),
    Query(
        metric="replicas_spec",
        expr='kube_deployment_spec_replicas{namespace="workload"}',
        keep_labels=("deployment",),
    ),
    Query(
        metric="cpu_cores",
        expr=(
            'sum by (pod) ('
            '  rate(container_cpu_usage_seconds_total{namespace="workload",container="workload"}[1m])'
            ')'
        ),
        keep_labels=("pod",),
    ),
    Query(
        metric="cpu_utilisation",
        expr=(
            'sum by (pod) ('
            '  rate(container_cpu_usage_seconds_total{namespace="workload",container="workload"}[1m])'
            ')'
            ' / on(pod) sum by (pod) ('
            '  kube_pod_container_resource_requests{namespace="workload",container="workload",resource="cpu"}'
            ')'
        ),
        keep_labels=("pod",),
    ),
    Query(
        metric="mem_mib",
        expr=(
            'sum by (pod) ('
            '  container_memory_working_set_bytes{namespace="workload",container="workload"}'
            ') / (1024*1024)'
        ),
        keep_labels=("pod",),
    ),
    Query(
        metric="rps",
        expr='sum by (tier) (rate(workload_requests_total{tier!="",route="/work"}[1m]))',
        keep_labels=("tier",),
    ),
    Query(
        metric="lat50_ms",
        expr=(
            'histogram_quantile(0.50, sum by (le, tier) ('
            '  rate(workload_request_duration_seconds_bucket{tier!="",route="/work"}[2m])'
            ')) * 1000'
        ),
        keep_labels=("tier",),
    ),
    Query(
        metric="lat95_ms",
        expr=(
            'histogram_quantile(0.95, sum by (le, tier) ('
            '  rate(workload_request_duration_seconds_bucket{tier!="",route="/work"}[2m])'
            ')) * 1000'
        ),
        keep_labels=("tier",),
    ),
    Query(
        metric="lat99_ms",
        expr=(
            'histogram_quantile(0.99, sum by (le, tier) ('
            '  rate(workload_request_duration_seconds_bucket{tier!="",route="/work"}[2m])'
            ')) * 1000'
        ),
        keep_labels=("tier",),
    ),
    Query(
        metric="inflight",
        expr='sum by (tier) (workload_inflight_requests{tier!=""})',
        keep_labels=("tier",),
    ),
]


def _parse_ts(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        pass
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _query_range(
    base_url: str,
    expr: str,
    start: float,
    end: float,
    step: str,
    timeout: float,
) -> list[dict]:
    r = requests.get(
        f"{base_url}/api/v1/query_range",
        params={"query": expr, "start": start, "end": end, "step": step},
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus query failed: {payload}")
    return payload["data"].get("result", [])


def _yield_rows(query: Query, raw: Iterable[dict]):
    for series in raw:
        labels = series.get("metric", {})
        label_str = query.emit_labels(labels)
        for ts, value_str in series.get("values", []):
            try:
                v = float(value_str)
            except ValueError:
                continue
            if math.isnan(v) or math.isinf(v):
                continue
            yield (ts, query.metric, label_str, v)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Export Prometheus metrics for a time window into long-format CSV.")
    p.add_argument("--start", required=True,
                   help="Start time: epoch seconds OR ISO 8601 (e.g. 2026-05-02T13:05:00Z)")
    p.add_argument("--end", required=True, help="End time, same formats as --start")
    p.add_argument("--step", default="10s", help="Sample step, default 10s")
    p.add_argument("--prometheus-url", default=os.getenv("PROMETHEUS_URL", "http://localhost:9090"),
                   help="Prometheus base URL (env: PROMETHEUS_URL, default http://localhost:9090)")
    p.add_argument("--output", "-o", default="-", help="Output CSV path, '-' for stdout")
    p.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout per query, seconds")
    args = p.parse_args(argv)

    start = _parse_ts(args.start)
    end = _parse_ts(args.end)
    if end <= start:
        p.error("--end must be after --start")

    base = args.prometheus_url.rstrip("/")

    out_fp = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="")
    try:
        writer = csv.writer(out_fp)
        writer.writerow(["ts", "metric", "labels", "value"])
        total_rows = 0
        for q in QUERIES:
            try:
                raw = _query_range(base, q.expr, start, end, args.step, args.timeout)
            except (requests.RequestException, RuntimeError) as e:
                print(f"WARN: query for '{q.metric}' failed: {e}", file=sys.stderr)
                continue
            n = 0
            for row in _yield_rows(q, raw):
                writer.writerow(row)
                n += 1
            total_rows += n
            print(f"  {q.metric}: {n} rows", file=sys.stderr)
        print(f"total rows written: {total_rows}", file=sys.stderr)
    finally:
        if out_fp is not sys.stdout:
            out_fp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
