import csv
import glob
import json
import os
import re
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
CPU_LIMIT_MILLI = 1000


def load_levels():
    rows = []
    for path in glob.glob(os.path.join(HERE, "level_*.json")):
        m = re.search(r"level_(\d+)\.json$", os.path.basename(path))
        if not m:
            continue
        target = int(m.group(1))
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        met = data["metrics"]
        rows.append({
            "target_rps": target,
            "observed_rps": met["http_reqs"]["rate"],
            "p95_ms": met["http_req_duration"]["p(95)"],
            "p90_ms": met["http_req_duration"]["p(90)"],
            "max_ms": met["http_req_duration"]["max"],
            "fail_rate": met["http_req_failed"]["value"],
            "n_reqs": met["http_reqs"]["count"],
        })
    rows.sort(key=lambda r: r["target_rps"])
    return rows


def load_cpu():
    path = os.path.join(HERE, "cpu_samples.csv")
    by_level = {}
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                lvl = int(row["level_rps"])
                milli = int(row["milli_cpu"])
            except (ValueError, KeyError):
                continue
            by_level.setdefault(lvl, []).append(milli)
    out = {}
    for lvl, vals in by_level.items():
        steady = vals[1:] if len(vals) > 1 else vals
        out[lvl] = statistics.mean(steady) if steady else float("nan")
    return out


def main():
    levels = load_levels()
    cpu = load_cpu()
    out_path = os.path.join(HERE, "calibration_table.csv")
    fields = ["target_rps", "observed_rps", "p95_ms", "p90_ms", "max_ms",
              "cpu_milli", "cpu_pct", "fail_rate", "n_reqs"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in levels:
            milli = cpu.get(r["target_rps"], float("nan"))
            r["cpu_milli"] = round(milli, 1) if milli == milli else ""
            r["cpu_pct"] = round(100.0 * milli / CPU_LIMIT_MILLI, 1) if milli == milli else ""
            w.writerow(r)

    print(f"{'tgtRPS':>6} {'obsRPS':>7} {'p95ms':>7} {'p90ms':>7} {'maxms':>8} {'cpu%':>6} {'fail':>6} {'reqs':>6}")
    for r in levels:
        cpu_pct = r["cpu_pct"] if r["cpu_pct"] != "" else "-"
        print(f"{r['target_rps']:>6} {r['observed_rps']:>7.1f} {r['p95_ms']:>7.1f} "
              f"{r['p90_ms']:>7.1f} {r['max_ms']:>8.1f} {str(cpu_pct):>6} "
              f"{r['fail_rate']:>6.3f} {r['n_reqs']:>6}")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
