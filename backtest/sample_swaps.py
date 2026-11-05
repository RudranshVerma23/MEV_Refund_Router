"""Build the backtest sample: user swaps, stratified by size, with weights back to the full year.

Usage (from the project root):
    python backtest/sample_swaps.py --swaps backtest/data/swaps_2024.csv --n 1000 \
        --out backtest/data/sample_1000.csv --threads 16

Why stratify: most swaps are small and create no arbitrage above gas, so a uniform sample would
mostly measure zero refunds. Each size bucket gets a quota (weighted toward large swaps), and
every sampled swap carries a `weight` = how many user swaps of the full period it represents.
Population totals in analyze.py are weighted sums, so oversampling large swaps doesn't bias them.

Why filter by `to`: a pool's Swap events include arbitrage bots (often the very backruns we want
to measure). Replaying a bot's trade as if it were a user's would be wrong. Only txs sent to a
known user-facing router or aggregator count as user swaps (ALLOWED_TO below). For each bucket
the script reports how many candidates were rejected, and the weights account for that rate.

Assumes a USDC/WETH pool with USDC as token0 (true for Uniswap V2 and Sushiswap USDC/WETH), so
the swap's USD size is simply its USDC amount.
"""

import argparse
import csv
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc import RpcError, call, load_dotenv  # noqa: E402

# User-facing routers and aggregators (Ethereum mainnet). Lower-case.
ALLOWED_TO = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2 Router02",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap Universal Router",
    "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b": "Uniswap Universal Router (old)",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch v5",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch v6",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x Exchange Proxy",
    "0x881d40237659c251811cec9c364ef91dc08d300c": "MetaMask Swaps",
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57": "ParaSwap v5",
    "0x6a000f20005980200259b80c5102003040001068": "ParaSwap v6.2",
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "KyberSwap MetaAggregator",
}

# (label, lower USD bound, quota share). Shares sum to 1; weighted toward large swaps.
BUCKETS = [
    ("<1k", 0, 0.10),
    ("1k-10k", 1_000, 0.15),
    ("10k-50k", 10_000, 0.25),
    ("50k-250k", 50_000, 0.30),
    (">=250k", 250_000, 0.20),
]


def bucket_of(usd: float) -> str:
    label = BUCKETS[0][0]
    for name, lo, _ in BUCKETS:
        if usd >= lo:
            label = name
    return label


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--swaps", default="backtest/data/swaps_2024.csv")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--out", default="backtest/data/sample_1000.csv")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--seed", type=int, default=2024)
    ap.add_argument("--rpc", default=os.environ.get("ETH_RPC_URL"))
    args = ap.parse_args()
    if not args.rpc:
        sys.exit("pass --rpc or set ETH_RPC_URL")

    with open(args.swaps) as f:
        rows = [r for r in csv.DictReader(f) if (int(r["amount0_in"]) == 0) != (int(r["amount1_in"]) == 0)]
    for r in rows:
        r["size_usd"] = (int(r["amount0_in"]) + int(r["amount0_out"])) / 1e6  # USDC side (token0)
        r["bucket"] = bucket_of(r["size_usd"])
    population = {name: [r for r in rows if r["bucket"] == name] for name, _, _ in BUCKETS}
    rng = random.Random(args.seed)
    for members in population.values():
        rng.shuffle(members)

    def tx_to(row: dict) -> str:
        try:
            return (call(args.rpc, "eth_getTransactionByHash", [row["tx_hash"]])["to"] or "").lower()
        except RpcError:
            return ""

    sample: list[dict] = []
    stats = {}
    carry = 0
    # Largest bucket first: if it has too few user swaps, its shortfall moves to the next bucket down.
    for name, _, share in reversed(BUCKETS):
        quota = round(args.n * share) + carry
        members = population[name]
        accepted, checked, rejected = [], 0, 0
        pos = 0
        while len(accepted) < quota and pos < len(members):
            batch = members[pos : pos + max(2 * (quota - len(accepted)), 32)]
            pos += len(batch)
            with ThreadPoolExecutor(max_workers=args.threads) as ex:
                for row, to in zip(batch, ex.map(tx_to, batch)):
                    checked += 1
                    if to in ALLOWED_TO and len(accepted) < quota:
                        row["tx_to"] = to
                        accepted.append(row)
                    elif to not in ALLOWED_TO:
                        rejected += 1
        carry = quota - len(accepted)
        user_rate = (checked - rejected) / checked if checked else 0
        est_users = len(members) * user_rate  # estimated user swaps in this bucket over the period
        weight = est_users / len(accepted) if accepted else 0
        for row in accepted:
            row["weight"] = f"{weight:.4f}"
        sample.extend(accepted)
        stats[name] = (len(members), checked, rejected, len(accepted), est_users, weight)
        print(f"{name:>9}: {len(members):>7} swaps, checked {checked}, non-user {rejected}, "
              f"sampled {len(accepted)} (weight {weight:.1f})", file=sys.stderr)

    if carry:
        print(f"note: only {len(sample)} user swaps available, short of {args.n}", file=sys.stderr)

    sample.sort(key=lambda r: (int(r["block"]), int(r["tx_index"])))
    fields = ["block", "tx_index", "tx_hash", "pool", "amount0_in", "amount1_in", "amount0_out", "amount1_out",
              "tx_to", "size_usd", "bucket", "weight"]
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(sample)

    print(f"\n{'bucket':>9} {'swaps':>8} {'user rate':>9} {'est. users':>10} {'sampled':>7} {'weight':>8}")
    for name, _, _ in BUCKETS:
        n, checked, rej, acc, est, wt = stats[name]
        rate = (checked - rej) / checked if checked else 0
        print(f"{name:>9} {n:>8} {rate:>9.0%} {est:>10.0f} {acc:>7} {wt:>8.1f}")
    print(f"wrote {len(sample)} swaps to {args.out}")


if __name__ == "__main__":
    main()
