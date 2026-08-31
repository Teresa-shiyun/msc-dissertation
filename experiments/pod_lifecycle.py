from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def sample_pods_raw(namespace: str, label_selector: str) -> list[dict]:
    cp = subprocess.run(
        ["kubectl", "-n", namespace, "get", "pods", "-l", label_selector, "-o", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if cp.returncode != 0:
        return []
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []
    out = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        container_statuses = status.get("containerStatuses") or []
        restart_count = container_statuses[0].get("restartCount") if container_statuses else None
        out.append({
            "uid": meta.get("uid"),
            "name": meta.get("name"),
            "phase": status.get("phase"),
            "restart_count": restart_count,
            "deletion_timestamp": meta.get("deletionTimestamp"),
        })
    return out


@dataclass
class PodRecord:
    uid: str
    name: str
    first_seen_ts: float
    last_seen_ts: float
    pre_existing: bool
    restart_counts: list = field(default_factory=list)
    phases: list = field(default_factory=list)
    seen_deletion_timestamp: bool = False


class LifecycleTracker:
    def __init__(self, namespace: str, label_selector: str, role: str):
        self.namespace = namespace
        self.label_selector = label_selector
        self.role = role
        self.records: dict[str, PodRecord] = {}
        self._first_observation_done = False
        self.raw_ticks: list[dict] = []

    def observe_samples(self, samples: list[dict], ts: float) -> None:
        for s in samples:
            uid = s.get("uid")
            if not uid:
                continue
            rc = s.get("restart_count")
            if uid not in self.records:
                self.records[uid] = PodRecord(
                    uid=uid,
                    name=s.get("name") or uid,
                    first_seen_ts=ts,
                    last_seen_ts=ts,
                    pre_existing=not self._first_observation_done,
                )
            rec = self.records[uid]
            rec.last_seen_ts = ts
            if s.get("name"):
                rec.name = s["name"]
            if rc is not None:
                rec.restart_counts.append((ts, rc))
            if s.get("phase"):
                rec.phases.append(s["phase"])
            if s.get("deletion_timestamp"):
                rec.seen_deletion_timestamp = True
        self._first_observation_done = True
        self.raw_ticks.append({
            "ts": ts, "role": self.role, "namespace": self.namespace,
            "label_selector": self.label_selector, "pods": samples,
        })

    def observe(self, ts: float) -> list[dict]:
        samples = sample_pods_raw(self.namespace, self.label_selector)
        self.observe_samples(samples, ts)
        return samples

    def check_validity(self) -> tuple[bool, list[dict]]:
        violations: list[dict] = []
        for uid, rec in self.records.items():
            if not rec.restart_counts:
                continue
            counts_in_order = [c for _, c in rec.restart_counts]
            first_count = counts_in_order[0]
            max_count = max(counts_in_order)
            max_ts = next(ts for ts, c in rec.restart_counts if c == max_count)
            if rec.pre_existing:
                if max_count > first_count:
                    violations.append({
                        "role": self.role, "uid": uid, "name": rec.name, "pre_existing": True,
                        "old_restart_count": first_count, "new_restart_count": max_count,
                        "first_seen_ts": rec.first_seen_ts, "violation_ts": max_ts,
                    })
            else:
                if max_count > 0:
                    violations.append({
                        "role": self.role, "uid": uid, "name": rec.name, "pre_existing": False,
                        "old_restart_count": 0, "new_restart_count": max_count,
                        "first_seen_ts": rec.first_seen_ts, "violation_ts": max_ts,
                    })
        return (len(violations) == 0), violations

    def write_jsonl(self, path: Path) -> None:
        with Path(path).open("a", encoding="utf-8") as f:
            for tick in self.raw_ticks:
                f.write(json.dumps(tick) + "\n")
