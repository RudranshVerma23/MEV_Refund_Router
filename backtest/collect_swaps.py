"""Collect historical Uniswap V2 swaps to replay in the backtest.

Usage (from the project root):
    python backtest/collect_swaps.py --rpc $ETH_RPC_URL \
        --from-block 20000000 --to-block 20050000 --out backtest/data/swaps.csv

Writes one row per swap transaction: block, tx_index, tx_hash, pool, amounts and (with
--with-to) the tx's `to` address. Standard library only.

TODO (sampling; state the choices in your report):
  * filter to USER swaps: keep txs whose `to` is a known router (Uniswap V2 Router02, Universal
    Router, 1inch, ...) and drop txs that are themselves arbitrage (several Swap logs forming a cycle);
  * stratify by swap size: small swaps rarely create arbitrage above gas, so a uniform sample is
    dominated by them;
  * add more pools (other WETH pairs, other V2 forks) once the replay handles them.
"""

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc import load_dotenv, SWAP_TOPIC, UNI_V2_USDC_WETH, call  # noqa: E402

UNI_V2_ROUTER02 = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("ETH_RPC_URL"))
    ap.add_argument("--pool", default=UNI_V2_USDC_WETH)
    ap.add_argument("--from-block", type=int, required=True)
    ap.add_argument("--to-block", type=int, required=True)
    ap.add_argument("--chunk", type=int, default=100, help="blocks per eth_getLogs call (dRPC free: max 100)")
    ap.add_argument("--threads", type=int, default=1, help="parallel eth_getLogs calls (mind the RPC rate limit)")
    ap.add_argument("--with-to", action="store_true", help="also fetch each tx's `to` (1 extra RPC call per swap)")
    ap.add_argument("--only-router02", action="store_true", help="keep only txs sent to Uniswap V2 Router02")
    ap.add_argument("--out", default="backtest/data/swaps.csv")
    args = ap.parse_args()
    if not args.rpc:
        sys.exit("pass --rpc or set ETH_RPC_URL")

    def fetch(start: int) -> list:
        end = min(start + args.chunk - 1, args.to_block)
        return call(args.rpc, "eth_getLogs", [{"address": args.pool, "fromBlock": hex(start), "toBlock": hex(end), "topics": [SWAP_TOPIC]}])

    starts = list(range(args.from_block, args.to_block + 1, args.chunk))
    seen: set[str] = set()
    rows = []
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        for i, logs in enumerate(ex.map(fetch, starts), 1):  # map keeps block order
            for lg in logs:
                tx_hash = lg["transactionHash"]
                if tx_hash in seen:  # one row per tx; multi-swap txs are usually bots
                    continue
                seen.add(tx_hash)
                data = bytes.fromhex(lg["data"][2:])
                a0in, a1in, a0out, a1out = (int.from_bytes(data[j : j + 32], "big") for j in range(0, 128, 32))
                to = ""
                if args.with_to or args.only_router02:
                    to = (call(args.rpc, "eth_getTransactionByHash", [tx_hash])["to"] or "").lower()
                    if args.only_router02 and to != UNI_V2_ROUTER02:
                        continue
                rows.append([int(lg["blockNumber"], 16), int(lg["transactionIndex"], 16), tx_hash, args.pool, a0in, a1in, a0out, a1out, to])
            if i % 500 == 0 or i == len(starts):
                print(f"{i}/{len(starts)} chunks: {len(rows)} swaps so far", file=sys.stderr)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["block", "tx_index", "tx_hash", "pool", "amount0_in", "amount1_in", "amount0_out", "amount1_out", "tx_to"])
        w.writerows(rows)
    print(f"wrote {len(rows)} swaps to {args.out}")


if __name__ == "__main__":
    main()
