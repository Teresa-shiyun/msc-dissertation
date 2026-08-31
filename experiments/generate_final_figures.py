from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "results/final_cumulative_analysis_10block"
FIG_DIR = OUT_DIR / "figures"

CORE6_ORDER = ["H50", "H100", "H115", "G30", "P30", "F30"]
CORE6_COLORS = {
    "H50": "#79b9e0", "H100": "#1f77b4", "H115": "#08306b",
    "G30": "#2ca02c", "P30": "#9467bd", "F30": "#ff7f0e",
}
HPA_ARMS = ["H50", "H100", "H115"]
PREDICTOR_ARMS = ["G30", "P30", "F30"]


def _save(fig, name: str) -> None:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG_DIR / f"{name}.png", dpi=150, bbox_inches="tight")
    fig.savefig(FIG_DIR / f"{name}.pdf", bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {FIG_DIR / (name + '.png')}")


def _annotate_n(ax, n_blocks: int, paired: bool = True) -> None:
    label = f"n = {n_blocks} blocks" + (" (paired)" if paired else "")
    ax.text(0.99, 0.02, label, transform=ax.transAxes, ha="right", va="bottom",
            fontsize=8, color="#555555")


def fig_latency_comparison(df: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(7, 4.2))
    data = [df[df["arm"] == a]["lat95_mean_ms"].dropna().values for a in CORE6_ORDER]
    bp = ax.boxplot(data, labels=CORE6_ORDER, patch_artist=True, showmeans=True)
    for patch, arm in zip(bp["boxes"], CORE6_ORDER):
        patch.set_facecolor(CORE6_COLORS[arm])
        patch.set_alpha(0.6)
    ax.set_ylabel("lat95_mean_ms (block-level)")
    ax.set_ylim(bottom=0)
    ax.set_title("Latency by arm — primary 10-block dataset (block6/block7 included)")
    _annotate_n(ax, 10)
    _save(fig, "latency_comparison")


def fig_slo_violation(df: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(7, 4.2))
    means = [df[df["arm"] == a]["slo_violation_rate"].mean() for a in CORE6_ORDER]
    bars = ax.bar(CORE6_ORDER, means, color=[CORE6_COLORS[a] for a in CORE6_ORDER], alpha=0.8)
    ax.set_ylabel("mean SLO violation rate (SLO = 200ms lat95)")
    ax.set_ylim(bottom=0)
    ax.set_title("SLO violation rate by arm — primary 10-block dataset")
    _annotate_n(ax, 10)
    _save(fig, "slo_violation")


def fig_replica_seconds(df: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(7, 4.2))
    data = [df[df["arm"] == a]["replica_seconds"].dropna().values for a in CORE6_ORDER]
    bp = ax.boxplot(data, labels=CORE6_ORDER, patch_artist=True, showmeans=True)
    for patch, arm in zip(bp["boxes"], CORE6_ORDER):
        patch.set_facecolor(CORE6_COLORS[arm])
        patch.set_alpha(0.6)
    ax.set_ylabel("replica_seconds (block-level)")
    ax.set_ylim(bottom=0)
    ax.set_title("Resource use (replica-seconds) by arm — primary 10-block dataset")
    _annotate_n(ax, 10)
    _save(fig, "replica_seconds")


def fig_tradeoff(df: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(6.5, 5.5))
    for arm in CORE6_ORDER:
        sub = df[df["arm"] == arm]
        ax.scatter(sub["replica_seconds"], sub["lat95_mean_ms"], color=CORE6_COLORS[arm],
                   label=arm, s=45, alpha=0.85, edgecolors="white", linewidths=0.5)
    ax.set_xlabel("replica_seconds (resource-usage proxy)")
    ax.set_ylabel("lat95_mean_ms (latency)")
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    ax.set_title("Latency–resource trade-off, one point per (block, arm) — primary dataset")
    ax.legend(fontsize=8, loc="upper right")
    _annotate_n(ax, 10, paired=False)
    _save(fig, "latency_resource_tradeoff")


def _paired_block_figure(df: pd.DataFrame, arm_a: str, arm_b: str, name: str) -> None:
    pivot = df[df["arm"].isin([arm_a, arm_b])].pivot(index="block_id", columns="arm", values="lat95_mean_ms")
    pivot = pivot.dropna()
    blocks = sorted(pivot.index, key=lambda x: int(x))
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    x = range(len(blocks))
    ax.plot(x, [pivot.loc[b, arm_a] for b in blocks], "o-", color=CORE6_COLORS[arm_a], label=arm_a)
    ax.plot(x, [pivot.loc[b, arm_b] for b in blocks], "s-", color=CORE6_COLORS[arm_b], label=arm_b)
    for xi, b in zip(x, blocks):
        marker_color = "#d62728" if b in ("6", "7") else "#888888"
        ax.axvline(xi, color=marker_color, alpha=0.08, linewidth=8)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"block{b}" for b in blocks])
    ax.set_ylabel("lat95_mean_ms")
    ax.set_ylim(bottom=0)
    ax.set_title(f"Paired block comparison: {arm_a} vs {arm_b} (block6/block7 highlighted, not excluded)")
    ax.legend(fontsize=9)
    _annotate_n(ax, len(blocks))
    _save(fig, name)


def fig_hpa_target_diagnostic(df: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    pivot = df[df["arm"].isin(HPA_ARMS)].pivot(index="block_id", columns="arm", values="lat95_mean_ms")
    blocks = sorted(pivot.index, key=lambda x: int(x))
    x = range(len(blocks))
    for arm in HPA_ARMS:
        ax.plot(x, [pivot.loc[b, arm] for b in blocks], "o-", color=CORE6_COLORS[arm], label=arm)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"block{b}" for b in blocks])
    ax.set_ylabel("lat95_mean_ms")
    ax.set_ylim(bottom=0)
    ax.set_title("HPA target-sensitivity diagnostic: H50 vs H100 vs H115 per block (not a ranking)")
    ax.legend(fontsize=9)
    _annotate_n(ax, len(blocks))
    _save(fig, "hpa_target_diagnostic")


def fig_sensitivity_comparison(results: dict) -> None:
    pc = results["primary_analysis"]["predictor_comparison_G30_P30_F30"]["block_paired_differences"]["lat95_mean_ms"]
    sens = results["sensitivity_analysis"]
    scopes = [
        ("primary (n=10)", pc),
        ("exclude block6 (n=9)", sens["exclude_block6"]["predictor_comparison"]["block_paired_differences"]["lat95_mean_ms"]),
        ("exclude block7 (n=9)", sens["exclude_block7"]["predictor_comparison"]["block_paired_differences"]["lat95_mean_ms"]),
        ("exclude both (n=8)", sens["exclude_block6_and_block7"]["predictor_comparison"]["block_paired_differences"]["lat95_mean_ms"]),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), sharey=False)
    for ax, comp in zip(axes, ["G30_vs_P30", "G30_vs_F30"]):
        labels = [s[0] for s in scopes]
        means = [s[1][comp]["mean_diff"] for s in scopes]
        los = [s[1][comp]["bootstrap_ci"]["ci_low"] for s in scopes]
        his = [s[1][comp]["bootstrap_ci"]["ci_high"] for s in scopes]
        y = list(range(len(labels)))
        colors = ["#1f77b4"] + ["#888888"] * 3
        for yi, m, lo, hi, c in zip(y, means, los, his, colors):
            ax.errorbar([m], [yi], xerr=[[m - lo], [hi - m]], fmt="o", color=c, ecolor=c, capsize=4)
        ax.axvline(0, color="red", linestyle="--", linewidth=1)
        ax.set_yticks(list(y))
        ax.set_yticklabels(labels, fontsize=9)
        ax.set_xlabel("lat95_mean_ms diff (arm1 − arm2), 95% bootstrap CI")
        ax.set_title(comp.replace("_", " "))
        ax.invert_yaxis()
    fig.suptitle("Primary vs disclosed sensitivity (whole-block exclusion) — direction and CI comparison")
    _save(fig, "sensitivity_comparison")


def main() -> int:
    df = pd.read_csv(OUT_DIR / "run_level_results.csv", dtype={"block_id": str})
    results = json.loads((OUT_DIR / "analysis_results.json").read_text(encoding="utf-8"))

    fig_latency_comparison(df)
    fig_slo_violation(df)
    fig_replica_seconds(df)
    fig_tradeoff(df)
    _paired_block_figure(df, "G30", "P30", "paired_G30_vs_P30")
    _paired_block_figure(df, "G30", "F30", "paired_G30_vs_F30")
    fig_hpa_target_diagnostic(df)
    fig_sensitivity_comparison(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
