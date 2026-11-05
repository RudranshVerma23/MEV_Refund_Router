"""Turn settle_results.csv (from test/fork/SettleReplay.t.sol) into refund and gas statistics.

Usage:
    python backtest/analyze.py [--results backtest/results/settle_results.csv] [--rows-out backtest/results/rows.csv]

Measured on the fork, per swap: user output (plain vs router), the arbitrage the backrun
captured, and execution gas for (1) a plain pair swap, (2) router fill, (3) full settle.

ASSUMPTIONS (state them in the report and vary them):
  * TIP_WEI: priority fee on top of the block's base fee.
  * INTRINSIC / CALLDATA gas: the harness measures execution gas only; a real tx adds 21,000
    plus calldata (16 gas per non-zero byte). The values below are estimates.
  * Bid model: the winner bids a fraction BID_FRACTIONS of the arbitrage net of all gas.
    Calibrate it from real backruns (coinbase transfer + tip divided by profit).
  * Attribution: only arbitrage CREATED by the swap counts (arbProfit - arbBefore).
  * USD: from the pool's own reserves; valid only for USDC (6 dp) / WETH pools.
"""

import argparse
import csv
import statistics

TIP_WEI = 1_000_000_000  # 1 gwei
INTRINSIC = 21_000
CALLDATA_SETTLE = 12_000  # ~900-byte settle() calldata (ESTIMATE; measure with cast estimate)
CALLDATA_PLAIN = 3_000  # a Router02 swap's calldata (ESTIMATE)
BID_FRACTIONS = [0.5, 0.8, 0.9, 0.99]
REFUND_SHARES = [0.5, 0.9]

COLUMNS = [
    "tx", "block", "basefee", "token_in", "amount_in", "plain_out", "router_out", "arb_before",
    "arb_profit", "bid", "refund", "gas_plain", "gas_fill", "gas_settle", "other_per_weth",
]
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def load(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in csv.reader(f):
            if not line:
                continue
            if len(line) != len(COLUMNS):
                print(f"skipping malformed line ({len(line)} fields)")
                continue
            r = dict(zip(COLUMNS, line))
            for k in COLUMNS:
                if k not in ("tx", "token_in"):
                    r[k] = int(r[k])
            rows.append(r)
    return rows


def med(xs):
    return statistics.median(xs) if xs else 0


def report_by_bucket(rows: list[dict], sample_path: str, f: float = 0.9, s: float = 0.9) -> None:
    """Per size bucket, and weighted up to every user swap in the sampled period."""
    with open(sample_path) as fh:
        meta = {r["tx_hash"].lower(): r for r in csv.DictReader(fh)}
    order = ["<1k", "1k-10k", "10k-50k", "50k-250k", ">=250k"]
    groups: dict[str, list[dict]] = {b: [] for b in order}
    for r in rows:
        m = meta.get(r["tx"].lower())
        if m:
            r["bucket"], r["weight"] = m["bucket"], float(m["weight"])
            groups.setdefault(m["bucket"], []).append(r)

    print()
    print(f"BY SWAP SIZE (bid fraction {f}, refund share {s})")
    print(f"{'bucket':>9} {'n':>5} {'auctioned':>9} {'median refund ETH':>18} {'mean refund ETH':>16} {'refund USD (sum)':>16}")
    total_eth = total_usd = 0.0
    for b in order:
        g = groups.get(b, [])
        if not g:
            continue
        refunds = [r["net"] * f * s if r["auctioned"] else 0 for r in g]
        usd = sum(x / 1e18 * r["usd_per_eth"] for x, r in zip(refunds, g))
        auctioned = sum(r["auctioned"] for r in g)
        print(f"{b:>9} {len(g):>5} {auctioned / len(g):>9.1%} {med(refunds) / 1e18:>18.6f} "
              f"{sum(refunds) / len(g) / 1e18:>16.6f} {usd:>16,.0f}")
        total_eth += sum(x * r["weight"] for x, r in zip(refunds, g)) / 1e18
        total_usd += sum(x / 1e18 * r["usd_per_eth"] * r["weight"] for x, r in zip(refunds, g))
    users = sum(r["weight"] for g in groups.values() for r in g)
    print(f"Weighted to the whole period (~{users:,.0f} user swaps): total refunds ~{total_eth:.3f} ETH (~${total_usd:,.0f}).")
    print("This is a lower bound: only the Uniswap V2 / Sushiswap arbitrage is captured.")
    rest = [r for g in groups.values() for r in g if not r["auctioned"]]
    relay = [(r["gas_fill"] + INTRINSIC + CALLDATA_PLAIN) * r["gas_price"] * r["weight"] for r in rest]
    relay_usd = sum(x / 1e18 * r["usd_per_eth"] for x, r in zip(relay, rest))
    print(f"If the auctioneer relayed every non-auctioned swap via fill(), weighted gas ~{sum(relay) / 1e18:,.0f} ETH "
          f"(~${relay_usd:,.0f}), far more than the refunds: route only predicted-profitable swaps through the router.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="backtest/results/settle_results.csv")
    ap.add_argument("--rows-out", default="", help="optional per-swap CSV for plotting")
    ap.add_argument("--sample", default="", help="sample CSV from sample_swaps.py: adds per-bucket and weighted results")
    args = ap.parse_args()

    rows = load(args.results)
    if not rows:
        raise SystemExit("no results yet: run backtest/run_fork_replays.py first")

    for r in rows:
        gp = r["basefee"] + TIP_WEI
        r["gas_price"] = gp
        r["usd_per_eth"] = r["other_per_weth"] / 1e6  # USDC(6dp)/WETH pools only
        r["created"] = max(r["arb_profit"] - r["arb_before"], 0)
        r["settle_tx_gas"] = r["gas_settle"] + INTRINSIC + CALLDATA_SETTLE
        r["plain_tx_gas"] = r["gas_plain"] + INTRINSIC + CALLDATA_PLAIN
        r["net"] = r["created"] - r["settle_tx_gas"] * gp if r["gas_settle"] else 0
        r["auctioned"] = r["net"] > 0
        # swap size in ETH
        if r["token_in"].lower() == WETH:
            r["swap_eth"] = r["amount_in"] / 1e18
        else:
            r["swap_eth"] = (r["amount_in"] / 1e6) / r["usd_per_eth"] if r["usd_per_eth"] else 0

    n = len(rows)
    auctioned = [r for r in rows if r["auctioned"]]
    mismatched = [r for r in rows if r["router_out"] != r["plain_out"]]

    print(f"swaps replayed                       : {n}")
    print(f"router output != plain output        : {len(mismatched)} (must be 0)")
    print(f"swap created any arbitrage           : {sum(r['created'] > 0 for r in rows)}")
    print(f"arbitrage > all gas (auctioned)      : {len(auctioned)} ({100 * len(auctioned) / n:.1f}%)")
    print()
    print("GAS (execution gas; medians over all swaps)")
    print(f"  plain pair swap                    : {med([r['gas_plain'] for r in rows]):,.0f}")
    print(f"  router fill (no backrun)           : {med([r['gas_fill'] for r in rows]):,.0f}")
    print(f"  router overhead = fill - plain     : {med([r['gas_fill'] - r['gas_plain'] for r in rows]):,.0f}")
    if auctioned:
        print(f"  full settle (auctioned swaps)      : {med([r['gas_settle'] for r in auctioned]):,.0f}")
        print(f"  backrun + payment = settle - fill  : {med([r['gas_settle'] - r['gas_fill'] for r in auctioned]):,.0f}")
        print(f"  settle tx cost, median (ETH)       : {med([r['settle_tx_gas'] * r['gas_price'] for r in auctioned]) / 1e18:.6f}")
    print()
    if not auctioned:
        print("No swap created arbitrage above gas cost in this sample.")
        return

    print("REFUND TO USERS (auctioned swaps only); rows = bid fraction, columns = refund share")
    header = "bid_frac | " + " | ".join(f"r={int(s * 100)}%: total ETH / total USD / median ETH" for s in REFUND_SHARES)
    print(header)
    for f in BID_FRACTIONS:
        cells = []
        for s in REFUND_SHARES:
            refunds = [r["net"] * f * s for r in auctioned]
            usd = sum(x / 1e18 * r["usd_per_eth"] for x, r in zip(refunds, auctioned))
            cells.append(f"{sum(refunds) / 1e18:.4f} / ${usd:,.0f} / {med(refunds) / 1e18:.6f}")
        print(f"{f:<8} | " + " | ".join(cells))
    print()

    f, s = 0.9, 0.9
    pct_of_mev = [r["net"] * f * s / r["created"] * 100 for r in auctioned]
    pct_of_swap = [r["net"] * f * s / 1e18 / r["swap_eth"] * 100 for r in auctioned if r["swap_eth"]]
    saved_gas = [r["plain_tx_gas"] * r["gas_price"] for r in auctioned]
    benefit = [r["net"] * f * s + g for r, g in zip(auctioned, saved_gas)]
    print(f"At bid fraction {f} and refund share {s}:")
    print(f"  refund as % of created MEV, median : {med(pct_of_mev):.1f}%")
    print(f"  refund as % of swap size, median   : {med(pct_of_swap):.4f}%")
    print(f"  user benefit vs plain swap, median : {med(benefit) / 1e18:.6f} ETH (refund + gas the user no longer pays)")
    print()
    fill_cost = [(r["gas_fill"] + INTRINSIC + CALLDATA_PLAIN) * r["gas_price"] for r in rows if not r["auctioned"]]
    print(f"Non-auctioned swaps go to fill(): {len(fill_cost)}; if the auctioneer relays them, it pays")
    print(f"  {sum(fill_cost) / 1e18:.4f} ETH of gas in total (to be covered by the treasury share).")

    if args.sample:
        report_by_bucket(rows, args.sample)

    if args.rows_out:
        keys = COLUMNS + ["gas_price", "usd_per_eth", "created", "settle_tx_gas", "plain_tx_gas", "net", "auctioned", "swap_eth", "bucket", "weight"]
        with open(args.rows_out, "w", newline="") as fo:
            w = csv.DictWriter(fo, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"\nper-swap rows written to {args.rows_out}")


if __name__ == "__main__":
    main()
