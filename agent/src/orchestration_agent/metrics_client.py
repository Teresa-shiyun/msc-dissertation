from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import requests


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricSamples:
    cpu_30s: Optional[float]   # mean CPU utilisation over last 30s (0.0–1.0+)
    cpu_60s: Optional[float]
    cpu_5m:  Optional[float]
    lat95_30s_ms: Optional[float]
    lat95_5m_ms:  Optional[float]
    rps_per_pod:  Optional[float]


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def query(self, expr: str) -> Optional[float]:
        try:
            r = requests.get(
                f"{self.base_url}/api/v1/query",
                params={"query": expr},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            log.warning("prometheus query failed: %s", e)
            return None

        if data.get("status") != "success":
            log.warning("prometheus error: %s", data)
            return None

        result = data["data"].get("result", [])
        if not result:
            return None
        try:
            value = float(result[0]["value"][1])
        except (KeyError, IndexError, ValueError):
            return None
        if math.isnan(value) or math.isinf(value):
            return None
        return value


def _cpu_per_pod_query(namespace: str, pod_prefix: str, container: str) -> str:
    pod_re = f"^{pod_prefix}-.*"
    return (
        f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",'
        f'container="{container}",pod=~"{pod_re}"}}[1m]))'
        f' / sum(kube_pod_container_resource_requests{{namespace="{namespace}",'
        f'container="{container}",pod=~"{pod_re}",resource="cpu"}})'
    )


def _windowed_avg(expr: str, window: str, step: str = "10s") -> str:
    return f"avg_over_time(({expr})[{window}:{step}])"


def _lat95_query(tier: str, rate_window: str = "2m") -> str:
    return (
        "histogram_quantile(0.95, sum by (le) ("
        f'rate(workload_request_duration_seconds_bucket{{tier="{tier}",route="/work"}}[{rate_window}])'
        ")) * 1000"
    )


def _rps_per_pod_query(tier: str, rate_window: str = "1m") -> str:
    return (
        f'sum(rate(workload_requests_total{{tier="{tier}",route="/work"}}[{rate_window}]))'
        f' / count(count by (pod) (workload_requests_total{{tier="{tier}"}}))'
    )


class WorkloadMetrics:
    def __init__(
        self,
        client: PrometheusClient,
        namespace: str,
        deployment_pod_prefix: str,
        tier: str,
        container: str = "workload",
    ) -> None:
        self.client = client
        self.cpu_expr = _cpu_per_pod_query(namespace, deployment_pod_prefix, container)
        self.lat_expr = _lat95_query(tier)
        self.rps_expr = _rps_per_pod_query(tier)

    def sample(self) -> MetricSamples:
        return MetricSamples(
            cpu_30s=self.client.query(_windowed_avg(self.cpu_expr, "30s")),
            cpu_60s=self.client.query(_windowed_avg(self.cpu_expr, "1m")),
            cpu_5m=self.client.query(_windowed_avg(self.cpu_expr, "5m")),
            lat95_30s_ms=self.client.query(_windowed_avg(self.lat_expr, "30s")),
            lat95_5m_ms=self.client.query(_windowed_avg(self.lat_expr, "5m")),
            rps_per_pod=self.client.query(self.rps_expr),
        )
