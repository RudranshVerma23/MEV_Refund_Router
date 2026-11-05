"""Replay each collected swap through the router on forked mainnet (test/fork/SettleReplay.t.sol).

Usage (from the project root):
    python backtest/run_fork_replays.py --swaps backtest/data/swaps.csv --limit 1000 --workers 2

Needs ETH_RPC_URL in the environment (an archive node). For each swap it looks up the block's
base fee and the earlier same-block txs that touched the two pools (fast mode, see
SettleReplay.t.sol), then runs forge. Each run appends one line to
backtest/results/settle_results.csv. Already-processed swaps are skipped, so the script can
resume. Keep --workers small: parallel runs append to the same file and share the RPC rate limit.
"""

import argparse
import csv
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc import load_dotenv, SUSHI_USDC_WETH, RpcError, block_context  # noqa: E402

RESULTS = os.path.join("backtest", "results", "settle_results.csv")


def done_hashes() -> set[str]:
    if not os.path.exists(RESULTS):
        return set()
    with open(RESULTS) as f:
        return {line.split(",")[0].lower() for line in f if line.strip()}


def replay(row: dict) -> tuple[str, bool, str]:
    rpc = os.environ["ETH_RPC_URL"]
    partner = row.get("partner_pool") or SUSHI_USDC_WETH
    try:
        ctx = block_context(rpc, int(row["block"]), int(row["tx_index"]), [row["pool"], partner])
    except RpcError as e:
        return row["tx_hash"], False, f"block context failed: {e}"
    env = dict(
        os.environ,
        TX_HASH=row["tx_hash"],
        POOL=row["pool"],
        PARTNER_POOL=partner,
        AMOUNT0_IN=row["amount0_in"],
        AMOUNT1_IN=row["amount1_in"],
        **ctx,
    )
    if not ctx["PRIOR_TXS"]:
        env.pop("PRIOR_TXS")  # an empty list is passed by leaving the variable unset
    proc = subprocess.run(
        ["forge", "test", "--match-path", "test/fork/SettleReplay.t.sol", "-vv"],
        env=env,
        capture_output=True,
        text=True,
    )
    return row["tx_hash"], proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--swaps", default="backtest/data/swaps.csv")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=1, help="parallel forge runs (mind RPC rate limits)")
    args = ap.parse_args()

    if not os.environ.get("ETH_RPC_URL"):
        sys.exit("set ETH_RPC_URL to an archive-node RPC first")

    with open(args.swaps) as f:
        rows = list(csv.DictReader(f))
    # The router replays one-directional swaps only: exactly one of amount0_in / amount1_in is non-zero.
    rows = [r for r in rows if (int(r["amount0_in"]) == 0) != (int(r["amount1_in"]) == 0)]
    skip = done_hashes()
    todo = [r for r in rows if r["tx_hash"].lower() not in skip][: args.limit]
    print(f"{len(skip)} already done, {len(todo)} to replay")

    # Build once up front so parallel runs don't all try to compile.
    subprocess.run(["forge", "build"], check=True, capture_output=True)

    ok = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for tx_hash, success, log in pool.map(replay, todo):
            if success:
                ok += 1
            else:
                failed += 1
                print(f"FAILED {tx_hash}\n{log}", file=sys.stderr)
            if (ok + failed) % 25 == 0:
                print(f"{ok + failed}/{len(todo)} (ok={ok}, failed={failed})")
    print(f"done: ok={ok}, failed={failed}; results in {RESULTS}")


if __name__ == "__main__":
    main()
