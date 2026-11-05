"""Example searcher bot: watches auction hints, values the backrun, bids, settles if it wins.

Run (from the project root):
    SEARCHER_PK=0x... python -m offchain.searcher.bot --service http://127.0.0.1:8600 \
        --rpc http://127.0.0.1:8545 --router 0x... --executor 0x... --bid-fraction 0.8 --name alice

Strategy (deliberately simple, so different bots show different bid shading):
    value = estimated arbitrage profit - own gas cost of settle()
    bid   = bid_fraction x value   (if value > 0 and bid >= the hint's reserve)

In a first-price auction a rational searcher bids below its value (bid shading); how far below
depends on how much competition it expects. --bid-fraction stands in for that choice.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

from ..auctioneer.detector import estimate_created_arb
from ..auctioneer.models import bid_message
from ..common.chain import Chain, ChainError


def http(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", default="http://127.0.0.1:8600")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--router", required=True)
    ap.add_argument("--executor", required=True)
    ap.add_argument("--weth", default="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
    ap.add_argument("--bid-fraction", type=float, default=0.8)
    ap.add_argument("--gas-units", type=int, default=390_000, help="own estimate of settle() gas")
    ap.add_argument("--tip-wei", type=int, default=1_000_000_000)
    ap.add_argument("--name", default="searcher")
    ap.add_argument("--max-orders", type=int, default=0, help="exit after this many auctions (0 = run forever)")
    args = ap.parse_args()

    pk = os.environ.get("SEARCHER_PK")
    if not pk:
        sys.exit("set SEARCHER_PK")
    chain = Chain(args.rpc)
    me = chain.address_of(pk)
    log = lambda m: print(f"[{args.name}] {m}", flush=True)  # noqa: E731
    log(f"address {me}, executor {args.executor}, bid fraction {args.bid_fraction}")

    seen = 0
    pending: dict[str, float] = {}  # order_id -> close_at
    handled = 0
    while args.max_orders == 0 or handled < args.max_orders:
        for h in http("GET", f"{args.service}/hints?after={seen}"):
            seen = max(seen, h["seq"])
            try:
                bid_on(h, args, chain, pk, me, log)
                pending[h["order_id"]] = h["close_at"]
            except ChainError as e:
                log(f"{h['order_id']}: could not value: {e}")
        for oid, close_at in list(pending.items()):
            if time.time() < close_at + 0.5:
                continue
            res = http("GET", f"{args.service}/results/{oid}?searcher={me}")
            if res.get("status") in ("open", "closed", "received"):
                continue  # still simulating / awarding
            del pending[oid]
            handled += 1
            if res.get("status") == "won":
                settle(res, chain, pk, args.router, log, oid)
            else:
                log(f"{oid}: {res.get('status')}")
        time.sleep(0.3)


def bid_on(h: dict, args, chain: Chain, pk: str, me: str, log) -> None:
    pool, partner = h["pool"], h["partner_hint"]
    reserves = {p: chain.reserves(p) for p in (pool, partner)}
    order_like = {"pool": pool, "tokenIn": h["tokenIn"], "amountIn": h["amountIn"]}
    profit, _ = estimate_created_arb(order_like, reserves, [partner], args.weth)
    cost = args.gas_units * (chain.base_fee() + args.tip_wei)
    value = profit - cost
    amount = int(value * args.bid_fraction)
    if value <= 0 or amount < int(h["reserve"]):
        log(f"{h['order_id']}: pass (value {value / 1e18:.5f} ETH, reserve {int(h['reserve']) / 1e18:.5f})")
        return
    data = chain.abi_encode("f(address,address)", pool, partner)
    sig = chain.sign_message(pk, bid_message(h["order_id"], args.executor, amount, data))
    r = http("POST", f"{args.service}/bids", {
        "order_id": h["order_id"], "searcher": me, "executor": args.executor,
        "amount": str(amount), "backrun_data": data, "signature": sig,
    })
    log(f"{h['order_id']}: bid {amount / 1e18:.5f} ETH (value {value / 1e18:.5f}) -> {r.get('detail')}")


def settle(res: dict, chain: Chain, pk: str, router: str, log, oid: str) -> None:
    try:
        receipt = chain.settle(pk, router, res["order"], res["user_signature"],
                               res["result"], res["auctioneer_signature"], res["backrun_data"])
    except ChainError as e:
        log(f"{oid}: WON but settle failed: {e}")
        return
    log(f"{oid}: WON at {int(res['result']['bid']) / 1e18:.5f} ETH; settle tx {receipt['transactionHash']} "
        f"gasUsed {int(str(receipt['gasUsed']), 0)}")


if __name__ == "__main__":
    main()
