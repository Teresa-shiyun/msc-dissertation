from __future__ import annotations

import csv
import hashlib
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "analysis"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))

import _lib as lib  # noqa: E402
from k6_log_parser import parse_k6_stdout_file  # noqa: E402
from stage_a_analysis import CORE6_ARMS, HPA_ARMS, PREDICTOR_ARMS, summarise_3block  # noqa: E402
from cumulative_analysis import (  # noqa: E402
    bootstrap_ci, paired_diff, build_hpa_diagnostic, build_predictor_comparison,
    build_session_sensitivity, load_final_manifest_runs,
)
from analysis_input_loader import DEFAULT_MANIFEST_PATH, AnalysisInputError  # noqa: E402

C6_DIR = REPO_ROOT / "results/core6_v1/raw_batch2_20260710/incremental_blocks_6_10"
BLOCK_RESULTS_DIRS = {
    **{b: REPO_ROOT / "results/core6_v1/raw_batch2_20260710" for b in (1, 2, 3)},
    **{b: REPO_ROOT / "results/core6_v1/raw_batch2_20260710/incremental_blocks_4_5" for b in (4, 5)},
    **{b: C6_DIR for b in range(6, 11)},
}
OUT_DIR = REPO_ROOT / "results/final_cumulative_analysis_10block"
BOOTSTRAP_SEED = 20260710
BOOTSTRAP_N_RESAMPLES = 10000
SLO_LAT95_MS = lib.DEFAULT_SLO_LAT95_MS
MAX_REPLICAS = 6


class FinalAnalysisError(RuntimeError):
    pass


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_resolver_safe_block_table(rows: list[dict], provenance_by_run_id: dict) -> pd.DataFrame:
    runs_df = pd.DataFrame(rows)
    runs_df["t_start_epoch"] = pd.to_datetime(runs_df["t_start"], utc=True).map(lambda t: int(t.timestamp()))
    runs_df["t_end_epoch"] = pd.to_datetime(runs_df["t_end"], utc=True).map(lambda t: int(t.timestamp()))
    runs_df["repetition"] = runs_df["repetition"].astype(int)

    chunks = []
    for _, r in runs_df.iterrows():
        run_dir = BLOCK_RESULTS_DIRS[int(r["block_id"])] / r["run_id"]
        m, _prov = lib.load_run_metrics_with_provenance(run_dir)
        m = m.copy()
        m["run_id"] = r["run_id"]
        m["arm"] = r["arm"]
        m["traffic"] = r["traffic"]
        m["repetition"] = int(r["repetition"])
        m["t_rel"] = m["ts"] - r["t_start_epoch"]
        chunks.append(m)
    metrics_df = pd.concat(chunks, ignore_index=True)

    agg = lib.run_aggregates(runs_df, metrics_df, slo_lat95_ms=SLO_LAT95_MS)
    rep_series = lib.cloud_replicas_series(metrics_df)
    lat50 = lib.cloud_latency_series(metrics_df, "lat50_ms")
    lat99 = lib.cloud_latency_series(metrics_df, "lat99_ms")

    row_by_run = {r["run_id"]: r for r in rows}
    out_rows = []
    for _, a in agg.iterrows():
        run_id = a["run_id"]
        src = row_by_run[run_id]
        k6 = parse_k6_stdout_file(BLOCK_RESULTS_DIRS[int(src["block_id"])] / run_id / "k6_stdout.log")
        l50 = lat50[lat50["run_id"] == run_id]["lat_ms"]
        l99 = lat99[lat99["run_id"] == run_id]["lat_ms"]
        rs = rep_series[rep_series["run_id"] == run_id]
        time_at_max = float((rs["replicas"] == MAX_REPLICAS).mean()) if not rs.empty else None
        prov = provenance_by_run_id[run_id]
        out_rows.append({
            "run_id": run_id, "block_id": src["block_id"], "session_id": src["session_id"], "arm": a["arm"],
            "lat50_mean_ms": round(float(l50.mean()), 3) if not l50.empty else None,
            "lat95_mean_ms": round(a["lat95_mean_ms"], 3) if pd.notna(a["lat95_mean_ms"]) else None,
            "lat99_mean_ms": round(float(l99.mean()), 3) if not l99.empty else None,
            "lat95_p95_ms": round(a["lat95_p95_ms"], 3) if pd.notna(a["lat95_p95_ms"]) else None,
            "slo_violation_rate": round(a["slo_violation_rate"], 4) if pd.notna(a["slo_violation_rate"]) else None,
            "k6_http_req_failed_rate": k6.get("http_req_failed_rate"),
            "k6_http_reqs_count": k6.get("http_reqs_count"),
            "k6_checks_pass_rate": k6.get("checks_pass_rate"),
            "replica_seconds": round(a["replica_seconds"], 2) if pd.notna(a["replica_seconds"]) else None,
            "replicas_peak": a["replicas_peak"],
            "scale_changes": a["scale_changes"],
            "time_at_max_replicas_frac": round(time_at_max, 4) if time_at_max is not None else None,
            "cpu_util_mean": round(a["cpu_util_mean"], 4) if pd.notna(a["cpu_util_mean"]) else None,
            "rps_mean": round(a["rps_mean"], 2) if pd.notna(a["rps_mean"]) else None,
            "original_validity": prov["original_validity"],
            "effective_data_status": prov["effective_data_status"],
            "metrics_source": prov["metrics_source"],
            "inclusion_status": prov["inclusion_status"],
            "deviation_flag": prov["deviation_flag"],
        })
    return pd.DataFrame(out_rows)


def hpa_per_block_monotonicity(block_df: pd.DataFrame) -> dict:
    sub = block_df[block_df["arm"].isin(HPA_ARMS)]
    pivot = sub.pivot(index="block_id", columns="arm", values="lat95_mean_ms")
    per_block = {}
    non_monotonic_blocks = []
    for block_id, row in pivot.iterrows():
        vals = [row.get(a) for a in HPA_ARMS]
        if any(pd.isna(v) for v in vals):
            per_block[str(block_id)] = "incomplete"
            continue
        if vals == sorted(vals):
            per_block[str(block_id)] = "monotonic_non_decreasing"
        elif vals == sorted(vals, reverse=True):
            per_block[str(block_id)] = "monotonic_non_increasing"
        else:
            per_block[str(block_id)] = "non_monotonic"
            non_monotonic_blocks.append(str(block_id))
    return {
        "metric": "lat95_mean_ms", "order": HPA_ARMS,
        "per_block_pattern": per_block,
        "non_monotonic_block_count": len(non_monotonic_blocks),
        "non_monotonic_blocks": non_monotonic_blocks,
    }


def hpa_pairwise_diffs(block_df: pd.DataFrame) -> dict:
    sub = block_df[block_df["arm"].isin(HPA_ARMS)]
    metrics = ["lat95_mean_ms", "replica_seconds", "replicas_peak", "time_at_max_replicas_frac", "scale_changes"]
    pairs = [("H50", "H100"), ("H100", "H115"), ("H50", "H115")]
    return {f"{a}_vs_{b}": {m: paired_diff(sub, m, a, b) for m in metrics} for a, b in pairs}


def run_sensitivity_variant(block_df: pd.DataFrame, exclude_blocks: list[str], label: str) -> dict:
    sub = block_df[~block_df["block_id"].astype(str).isin(exclude_blocks)].copy()
    n_blocks = sub["block_id"].nunique()
    return {
        "label": label,
        "excluded_blocks": exclude_blocks,
        "n_blocks": int(n_blocks),
        "predictor_comparison": build_predictor_comparison(sub),
        "hpa_diagnostic": build_hpa_diagnostic(sub),
    }


ADMISSION_HISTORY = [
    {"session_id": "S5", "block": 6, "attempt": 1, "verdict": "FAIL", "category": "overload_severe_instability", "p95_ms": 16.16, "cpu_pct": 23.73},
    {"session_id": "S10", "block": 6, "attempt": 2, "verdict": "DIAGNOSTIC_COLLECTION_INCOMPLETE", "category": "diagnostic_incomplete", "p95_ms": 9.254, "cpu_pct": 23.1},
    {"session_id": "S11", "block": 6, "attempt": 3, "verdict": "PASS", "category": "pass", "p95_ms": 8.99, "cpu_pct": 29.43},
    {"session_id": "S6", "block": 7, "attempt": 1, "verdict": "FAIL", "category": "overload_severe_instability", "p95_ms": 3550.219, "cpu_pct": 70.1},
    {"session_id": "S12", "block": 7, "attempt": 2, "verdict": "FAIL", "category": "overload_severe_instability", "p95_ms": 11469.008, "cpu_pct": 58.33},
    {"session_id": "S13", "block": 7, "attempt": 3, "verdict": "PASS", "category": "pass", "p95_ms": 4.71, "cpu_pct": 16.23},
    {"session_id": "S14", "block": 8, "attempt": 1, "verdict": "FAIL", "category": "low_cpu_lower_band", "p95_ms": 4.519, "cpu_pct": 14.77},
    {"session_id": "S15", "block": 8, "attempt": 2, "verdict": "FAIL", "category": "low_cpu_lower_band", "p95_ms": 4.57, "cpu_pct": 15.03},
    {"session_id": "S16", "block": 8, "attempt": 3, "verdict": "PASS", "category": "pass", "p95_ms": 4.471, "cpu_pct": 16.6},
    {"session_id": "S17", "block": 9, "attempt": 1, "verdict": "PASS", "category": "pass", "p95_ms": 4.959, "cpu_pct": 18.6},
    {"session_id": "S18", "block": 10, "attempt": 1, "verdict": "FAIL", "category": "low_cpu_lower_band", "p95_ms": 5.016, "cpu_pct": 8.9},
    {"session_id": "S19", "block": 10, "attempt": 2, "verdict": "FAIL", "category": "low_cpu_lower_band", "p95_ms": 4.341, "cpu_pct": 14.5},
    {"session_id": "S20", "block": 10, "attempt": 3, "verdict": "PASS", "category": "pass", "p95_ms": 4.591, "cpu_pct": 17.77},
]


def admission_execution_summary() -> dict:
    from collections import Counter
    cat_counts = Counter(r["category"] for r in ADMISSION_HISTORY)
    return {
        "note": "Execution/method-limitation summary only -- never mixed with arm performance data.",
        "total_sessions": len(ADMISSION_HISTORY),
        "category_counts": dict(cat_counts),
        "FAIL_total": sum(1 for r in ADMISSION_HISTORY if r["verdict"] == "FAIL"),
        "INVALID_OR_DIAGNOSTIC_INCOMPLETE_total": sum(1 for r in ADMISSION_HISTORY if r["verdict"] == "DIAGNOSTIC_COLLECTION_INCOMPLETE"),
        "PASS_total": sum(1 for r in ADMISSION_HISTORY if r["verdict"] == "PASS"),
        "rows": ADMISSION_HISTORY,
    }


def run_order_session_diagnostics(block_df: pd.DataFrame, run_order_by_run_id: dict) -> dict:
    df = block_df.copy()
    df["run_order"] = df["run_id"].map(run_order_by_run_id)
    df = df.dropna(subset=["run_order", "lat95_mean_ms"])
    run_order_corr = None
    if len(df) > 2:
        run_order_corr = round(float(df["run_order"].astype(float).corr(df["lat95_mean_ms"].astype(float))), 4)

    session_diag = build_session_sensitivity(block_df)

    block6_7_influence = {}
    for b in ("6", "7"):
        block_vals = block_df[block_df["block_id"].astype(str) == b]["lat95_mean_ms"].dropna()
        other_vals = block_df[block_df["block_id"].astype(str) != b]["lat95_mean_ms"].dropna()
        block6_7_influence[f"block_{b}"] = {
            "mean_lat95_this_block": round(float(block_vals.mean()), 3) if not block_vals.empty else None,
            "mean_lat95_other_blocks": round(float(other_vals.mean()), 3) if not other_vals.empty else None,
        }

    return {
        "status": "EXPLORATORY_DIAGNOSTIC",
        "note": "Descriptive only -- not used to re-select, exclude, or re-weight the primary dataset. "
                "A trend found here is reported, never acted on.",
        "lat95_mean_ms_vs_run_order_pearson_r": run_order_corr,
        "session_sensitivity": session_diag,
        "block6_block7_lat95_vs_rest": block6_7_influence,
    }


def main() -> int:
    started_at = datetime.now(timezone.utc)
    print(f"=== FINAL 10-block cumulative analysis starting {started_at.isoformat()} ===", file=sys.stderr)

    try:
        rows, provenance_by_run_id = load_final_manifest_runs()
    except AnalysisInputError as e:
        print(f"ANALYSIS_EXECUTION_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if len(rows) != 60:
        print(f"ANALYSIS_EXECUTION_FAILED: expected 60 loaded runs, got {len(rows)}", file=sys.stderr)
        return 1

    run_order_by_run_id = {}
    manifest_run_order = {r["run_id"]: int(r["run_order"]) for r in
                           csv.DictReader(DEFAULT_MANIFEST_PATH.open(encoding="utf-8"))}
    run_order_by_run_id.update(manifest_run_order)

    for r in rows:
        meta = json.loads((BLOCK_RESULTS_DIRS[int(r["block_id"])] / r["run_id"] / "meta.json").read_text(encoding="utf-8"))
        r["t_start"] = meta["t_start"]
        r["t_end"] = meta["t_end"]
        r["traffic"] = meta.get("traffic", r.get("traffic", "spike"))

    try:
        block_df = build_resolver_safe_block_table(rows, provenance_by_run_id)
    except lib.MetricsLoadError as e:
        print(f"ANALYSIS_EXECUTION_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    problems = []
    if len(block_df) != 60:
        problems.append(f"run-level table has {len(block_df)} rows, expected 60")
    arm_counts = block_df["arm"].value_counts().to_dict()
    if not all(arm_counts.get(a, 0) == 10 for a in CORE6_ARMS):
        problems.append(f"not every arm has 10 runs: {arm_counts}")
    block_counts = block_df["block_id"].astype(str).value_counts().to_dict()
    if not all(block_counts.get(str(b), 0) == 6 for b in range(1, 11)):
        problems.append(f"not every block has 6 runs: {block_counts}")
    g30_row = block_df[(block_df["block_id"].astype(str) == "7") & (block_df["arm"] == "G30")]
    if g30_row.empty or g30_row.iloc[0]["metrics_source"] != "recovered":
        problems.append("block7 G30 did not resolve to recovered metrics_source")
    others = block_df[~((block_df["block_id"].astype(str) == "7") & (block_df["arm"] == "G30"))]
    if not (others["metrics_source"] == "original").all():
        problems.append("a non-block7-G30 run resolved to a non-original metrics_source")
    if block_df["run_id"].duplicated().any():
        problems.append("duplicate run_id in block-level table")
    if block_df[["lat95_mean_ms", "replica_seconds"]].isna().any().any():
        problems.append("empty/NaN core metric values present")

    if problems:
        print("ANALYSIS_EXECUTION_FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "ANALYSIS_EXECUTION_FAILED_ISSUES.json").write_text(json.dumps(problems, indent=2), encoding="utf-8")
        return 1

    print(f"Integrity gate passed: 60/60 runs, 10/10 blocks, 6/6 arms=10, block7 G30 recovered.", file=sys.stderr)

    predictor_primary = build_predictor_comparison(block_df)
    hpa_primary = build_hpa_diagnostic(block_df)
    hpa_primary["per_block_monotonicity"] = hpa_per_block_monotonicity(block_df)
    hpa_primary["pairwise_diffs"] = hpa_pairwise_diffs(block_df)

    arm_summary_metrics = ["lat95_mean_ms", "lat95_p95_ms", "slo_violation_rate", "replica_seconds",
                            "replicas_peak", "time_at_max_replicas_frac", "scale_changes", "cpu_util_mean", "rps_mean"]
    arm_summary = {m: summarise_3block(block_df, m) for m in arm_summary_metrics}

    sensitivity = {
        "exclude_block6": run_sensitivity_variant(block_df, ["6"], "EXCLUDE_BLOCK6_unauthorised_attempt3_continuation"),
        "exclude_block7": run_sensitivity_variant(block_df, ["7"], "EXCLUDE_BLOCK7_G30_metrics_historical_reexport"),
        "exclude_block6_and_block7": run_sensitivity_variant(block_df, ["6", "7"], "EXCLUDE_BOTH_additional_robustness"),
    }

    admission_summary = admission_execution_summary()
    ro_diag = run_order_session_diagnostics(block_df, run_order_by_run_id)

    n_primary_pairs = block_df.pivot(index="block_id", columns="arm", values="lat95_mean_ms").dropna().shape[0]
    n_ex6_pairs = block_df[block_df["block_id"].astype(str) != "6"].pivot(index="block_id", columns="arm", values="lat95_mean_ms").dropna().shape[0]
    n_ex7_pairs = block_df[block_df["block_id"].astype(str) != "7"].pivot(index="block_id", columns="arm", values="lat95_mean_ms").dropna().shape[0]
    n_exboth_pairs = block_df[~block_df["block_id"].astype(str).isin(["6", "7"])].pivot(index="block_id", columns="arm", values="lat95_mean_ms").dropna().shape[0]

    integrity = {
        "input_runs": len(rows), "output_run_level_rows": len(block_df),
        "runs_per_arm": arm_counts, "runs_per_block": block_counts,
        "primary_paired_blocks": int(n_primary_pairs), "exclude_block6_paired_blocks": int(n_ex6_pairs),
        "exclude_block7_paired_blocks": int(n_ex7_pairs), "exclude_both_paired_blocks": int(n_exboth_pairs),
        "block7_g30_metrics_source": g30_row.iloc[0]["metrics_source"],
        "other_59_metrics_source_all_original": bool((others["metrics_source"] == "original").all()),
        "no_empty_data": True, "no_duplicates": True, "no_silent_skip": True,
    }
    expected = {"primary_paired_blocks": 10, "exclude_block6_paired_blocks": 9,
                "exclude_block7_paired_blocks": 9, "exclude_both_paired_blocks": 8}
    integrity_ok = all(integrity[k] == v for k, v in expected.items()) and integrity["input_runs"] == 60 and integrity["output_run_level_rows"] == 60

    verdict = "FINAL_CUMULATIVE_ANALYSIS_COMPLETE" if integrity_ok else "FINAL_CUMULATIVE_ANALYSIS_FAILED"

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    block_df.to_csv(OUT_DIR / "run_level_results.csv", index=False)
    block_level = block_df.groupby(["block_id", "arm"], as_index=False).first()
    block_level.to_csv(OUT_DIR / "block_level_results.csv", index=False)

    arm_summary_rows = []
    for metric, by_arm in arm_summary.items():
        for arm, stats in by_arm.items():
            arm_summary_rows.append({"metric": metric, "arm": arm, **{k: v for k, v in stats.items() if k != "raw_by_block"}})
    pd.DataFrame(arm_summary_rows).to_csv(OUT_DIR / "arm_summary.csv", index=False)

    contrast_rows = []
    for metric, comparisons in predictor_primary["block_paired_differences"].items():
        for comp_name, d in comparisons.items():
            contrast_rows.append({"comparison": comp_name, "metric": metric, "scope": "primary_10block", **{k: v for k, v in d.items() if k != "pairs_by_block"}})
    for label, variant in sensitivity.items():
        for metric, comparisons in variant["predictor_comparison"]["block_paired_differences"].items():
            for comp_name, d in comparisons.items():
                contrast_rows.append({"comparison": comp_name, "metric": metric, "scope": variant["label"], **{k: v for k, v in d.items() if k != "pairs_by_block"}})
    pd.DataFrame(contrast_rows).to_csv(OUT_DIR / "paired_contrasts.csv", index=False)

    hpa_rows = []
    for pair_name, metrics in hpa_primary["pairwise_diffs"].items():
        for metric, d in metrics.items():
            hpa_rows.append({"pair": pair_name, "metric": metric, "scope": "primary_10block", **{k: v for k, v in d.items() if k != "pairs_by_block"}})
    pd.DataFrame(hpa_rows).to_csv(OUT_DIR / "hpa_target_diagnostic.csv", index=False)

    sens_rows = []
    for label, variant in sensitivity.items():
        for metric, comparisons in variant["predictor_comparison"]["block_paired_differences"].items():
            for comp_name, d in comparisons.items():
                primary_d = predictor_primary["block_paired_differences"].get(metric, {}).get(comp_name, {})
                sens_rows.append({
                    "sensitivity_scope": variant["label"], "n_blocks": variant["n_blocks"],
                    "comparison": comp_name, "metric": metric,
                    "sensitivity_mean_diff": d.get("mean_diff"), "primary_mean_diff": primary_d.get("mean_diff"),
                    "sensitivity_ci_low": d.get("bootstrap_ci", {}).get("ci_low"),
                    "sensitivity_ci_high": d.get("bootstrap_ci", {}).get("ci_high"),
                    "primary_ci_low": primary_d.get("bootstrap_ci", {}).get("ci_low"),
                    "primary_ci_high": primary_d.get("bootstrap_ci", {}).get("ci_high"),
                    "same_direction_as_primary": (
                        (d.get("mean_diff") is not None and primary_d.get("mean_diff") is not None)
                        and ((d["mean_diff"] > 0) == (primary_d["mean_diff"] > 0))
                    ),
                    "sensitivity_effect_size": d.get("paired_effect_size"), "primary_effect_size": primary_d.get("paired_effect_size"),
                })
    pd.DataFrame(sens_rows).to_csv(OUT_DIR / "sensitivity_summary.csv", index=False)

    pd.DataFrame(admission_summary["rows"]).to_csv(OUT_DIR / "admission_summary.csv", index=False)

    ro_rows = []
    for session_id, d in ro_diag["session_sensitivity"]["by_session"].items():
        ro_rows.append({"session_id": session_id, **{k: v for k, v in d.items() if k != "lat95_mean_ms_by_arm"}})
    pd.DataFrame(ro_rows).to_csv(OUT_DIR / "run_order_diagnostics.csv", index=False)

    ended_at = datetime.now(timezone.utc)

    results_json = {
        "generated_at": ended_at.isoformat(),
        "final_verdict": verdict,
        "integrity": integrity,
        "primary_analysis": {
            "n_blocks": 10,
            "predictor_comparison_G30_P30_F30": predictor_primary,
            "hpa_target_diagnostic_H50_H100_H115": hpa_primary,
            "arm_summary": arm_summary,
        },
        "sensitivity_analysis": sensitivity,
        "admission_execution_summary": admission_summary,
        "run_order_session_diagnostics": ro_diag,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_n_resamples": BOOTSTRAP_N_RESAMPLES,
        "slo_lat95_ms_threshold": SLO_LAT95_MS,
        "max_replicas": MAX_REPLICAS,
    }
    (OUT_DIR / "analysis_results.json").write_text(json.dumps(results_json, indent=2, default=str), encoding="utf-8")

    import numpy
    code_files = [
        REPO_ROOT / "experiments/final_cumulative_analysis.py",
        REPO_ROOT / "experiments/cumulative_analysis.py",
        REPO_ROOT / "experiments/analysis_input_loader.py",
        REPO_ROOT / "analysis/_lib.py",
        REPO_ROOT / "experiments/metrics_source_resolver.py",
        REPO_ROOT / "experiments/stage_a_analysis.py",
        REPO_ROOT / "experiments/k6_log_parser.py",
    ]
    code_hashes = {str(p.relative_to(REPO_ROOT)).replace("\\", "/"): sha256_of(p) for p in code_files}
    manifest_hash = sha256_of(C6_DIR / "accepted_runs.csv")
    input_manifest_hash = sha256_of(C6_DIR / "analysis_inputs.csv")

    output_files = sorted(OUT_DIR.glob("*"))
    output_hashes = {f.name: sha256_of(f) for f in output_files if f.is_file()}

    provenance_md = f"""# Final Analysis Execution Provenance

Ended {ended_at.isoformat()}, started {started_at.isoformat()}.

## Command
`python experiments/final_cumulative_analysis.py`

## Environment
- Python: {platform.python_version()} ({platform.platform()})
- pandas: {pd.__version__}
- numpy: {numpy.__version__}

## Parameters
- bootstrap_seed = {BOOTSTRAP_SEED}
- bootstrap_n_resamples = {BOOTSTRAP_N_RESAMPLES}
- SLO lat95 threshold = {SLO_LAT95_MS} ms
- max_replicas = {MAX_REPLICAS}

## Input manifests (whitelist, not modified)
- accepted_runs.csv sha256 = {manifest_hash}
- analysis_inputs.csv sha256 = {input_manifest_hash}

## Analysis code hashes (as run this round)
{chr(10).join(f"- {k} = {v}" for k, v in code_hashes.items())}

## Output file hashes
{chr(10).join(f"- {k} = {v}" for k, v in sorted(output_hashes.items()))}

## What was NOT modified
- execution_snapshot.json, execution_sha256.csv (prior audit freeze)
- runs.csv (all 3 stage directories), any original metrics.csv, metrics_recovered.csv,
  adjudication_manifest.json
- schedule, policy, traffic, admission band/reference files
- Final verdict: **{verdict}**
"""
    (OUT_DIR / "analysis_provenance.md").write_text(provenance_md, encoding="utf-8")

    print(f"=== done, verdict={verdict} ===", file=sys.stderr)
    print(json.dumps({"verdict": verdict, "integrity": integrity}, indent=2))
    return 0 if verdict == "FINAL_CUMULATIVE_ANALYSIS_COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
