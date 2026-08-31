from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REQUIRED_METRICS = (
    "replicas_status", "replicas_spec", "cpu_cores", "cpu_utilisation",
    "mem_mib", "rps", "lat50_ms", "lat95_ms", "lat99_ms", "inflight",
)

REQUIRED_ADJUDICATION_STATUS = "RECOVERY_COMPLETE_AND_QC_PASS"
REQUIRED_EFFECTIVE_DATA_STATUS = "VALID_AFTER_HISTORICAL_METRICS_REEXPORT"

WINDOW_COVERAGE_TOLERANCE_SECONDS = 20.0


def _parse_iso(s: str) -> float:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()


@dataclass
class MetricsSourceResult:
    run_id: str
    metrics_path: Path
    metrics_source: str            # "original" | "recovered"
    original_validity: str
    effective_data_status: str
    eligible_for_primary_analysis: bool
    reason: str = ""
    manifest_rejected_reason: Optional[str] = None
    warnings: list = field(default_factory=list)

    def audit_row(self) -> dict:
        return {
            "run_id": self.run_id,
            "original_validity": self.original_validity,
            "effective_data_status": self.effective_data_status,
            "metrics_source": self.metrics_source,
            "eligible_for_primary_analysis": self.eligible_for_primary_analysis,
        }


def _read_required_metrics_and_coverage(csv_path: Path) -> tuple[set, Optional[float], Optional[float]]:
    if not csv_path.exists():
        return set(), None, None
    metrics_present: set = set()
    ts_values: list = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            metric = row.get("metric")
            if metric:
                metrics_present.add(metric)
            try:
                ts_values.append(float(row["ts"]))
            except (KeyError, ValueError, TypeError):
                continue
    if not ts_values:
        return metrics_present, None, None
    return metrics_present, min(ts_values), max(ts_values)


def resolve_metrics_source(run_dir: Path) -> MetricsSourceResult:
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    run_id = meta["run_id"]
    original_validity = meta.get("validity", "unknown")
    original_metrics_path = run_dir / "metrics.csv"

    default = MetricsSourceResult(
        run_id=run_id,
        metrics_path=original_metrics_path,
        metrics_source="original",
        original_validity=original_validity,
        effective_data_status=original_validity,
        eligible_for_primary_analysis=(original_validity == "valid"),
        reason="no adjudication manifest present; using original metrics.csv and recorded validity",
    )

    manifest_path = run_dir / "adjudication_manifest.json"
    if not manifest_path.exists():
        return default

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        default.manifest_rejected_reason = f"manifest_unreadable:{e.__class__.__name__}"
        default.warnings.append(default.manifest_rejected_reason)
        return default

    if manifest.get("run_id") != run_id:
        default.manifest_rejected_reason = (
            f"manifest_run_id_mismatch(manifest={manifest.get('run_id')!r}, run_dir={run_id!r})"
        )
        default.warnings.append(default.manifest_rejected_reason)
        return default

    if manifest.get("adjudication_status") != REQUIRED_ADJUDICATION_STATUS:
        default.manifest_rejected_reason = (
            f"adjudication_status_not_pass(actual={manifest.get('adjudication_status')!r})"
        )
        default.warnings.append(default.manifest_rejected_reason)
        return default
    if manifest.get("effective_data_status") != REQUIRED_EFFECTIVE_DATA_STATUS:
        default.manifest_rejected_reason = (
            f"effective_data_status_not_valid(actual={manifest.get('effective_data_status')!r})"
        )
        default.warnings.append(default.manifest_rejected_reason)
        return default

    manifest_window = manifest.get("window", {})
    if manifest_window.get("t_start") != meta.get("t_start") or manifest_window.get("t_end") != meta.get("t_end"):
        default.manifest_rejected_reason = (
            f"manifest_window_mismatch(manifest={manifest_window}, "
            f"meta_t_start={meta.get('t_start')!r}, meta_t_end={meta.get('t_end')!r})"
        )
        default.warnings.append(default.manifest_rejected_reason)
        return default

    recovered_filename = manifest.get("effective_metrics_file", "")
    recovered_path = run_dir / recovered_filename
    if not recovered_filename or recovered_path.resolve().parent != run_dir.resolve():
        default.manifest_rejected_reason = f"effective_metrics_file_not_in_run_dir({recovered_filename!r})"
        default.warnings.append(default.manifest_rejected_reason)
        return default
    if recovered_path == original_metrics_path:
        default.manifest_rejected_reason = "effective_metrics_file_would_overwrite_original"
        default.warnings.append(default.manifest_rejected_reason)
        return default

    metrics_present, min_ts, max_ts = _read_required_metrics_and_coverage(recovered_path)
    missing = sorted(set(REQUIRED_METRICS) - metrics_present)
    if missing:
        default.manifest_rejected_reason = f"recovered_csv_missing_metrics({','.join(missing)})"
        default.warnings.append(default.manifest_rejected_reason)
        return default

    if min_ts is None or max_ts is None:
        default.manifest_rejected_reason = "recovered_csv_empty"
        default.warnings.append(default.manifest_rejected_reason)
        return default

    t_start_epoch = _parse_iso(meta["t_start"])
    t_end_epoch = _parse_iso(meta["t_end"])
    start_gap = abs(min_ts - t_start_epoch)
    end_gap = abs(max_ts - t_end_epoch)
    if start_gap > WINDOW_COVERAGE_TOLERANCE_SECONDS or end_gap > WINDOW_COVERAGE_TOLERANCE_SECONDS:
        default.manifest_rejected_reason = (
            f"recovered_csv_window_coverage_mismatch(start_gap={start_gap:.1f}s, end_gap={end_gap:.1f}s, "
            f"tolerance={WINDOW_COVERAGE_TOLERANCE_SECONDS}s)"
        )
        default.warnings.append(default.manifest_rejected_reason)
        return default

    return MetricsSourceResult(
        run_id=run_id,
        metrics_path=recovered_path,
        metrics_source="recovered",
        original_validity=original_validity,
        effective_data_status=manifest["effective_data_status"],
        eligible_for_primary_analysis=bool(manifest.get("eligible_for_primary_analysis", False)),
        reason="adjudication manifest verified: identity+window bound, exact-status match, "
               "recovered CSV independently re-checked for metric completeness and window coverage",
    )
