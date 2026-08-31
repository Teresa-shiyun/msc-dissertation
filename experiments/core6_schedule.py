from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ARMS = ["H50", "H100", "H115", "G30", "P30", "F30"]
BASE_SEED = 20260710
REPO = Path(__file__).resolve().parents[1]


def build_schedule(arms: list[str], first_block: int, n_blocks: int,
                    base_seed: int, traffic: str,
                    run_order_offset: int = 0,
                    prev_last: str | None = None) -> list[dict]:
    order: list[dict] = []
    for i in range(n_blocks):
        block_id = first_block + i
        rng = random.Random(base_seed + block_id)
        block = arms[:]
        for _ in range(1000):
            rng.shuffle(block)
            if prev_last is None or block[0] != prev_last:
                break
        else:
            raise RuntimeError(f"block {block_id}: could not satisfy no-consecutive-same-arm constraint")
        prev_last = block[-1]
        for arm in block:
            order.append({
                "run_order": run_order_offset + len(order) + 1,
                "block_id": block_id,
                "arm": arm,
                "traffic": traffic,
                "rep": block_id,
            })
    return order


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--first-block", type=int, default=1)
    p.add_argument("--blocks", type=int, default=3,
                   help="Number of blocks to generate (Stage A pilot = 3).")
    p.add_argument("--base-seed", type=int, default=BASE_SEED,
                   help="Per-block seed = base_seed + block_id.")
    p.add_argument("--traffic", default="spike")
    p.add_argument("--arms", default=",".join(ARMS))
    p.add_argument("--run-order-offset", type=int, default=0)
    p.add_argument("--prev-last-arm", default=None,
                   help="Last arm of the immediately preceding block, if extending an existing schedule "
                        "(continuity constraint across the boundary). Omit for a fresh block 1.")
    p.add_argument("--out", type=Path,
                   default=REPO / "results" / "core6_v1" / "schedule.json")
    args = p.parse_args(argv)

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    if sorted(arms) != sorted(ARMS):
        raise SystemExit(f"ERROR: --arms must be exactly the core-6 set {ARMS}, got {arms}")

    order = build_schedule(
        arms, args.first_block, args.blocks, args.base_seed, args.traffic,
        args.run_order_offset, args.prev_last_arm,
    )

    by_block: dict[int, list[str]] = {}
    for row in order:
        by_block.setdefault(row["block_id"], []).append(row["arm"])
    balanced = all(sorted(v) == sorted(arms) for v in by_block.values())
    no_consec_internal = all(order[i]["arm"] != order[i - 1]["arm"] for i in range(1, len(order)))
    boundary_ok = (args.prev_last_arm is None) or (order[0]["arm"] != args.prev_last_arm)
    run_orders = [r["run_order"] for r in order]
    contiguous = run_orders == list(range(args.run_order_offset + 1,
                                           args.run_order_offset + 1 + len(order)))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_seed": args.base_seed,
        "seed_formula": "base_seed + block_id (independent RNG per block)",
        "first_block": args.first_block,
        "blocks": args.blocks,
        "arms": arms,
        "traffic": args.traffic,
        "total_runs": len(order),
        "run_order_offset": args.run_order_offset,
        "prev_last_arm": args.prev_last_arm,
        "block_balanced": balanced,
        "no_consecutive_same_arm_internal": no_consec_internal,
        "boundary_prev_last_ne_new_first": boundary_ok,
        "run_order_contiguous": contiguous,
        "execution_plan": "docs/execution_plan_hpa_predictor_v2.md",
        "schedule": order,
    }
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"base_seed={args.base_seed} first_block={args.first_block} blocks={args.blocks} total_runs={len(order)}")
    print(f"balanced per block            : {balanced}")
    print(f"no consecutive same arm       : {no_consec_internal}")
    print(f"boundary (prev-last != new-1) : {boundary_ok}")
    print(f"run_order contiguous          : {contiguous}")
    for row in order:
        print(f"  #{row['run_order']:>2}  block{row['block_id']}  {row['arm']:<5}  {row['traffic']}  rep{row['rep']}")
    print(f"\nwritten: {args.out}")

    ok = balanced and no_consec_internal and boundary_ok and contiguous
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
