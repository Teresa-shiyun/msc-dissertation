from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional


def _kubectl(args: list[str]) -> Optional[str]:
    cp = subprocess.run(["kubectl", *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if cp.returncode != 0:
        return None
    out = cp.stdout.strip()
    return out if out else None


def _kubectl_json(args: list[str]) -> Optional[dict]:
    out = _kubectl(args)
    if out is None:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _parse_top_nodes_text(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5:
            rows.append({
                "node": parts[0], "cpu_milli": parts[1], "cpu_pct": parts[2],
                "mem_mib": parts[3], "mem_pct": parts[4],
            })
    return rows


def sample_node_top() -> list[dict]:
    out = _kubectl(["top", "nodes", "--no-headers"])
    if out is None:
        return []
    return _parse_top_nodes_text(out)


def sample_node_conditions() -> list[dict]:
    data = _kubectl_json(["get", "nodes", "-o", "json"])
    if data is None:
        return []
    out = []
    for n in data.get("items", []):
        conds = {c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])}
        out.append({"node": n["metadata"]["name"], "conditions": conds})
    return out


def _parse_top_pods_text(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append({"pod": parts[0], "cpu_milli": parts[1], "mem_mib": parts[2]})
    return rows


def sample_workload_pod_cpu(namespace: str = "workload", label_selector: str = "app=workload,tier=cloud") -> list[dict]:
    out = _kubectl(["top", "pod", "-n", namespace, "-l", label_selector, "--no-headers"])
    if out is None:
        return []
    return _parse_top_pods_text(out)


def _parse_pod_terminations_json(data: dict) -> list[dict]:
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        cs = (status.get("containerStatuses") or [{}])[0]
        last = cs.get("lastState", {}).get("terminated")
        out.append({
            "pod": meta.get("name"), "uid": meta.get("uid"),
            "phase": status.get("phase"),
            "restart_count": cs.get("restartCount"),
            "last_termination_reason": last.get("reason") if last else None,
            "last_termination_exit_code": last.get("exitCode") if last else None,
        })
    return out


def sample_pod_terminations(namespace: str = "workload", label_selector: str = "app=workload,tier=cloud") -> list[dict]:
    data = _kubectl_json(["-n", namespace, "get", "pods", "-l", label_selector, "-o", "json"])
    if data is None:
        return []
    return _parse_pod_terminations_json(data)


def sample_cpu_throttling(namespace: str = "workload", prometheus_url: str = "http://localhost:9090") -> Optional[list[dict]]:
    import requests
    try:
        r = requests.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": f'container_cpu_cfs_throttled_seconds_total{{namespace="{namespace}"}}'},
            timeout=5,
        )
        data = r.json()
    except Exception:
        return None
    result = data.get("data", {}).get("result", [])
    if not result:
        return None
    return [{"pod": s["metric"].get("pod"), "value": s["value"][1]} for s in result]


def sample_docker_node_stats(node_names: list[str]) -> list[dict]:
    out = []
    for node in node_names:
        cp = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             '{"name":"{{.Name}}","cpu_pct":"{{.CPUPerc}}","mem_usage":"{{.MemUsage}}","mem_pct":"{{.MemPerc}}"}',
             node],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if cp.returncode == 0 and cp.stdout.strip():
            try:
                out.append(json.loads(cp.stdout.strip().splitlines()[0]))
            except (json.JSONDecodeError, IndexError):
                pass
    return out


def sample_host_snapshot(*, ts: float, node_names: list[str],
                          namespace: str = "workload",
                          label_selector: str = "app=workload,tier=cloud",
                          prometheus_url: str = "http://localhost:9090") -> dict:
    return {
        "ts": ts,
        "node_top": sample_node_top(),
        "node_conditions": sample_node_conditions(),
        "workload_pod_cpu": sample_workload_pod_cpu(namespace, label_selector),
        "pod_terminations": sample_pod_terminations(namespace, label_selector),
        "cpu_throttling": sample_cpu_throttling(namespace, prometheus_url),
        "docker_node_stats": sample_docker_node_stats(node_names),
    }


class HostMonitor:
    def __init__(self, node_names: list[str], namespace: str = "workload",
                 label_selector: str = "app=workload,tier=cloud",
                 prometheus_url: str = "http://localhost:9090"):
        self.node_names = node_names
        self.namespace = namespace
        self.label_selector = label_selector
        self.prometheus_url = prometheus_url
        self.snapshots: list[dict] = []

    def sample(self, ts: float) -> None:
        self.snapshots.append(sample_host_snapshot(
            ts=ts, node_names=self.node_names, namespace=self.namespace,
            label_selector=self.label_selector, prometheus_url=self.prometheus_url,
        ))

    def write_jsonl(self, path: Path) -> None:
        with Path(path).open("a", encoding="utf-8") as f:
            for snap in self.snapshots:
                f.write(json.dumps(snap) + "\n")
