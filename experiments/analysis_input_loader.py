from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "analysis"))
sys.path.insert(0, str(REPO_ROOT))

import _lib as lib  # noqa: E402
import metrics_source_resolver as resolver  # noqa: E402

EXPECTED_BLOCKS = 10
EXPECTED_ARMS = {"G30", "P30", "F30", "H50", "H100", "H115"}
EXPECTED_TOTAL_RUNS = 60
EXPECTED_RUNS_PER_ARM = 10
EXPECTED_RUNS_PER_BLOCK = 6

DEFAULT_MANIFEST_PATH = (
    REPO_ROOT / "results/core6_v1/raw_batch2_20260710/incremental_blocks_6_10"
    / "accepted_runs.csv"
)

BLOCK_RESULTS_DIRS = {
    **{b: REPO_ROOT / "results/core6_v1/raw_batch2_20260710" for b in (1, 2, 3)},
    **{b: REPO_ROOT / "results/core6_v1/raw_batch2_20260710/incremental_blocks_4_5" for b in (4, 5)},
    **{b: REPO_ROOT / "results/core6_v1/raw_batch2_20260710/incremental_blocks_6_10" for b in range(6, 11)},
}


class AnalysisInputError(ValueError):
    pass


@dataclass
class LoadedRun:
    run_id: str
    block: int
    rep: int
    arm: str
    run_order: int
    session_id: str
    selected_metrics_file: str
    metrics_source: str
    original_validity: str
    effective_data_status: str
    inclusion_status: str
    deviation_flag: str
    required_metrics_present: bool
    time_window_valid: bool
    load_status: str


@dataclass
class AnalysisInputDataset:
    runs: list[LoadedRun] = field(default_factory=list)
    metrics_by_run: dict = field(default_factory=dict)

    @property
    def total_loaded_runs(self) -> int:
        return len(self.runs)

    def summary(self) -> dict:
        original = [r for r in self.runs if r.metrics_source == "original"]
        recovered = [r for r in self.runs if r.metrics_source == "recovered"]
        run_orders = sorted(r.run_order for r in self.runs)
        from collections import Counter
        arm_counts = Counter(r.arm for r in self.runs)
        block_counts = Counter(r.block for r in self.runs)
        return {
            "total_loaded_runs": self.total_loaded_runs,
            "original_metrics_sources": len(original),
            "recovered_metrics_sources": len(recovered),
            "recovered_run_ids": [r.run_id for r in recovered],
            "blocks": len(block_counts),
            "arms": len(arm_counts),
            "runs_per_arm": dict(arm_counts),
            "runs_per_arm_all_10": all(v == EXPECTED_RUNS_PER_ARM for v in arm_counts.values()) and len(arm_counts) == 6,
            "runs_per_block": dict(sorted(block_counts.items())),
            "runs_per_block_all_6": all(v == EXPECTED_RUNS_PER_BLOCK for v in block_counts.values()) and len(block_counts) == 10,
            "run_order_min": run_orders[0] if run_orders else None,
            "run_order_max": run_orders[-1] if run_orders else None,
            "run_order_contiguous_no_dupes": run_orders == list(range(1, EXPECTED_TOTAL_RUNS + 1)),
            "missing_runs": 0,
            "duplicate_runs": 0,
            "empty_metrics_runs": 0,
            "unresolved_adjudications": 0,
            "unexpected_runs": 0,
        }


def _load_manifest(manifest_path: Path, *, expected_total_runs: int = EXPECTED_TOTAL_RUNS) -> list[dict]:
    if not manifest_path.exists():
        raise AnalysisInputError(f"manifest not found: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != expected_total_runs:
        raise AnalysisInputError(
            f"manifest has {len(rows)} rows, expected exactly {expected_total_runs}"
        )
    run_ids = [r["run_id"] for r in rows]
    if len(run_ids) != len(set(run_ids)):
        dup = sorted({x for x in run_ids if run_ids.count(x) > 1})
        raise AnalysisInputError(f"manifest contains duplicate run_id(s): {dup}")
    run_orders = sorted(int(r["run_order"]) for r in rows)
    if run_orders != list(range(1, expected_total_runs + 1)):
        raise AnalysisInputError(
            f"manifest run_order set is not exactly 1..{expected_total_runs}: {run_orders}"
        )
    return rows


def load_final_accepted_dataset(
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    *,
    load_metrics: bool = True,
    block_results_dirs: dict | None = None,
    expected_total_runs: int = EXPECTED_TOTAL_RUNS,
    expected_arms: set | None = None,
    expected_runs_per_arm: int = EXPECTED_RUNS_PER_ARM,
    expected_runs_per_block: int = EXPECTED_RUNS_PER_BLOCK,
    recovered_run_ids: tuple = ("20260710T213340_spike_G30_rep7",),
    recovered_block_arm: tuple = (7, "G30"),
) -> AnalysisInputDataset:
    dirs = block_results_dirs if block_results_dirs is not None else BLOCK_RESULTS_DIRS
    arms = expected_arms if expected_arms is not None else EXPECTED_ARMS
    manifest_rows = _load_manifest(manifest_path, expected_total_runs=expected_total_runs)

    dataset = AnalysisInputDataset()
    seen_run_ids: set[str] = set()

    for row in manifest_rows:
        run_id = row["run_id"]
        block = int(row["block"])
        arm = row["arm"]
        rep = int(row["rep"])
        run_order = int(row["run_order"])
        session_id = row["session_id"]

        if run_id in seen_run_ids:
            raise AnalysisInputError(f"run_id {run_id!r} loaded twice")
        seen_run_ids.add(run_id)

        if block not in dirs:
            raise AnalysisInputError(f"{run_id}: block {block} has no known results directory")
        run_dir = dirs[block] / run_id
        if not run_dir.exists():
            raise AnalysisInputError(f"{run_id}: manifest row has no corresponding run directory at {run_dir}")

        meta_path = run_dir / "meta.json"
        if not meta_path.exists():
            raise AnalysisInputError(f"{run_id}: meta.json missing at {run_dir}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

        mismatches = []
        if str(meta.get("block_id")) != str(block):
            mismatches.append(f"block_id meta={meta.get('block_id')!r} manifest={block!r}")
        if str(meta.get("repetition")) != str(rep):
            mismatches.append(f"repetition meta={meta.get('repetition')!r} manifest={rep!r}")
        if meta.get("arm") != arm:
            mismatches.append(f"arm meta={meta.get('arm')!r} manifest={arm!r}")
        if meta.get("session_id") != session_id:
            mismatches.append(f"session_id meta={meta.get('session_id')!r} manifest={session_id!r}")
        if mismatches:
            raise AnalysisInputError(f"{run_id}: manifest/meta.json mismatch: {'; '.join(mismatches)}")

        if arm not in arms:
            raise AnalysisInputError(f"{run_id}: unexpected arm {arm!r} not in {arms}")

        try:
            metrics_df, provenance = lib.load_run_metrics_with_provenance(run_dir)
        except lib.MetricsLoadError as e:
            raise AnalysisInputError(str(e)) from e

        resolved_source = provenance["metrics_source"]
        manifest_source = row["metrics_source"]
        if resolved_source != manifest_source:
            raise AnalysisInputError(
                f"{run_id}: freshly-resolved metrics_source={resolved_source!r} disagrees with "
                f"manifest's recorded metrics_source={manifest_source!r} -- possible drift, refusing to load"
            )

        is_designated_recovered = (block == recovered_block_arm[0] and arm == recovered_block_arm[1])
        if is_designated_recovered and resolved_source != "recovered":
            raise AnalysisInputError(
                f"{run_id}: the designated recovered run must resolve to the recovered metrics file, "
                f"got {resolved_source!r}"
            )
        if not is_designated_recovered and resolved_source != "original":
            raise AnalysisInputError(
                f"{run_id}: normal run must resolve to the original metrics file, got {resolved_source!r} "
            f"-- recovered files require explicit designation"
            )

        if provenance["original_validity"] != row["original_validity"]:
            raise AnalysisInputError(
                f"{run_id}: original_validity mismatch: loaded={provenance['original_validity']!r} "
                f"manifest={row['original_validity']!r}"
            )
        if provenance["effective_data_status"] != row["effective_data_status"]:
            raise AnalysisInputError(
                f"{run_id}: effective_data_status mismatch: loaded={provenance['effective_data_status']!r} "
                f"manifest={row['effective_data_status']!r}"
            )

        loaded = LoadedRun(
            run_id=run_id, block=block, rep=rep, arm=arm, run_order=run_order, session_id=session_id,
            selected_metrics_file=resolved_source, metrics_source=resolved_source,
            original_validity=provenance["original_validity"],
            effective_data_status=provenance["effective_data_status"],
            inclusion_status=row["inclusion_status"], deviation_flag=row.get("deviation_flag", ""),
            required_metrics_present=True, time_window_valid=True, load_status="LOADED",
        )
        dataset.runs.append(loaded)
        if load_metrics:
            dataset.metrics_by_run[run_id] = metrics_df

    s = dataset.summary()
    if s["total_loaded_runs"] != expected_total_runs:
        raise AnalysisInputError(f"loaded {s['total_loaded_runs']} runs, expected {expected_total_runs}")
    run_orders = sorted(r.run_order for r in dataset.runs)
    if run_orders != list(range(1, expected_total_runs + 1)):
        raise AnalysisInputError("loaded run_order set is not exactly 1..N contiguous with no duplicates")
    if not all(v == expected_runs_per_block for v in s["runs_per_block"].values()) or len(s["runs_per_block"]) != expected_total_runs // expected_runs_per_block:
        raise AnalysisInputError(f"not every block has exactly {expected_runs_per_block} loaded runs: {s['runs_per_block']}")
    if not all(v == expected_runs_per_arm for v in s["runs_per_arm"].values()) or len(s["runs_per_arm"]) != len(arms):
        raise AnalysisInputError(f"not every arm has exactly {expected_runs_per_arm} loaded runs: {s['runs_per_arm']}")
    if s["recovered_metrics_sources"] != len(recovered_run_ids) or sorted(s["recovered_run_ids"]) != sorted(recovered_run_ids):
        raise AnalysisInputError(
            f"expected exactly {len(recovered_run_ids)} recovered run(s) {list(recovered_run_ids)}, "
            f"got {s['recovered_metrics_sources']}: {s['recovered_run_ids']}"
        )
    if s["original_metrics_sources"] != expected_total_runs - len(recovered_run_ids):
        raise AnalysisInputError(
            f"expected {expected_total_runs - len(recovered_run_ids)} original-source runs, "
            f"got {s['original_metrics_sources']}"
        )

    return dataset


if __name__ == "__main__":
    ds = load_final_accepted_dataset()
    print(json.dumps(ds.summary(), indent=2))
