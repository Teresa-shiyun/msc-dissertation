from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

import qc_checks as qc
from pod_lifecycle import LifecycleTracker
from host_monitor import HostMonitor

REPO_ROOT = Path(__file__).resolve().parents[1]
CLUSTER_NODE_NAMES = ["cloud-edge-control-plane", "cloud-edge-worker", "cloud-edge-worker2"]
DEFAULT_MATRIX = REPO_ROOT / "experiments" / "matrix.yaml"
DEFAULT_RESULTS_DIR = REPO_ROOT / "results" / "raw"
APPLY_ARM_SCRIPT = REPO_ROOT / "experiments" / "scripts" / "apply-arm.ps1"
EXPORT_METRICS_SCRIPT = REPO_ROOT / "experiments" / "scripts" / "export-metrics.py"
JOB_TEMPLATE = REPO_ROOT / "experiments" / "k6-job-template.yaml"
LOADGEN_DIR = REPO_ROOT / "workload" / "loadgen"


def run(cmd: list[str], *, check: bool = True, capture: bool = False, **kw) -> subprocess.CompletedProcess:
    pretty = " ".join(f'"{a}"' if " " in a else a for a in cmd)
    print(f"  $ {pretty}", file=sys.stderr)
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kw,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_compact(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


@dataclass(frozen=True)
class Arm:
    id: str
    apply_arm: str
    description: str = ""
    init_replicas: Optional[int] = None
    restart_agent: bool = False


@dataclass(frozen=True)
class Traffic:
    id: str
    description: str
    script_file: str
    expected_duration_seconds: int


@dataclass(frozen=True)
class Defaults:
    warmup_seconds: int
    cooldown_seconds: int
    repetitions: int
    k6_image: str
    work_n: str


def load_matrix(path: Path) -> tuple[Defaults, list[Arm], list[Traffic]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    d = data["defaults"]
    defaults = Defaults(
        warmup_seconds=int(d["warmup_seconds"]),
        cooldown_seconds=int(d["cooldown_seconds"]),
        repetitions=int(d["repetitions"]),
        k6_image=str(d["k6_image"]),
        work_n=str(d["work_n"]),
    )
    arms = [Arm(**a) for a in data["arms"]]
    traffics = [Traffic(**t) for t in data["traffics"]]
    return defaults, arms, traffics


def apply_arm(arm: Arm) -> None:
    cmd = ["powershell", "-NoProfile", "-File", str(APPLY_ARM_SCRIPT), "-Arm", arm.apply_arm]
    if arm.init_replicas is not None:
        cmd += ["-BaselineReplicas", str(arm.init_replicas)]
    run(cmd)


def reset_workload_to_one_replica() -> None:
    run(["kubectl", "-n", "workload", "scale", "deployment/workload-cloud", "--replicas=1"])
    run(["kubectl", "-n", "workload", "rollout", "status", "deployment/workload-cloud", "--timeout=60s"])


def restart_agent_pod() -> None:
    cp = run(
        ["kubectl", "-n", "tools", "get", "deployment", "orchestration-agent", "-o", "name"],
        check=False, capture=True,
    )
    if cp.returncode != 0 or not cp.stdout.strip():
        print("  (orchestration-agent not deployed yet — skipping restart)", file=sys.stderr)
        return
    run(["kubectl", "-n", "tools", "rollout", "restart", "deployment/orchestration-agent"])
    run(["kubectl", "-n", "tools", "rollout", "status", "deployment/orchestration-agent", "--timeout=120s"])


def refresh_k6_configmap() -> None:
    js_files = sorted(LOADGEN_DIR.glob("*.js"))
    if not js_files:
        raise RuntimeError(f"no k6 scripts found in {LOADGEN_DIR}")
    args = ["kubectl", "create", "configmap", "k6-scripts", "-n", "workload"]
    for f in js_files:
        args += [f"--from-file={f.name}={f}"]
    args += ["--dry-run=client", "-o", "yaml"]
    cp = run(args, capture=True)
    apply = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=cp.stdout, text=True, encoding="utf-8", errors="replace", check=True,
    )


def render_job_yaml(template: str, **subs: str) -> str:
    return template.format(**subs)


def submit_k6_job(
    job_name: str,
    run_id: str,
    arm_id: str,
    traffic_id: str,
    script_name: str,
    k6_image: str,
    work_n: str,
    target_url: str,
) -> None:
    template_text = JOB_TEMPLATE.read_text(encoding="utf-8")
    rendered = render_job_yaml(
        template_text,
        job_name=job_name,
        run_id=run_id,
        arm_id=arm_id,
        traffic_id=traffic_id,
        script_name=script_name,
        k6_image=k6_image,
        work_n=work_n,
        target_url=target_url,
    )
    p = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=rendered, text=True, encoding="utf-8", errors="replace", check=True,
    )


def wait_for_job(
    job_name: str,
    timeout_seconds: int,
    trackers: Optional[list[LifecycleTracker]] = None,
    host_monitor=None,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    if trackers:
        for t in trackers:
            t.observe(ts=time.time())
    if host_monitor is not None:
        host_monitor.sample(ts=time.time())
    while time.monotonic() < deadline:
        cp = subprocess.run(
            [
                "kubectl", "-n", "workload", "get", "job", job_name,
                "-o", "jsonpath={.status.succeeded}|{.status.failed}",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if trackers:
            for t in trackers:
                t.observe(ts=time.time())
        if host_monitor is not None:
            host_monitor.sample(ts=time.time())
        if cp.returncode == 0:
            ok, fail = (cp.stdout.split("|") + ["", ""])[:2]
            if ok and ok != "0":
                return "complete"
            if fail and fail != "0":
                return "failed"
        time.sleep(5)
    return "timeout"


def capture_job_logs(job_name: str) -> str:
    cp = subprocess.run(
        ["kubectl", "-n", "workload", "logs", f"job/{job_name}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return cp.stdout if cp.returncode == 0 else f"<failed to capture logs: {cp.stderr}>"


def capture_agent_logs(since_iso: str) -> str:
    cp = subprocess.run(
        ["kubectl", "-n", "tools", "logs", "deploy/orchestration-agent", f"--since-time={since_iso}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return cp.stdout if cp.returncode == 0 else f"<failed to capture agent logs: {cp.stderr}>"


def export_metrics(start: datetime, end: datetime, out_csv: Path, prom_url: str) -> None:
    cmd = [
        sys.executable, str(EXPORT_METRICS_SCRIPT),
        "--start", utc_iso(start),
        "--end", utc_iso(end),
        "--step", "10s",
        "--prometheus-url", prom_url,
        "--output", str(out_csv),
    ]
    run(cmd)


def filter_by_id(items, allow_csv: Optional[str]):
    if not allow_csv:
        return items
    allow = {x.strip() for x in allow_csv.split(",") if x.strip()}
    return [i for i in items if i.id in allow]


def check_session_admission(admission_log: Path, session_id: str) -> dict:
    if not admission_log.exists():
        raise RuntimeError(f"admission log not found: {admission_log}")
    with admission_log.open("r", encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("session_id") == session_id]
    if not rows:
        raise RuntimeError(f"no admission-check record for session_id={session_id} in {admission_log}")
    last = rows[-1]
    if last.get("verdict") != "PASS":
        raise RuntimeError(
            f"session_id={session_id} latest admission verdict={last.get('verdict')!r} "
            f"(not PASS) -- refusing to start formal runs."
        )
    status = last.get("status", "")
    if status != "OK":
        raise RuntimeError(
            f"session_id={session_id} admission status={status!r} (not 'OK') -- "
            f"refusing to start formal runs. reason: {last.get('status_reason', '(none recorded)')}"
        )
    return last


def run_one(
    arm: Arm,
    traffic: Traffic,
    rep: int,
    defaults: Defaults,
    results_dir: Path,
    prom_url: str,
    target_url: str,
    block_id: Optional[int] = None,
    session_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    schedule_seed: Optional[int] = None,
    calibration_p95_ms: str = "",
    enable_host_monitor: bool = False,
) -> dict:
    started = utc_now()
    run_id = f"{utc_compact(started)}_{traffic.id}_{arm.id}_rep{rep}"
    job_name = f"k6-{traffic.id}-{arm.id}-rep{rep}-{utc_compact(started)}".lower().replace("_", "-")
    out_dir = results_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== {run_id} ===", file=sys.stderr)

    reset_workload_to_one_replica()
    if arm.restart_agent:
        restart_agent_pod()

    apply_arm(arm)

    print(f"  warmup {defaults.warmup_seconds}s ...", file=sys.stderr)
    time.sleep(defaults.warmup_seconds)

    refresh_k6_configmap()

    is_hpa_arm = arm.id in qc.EXPECTED_HPA_AVERAGE_VALUE
    is_agent_arm = arm.id in qc.EXPECTED_AGENT_POLICY

    workload_tracker = LifecycleTracker("workload", "app=workload,tier=cloud", role="workload")
    agent_tracker = LifecycleTracker("tools", "app=orchestration-agent", role="agent") if is_agent_arm else None
    workload_tracker.observe(ts=time.time())
    if agent_tracker:
        agent_tracker.observe(ts=time.time())

    host_monitor = HostMonitor(CLUSTER_NODE_NAMES, prometheus_url=prom_url) if enable_host_monitor else None
    if host_monitor is not None:
        host_monitor.sample(ts=time.time())

    t_start = utc_now()
    script_name = Path(traffic.script_file).name
    submit_k6_job(
        job_name=job_name,
        run_id=run_id,
        arm_id=arm.id,
        traffic_id=traffic.id,
        script_name=script_name,
        k6_image=defaults.k6_image,
        work_n=defaults.work_n,
        target_url=target_url,
    )

    timeout = traffic.expected_duration_seconds + 600
    active_trackers = [workload_tracker] + ([agent_tracker] if agent_tracker else [])
    status = wait_for_job(job_name, timeout, trackers=active_trackers, host_monitor=host_monitor)
    print(f"  k6 job: {status}", file=sys.stderr)

    workload_tracker.observe(ts=time.time())
    if agent_tracker:
        agent_tracker.observe(ts=time.time())
    if host_monitor is not None:
        host_monitor.sample(ts=time.time())

    _, workload_pod_violations = workload_tracker.check_validity()
    _, agent_pod_violations = (agent_tracker.check_validity() if agent_tracker else (True, []))

    lifecycle_path = out_dir / "pod_lifecycle.jsonl"
    workload_tracker.write_jsonl(lifecycle_path)
    if agent_tracker:
        agent_tracker.write_jsonl(lifecycle_path)
    if host_monitor is not None:
        host_monitor.write_jsonl(out_dir / "host_monitor.jsonl")

    actual_hpa_average_value = (
        qc.get_hpa_average_value("workload", qc.HPA_NAME_FOR_ARM[arm.id]) if is_hpa_arm else None
    )
    actual_agent_policy_env = qc.get_agent_policy_env() if is_agent_arm else None

    print(f"  cooldown {defaults.cooldown_seconds}s ...", file=sys.stderr)
    time.sleep(defaults.cooldown_seconds)
    t_end = utc_now()

    (out_dir / "k6_stdout.log").write_text(capture_job_logs(job_name), encoding="utf-8")

    if arm.restart_agent or "agent" in arm.id:
        (out_dir / "agent_events.jsonl").write_text(
            capture_agent_logs(utc_iso(t_start)), encoding="utf-8",
        )

    try:
        export_metrics(t_start, t_end, out_dir / "metrics.csv", prom_url)
    except subprocess.CalledProcessError as e:
        print(f"  WARN: export-metrics failed: {e}", file=sys.stderr)

    metrics_path = out_dir / "metrics.csv"
    prom_ts = qc.load_prometheus_timestamps(metrics_path)
    qc_ok, qc_reasons = qc.run_all_qc(
        metrics_csv_path=metrics_path,
        arm_id=arm.id,
        is_hpa_arm=is_hpa_arm,
        is_agent_arm=is_agent_arm,
        workload_pod_violations=workload_pod_violations,
        agent_pod_violations=agent_pod_violations,
        actual_hpa_average_value=actual_hpa_average_value,
        actual_agent_policy_env=actual_agent_policy_env,
        prometheus_timestamps=prom_ts if prom_ts else None,
    )
    valid = (status == "complete") and qc_ok
    reasons = list(qc_reasons)
    if status != "complete":
        reasons.insert(0, f"k6_status={status}")
    invalid_reason = "" if valid else "; ".join(reasons)

    meta = {
        "run_id": run_id,
        "arm": arm.id,
        "traffic": traffic.id,
        "repetition": rep,
        "block_id": block_id,
        "session_id": session_id,
        "batch_id": batch_id,
        "schedule_seed": schedule_seed,
        "calibration_p95_ms": calibration_p95_ms,
        "validity": "valid" if valid else "invalid_needs_review",
        "invalid_reason": invalid_reason,
        "started_at": utc_iso(started),
        "t_start": utc_iso(t_start),
        "t_end": utc_iso(t_end),
        "k6_status": status,
        "warmup_seconds": defaults.warmup_seconds,
        "cooldown_seconds": defaults.cooldown_seconds,
        "k6_script": traffic.script_file,
        "work_n": defaults.work_n,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def already_completed(results_dir: Path) -> set[tuple[str, str, int]]:
    index = results_dir / "runs.csv"
    if not index.exists():
        return set()
    done: set[tuple[str, str, int]] = set()
    with index.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("k6_status") != "complete":
                continue
            try:
                done.add((row["traffic"], row["arm"], int(row["repetition"])))
            except (KeyError, ValueError):
                continue
    return done


def append_runs_index(meta: dict, results_dir: Path) -> None:
    index = results_dir / "runs.csv"
    full_fields = [
        "run_id", "arm", "traffic", "repetition",
        "started_at", "t_start", "t_end", "k6_status",
        "warmup_seconds", "cooldown_seconds", "k6_script", "work_n",
        "block_id", "session_id", "batch_id", "schedule_seed",
        "calibration_p95_ms", "validity", "invalid_reason",
    ]
    if index.exists():
        with index.open("r", encoding="utf-8", newline="") as f:
            existing_header = next(csv.reader(f), None)
        fields = existing_header if existing_header else full_fields
        write_header = existing_header is None
    else:
        fields = full_fields
        write_header = True
    with index.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow({k: meta.get(k, "") for k in fields})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    p.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    p.add_argument("--arm", help="Comma-separated arm IDs to include (default: all)")
    p.add_argument("--traffic", help="Comma-separated traffic IDs to include (default: all)")
    p.add_argument("--repetitions", type=int, default=None,
                   help="Override default repetitions from matrix.yaml")
    p.add_argument("--prometheus-url", default=os.getenv("PROMETHEUS_URL", "http://localhost:9090"))
    p.add_argument("--target-url", default="http://workload.workload.svc",
                   help="k6 TARGET_URL injected into each Job. Default is the "
                        "aggregate service (cloud+edge); pass "
                        "http://workload-cloud.workload.svc for cloud-only v2 runs.")
    p.add_argument("--schedule-file", type=Path, default=None,
                   help="Optional schedule.json (from ablation_schedule.py) that "
                        "fixes the exact run order (block-randomised). When given, "
                        "cells run in that order instead of the default "
                        "traffic x arm x repetition nesting.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the cells that would run, then exit.")
    p.add_argument("--skip-completed", action="store_true",
                   help="Skip (traffic, arm, repetition) cells already recorded "
                        "as complete in results/raw/runs.csv. Use this to resume "
                        "after stopping a long matrix mid-way.")
    p.add_argument("--session-id", default=None,
                   help="Session identifier written to meta.json and runs.csv.")
    p.add_argument("--batch-id", default=None,
                   help="Batch identifier written to meta.json and runs.csv.")
    p.add_argument("--require-admission", action="store_true",
                   help="Refuse to start any run unless results/calibration/"
                        "session_admission_log.csv has a PASS row for --session-id.")
    p.add_argument("--admission-log", type=Path,
                   default=REPO_ROOT / "results" / "calibration" / "session_admission_log.csv")
    p.add_argument("--stop-on-invalid", action="store_true",
                   help="Stop the whole schedule immediately (do not run the next cell) the "
                        "moment a run's computed validity is not 'valid'. No silent retry. "
                        "Off by default for ad-hoc runs.")
    p.add_argument("--host-monitor", action="store_true",
                   help="Sample descriptive host/node state (node CPU/mem, pod CPU, throttling, "
                        "restarts/evictions, node pressure) on the same fixed cadence as pod-lifecycle "
                        "polling, writing host_monitor.jsonl per run.")
    args = p.parse_args(argv)

    defaults, arms, traffics = load_matrix(args.matrix)
    arms = filter_by_id(arms, args.arm)
    traffics = filter_by_id(traffics, args.traffic)
    repetitions = args.repetitions or defaults.repetitions

    if not arms or not traffics:
        print("ERROR: arms or traffics filter left nothing to do.", file=sys.stderr)
        return 2

    schedule_seed = None
    if args.schedule_file:
        sched = json.loads(args.schedule_file.read_text(encoding="utf-8"))
        arms_by_id = {a.id: a for a in arms}
        traffics_by_id = {t.id: t for t in traffics}
        schedule_seed = sched.get("base_seed", sched.get("seed"))
        cells = []
        for row in sched["schedule"]:
            a = arms_by_id.get(row["arm"])
            t = traffics_by_id.get(row["traffic"])
            if a is None or t is None:
                print(f"  (schedule row {row} filtered out by --arm/--traffic)", file=sys.stderr)
                continue
            cells.append((t, a, int(row["rep"]), row.get("block_id")))
        print(f"Using fixed block-randomised order from {args.schedule_file} "
              f"(seed={schedule_seed}).", file=sys.stderr)
    else:
        cells = [(t, a, r, None) for t in traffics for a in arms for r in range(1, repetitions + 1)]

    if args.skip_completed:
        done = already_completed(args.results_dir)
        before = len(cells)
        cells = [(t, a, r, b) for t, a, r, b in cells if (t.id, a.id, r) not in done]
        print(f"Skipping {before - len(cells)} already-completed cells "
              f"({len(done)} unique completions on record).", file=sys.stderr)

    print(f"Plan: {len(cells)} runs.", file=sys.stderr)
    for t, a, r, b in cells:
        block_label = f" block{b}" if b is not None else ""
        print(f"  - {t.id:<8} × {a.id:<14} rep{r}{block_label}", file=sys.stderr)
    if args.dry_run:
        return 0
    if not cells:
        print("Nothing to do.", file=sys.stderr)
        return 0

    prom_ok, prom_reason = qc.check_prometheus_reachable_for_formal_cells(args.prometheus_url)
    if not prom_ok:
        print(
            f"ERROR: mandatory formal-cell preflight failed: Prometheus not reachable/queryable at "
            f"{args.prometheus_url} ({prom_reason}). A cluster-internal-healthy Prometheus is not "
            f"sufficient -- this exact URL must work from here, the same way experiments/scripts/export-metrics.py "
            f"will use it after each cell. Start a port-forward first, e.g.:\n"
            f"  kubectl -n monitoring port-forward svc/kube-prom-stack-prometheus 9090:9090\n"
            f"No formal cell will be started.",
            file=sys.stderr,
        )
        return 5

    calibration_p95_ms = ""
    if args.require_admission:
        if not args.session_id or not args.batch_id:
            print("ERROR: --require-admission needs both --session-id and --batch-id.", file=sys.stderr)
            return 2
        try:
            admit_row = check_session_admission(args.admission_log, args.session_id)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 3
        calibration_p95_ms = admit_row.get("p95_ms", "")
        print(f"Admission check OK: session_id={args.session_id} p95={calibration_p95_ms}ms verdict=PASS",
              file=sys.stderr)

    args.results_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for t, a, r, b in cells:
        try:
            meta = run_one(
                arm=a, traffic=t, rep=r,
                defaults=defaults,
                results_dir=args.results_dir,
                prom_url=args.prometheus_url,
                target_url=args.target_url,
                block_id=b,
                session_id=args.session_id,
                batch_id=args.batch_id,
                schedule_seed=schedule_seed,
                calibration_p95_ms=calibration_p95_ms,
                enable_host_monitor=args.host_monitor,
            )
            append_runs_index(meta, args.results_dir)
            if args.stop_on_invalid and meta.get("validity") != "valid":
                print(
                    f"\nSTOPPED: {meta['run_id']} validity={meta.get('validity')} "
                    f"invalid_reason={meta.get('invalid_reason')!r}. "
                    f"--stop-on-invalid is set: not running the next cell. "
                    f"No automatic retry.",
                    file=sys.stderr,
                )
                return 4
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130
        except Exception as e:
            failures += 1
            print(f"FAIL: {t.id} × {a.id} rep{r}: {e}", file=sys.stderr)
            if args.stop_on_invalid:
                print(
                    "\nSTOPPED: an exception during the run counts as a stop condition "
                    "under --stop-on-invalid. Not running the next cell.",
                    file=sys.stderr,
                )
                return 4

    print(f"\nDone. {len(cells) - failures} ok, {failures} failed.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
