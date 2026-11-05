"""End-to-end demo on a local fork of mainnet: real historical swap -> auction -> settle -> refund.

Run (from the project root; needs ETH_RPC_URL in .env and Foundry installed):
    python -m offchain.demo.run_demo                 # replays the largest WETH-in swap the backtest auctioned
    python -m offchain.demo.run_demo --block 20114510 --amount-in 366200000000000000000

What happens:
  1. anvil forks mainnet at the END of the block before the historical swap;
  2. the router and one ReferenceArbExecutor per searcher are deployed (script/DeployLocal.s.sol);
  3. the auctioneer service and three searcher bots start as separate processes; the bots shade
     their bids differently (50%, 80%, 95% of their estimated value);
  4. a test user makes the same swap as the historical trader (same pool, direction, amount),
     signs it as an Order and sends it to the auctioneer;
  5. the auctioneer validates, detects the arbitrage, runs the auction and awards it; the winner
     submits settle(); the script then checks every balance on-chain;
  6. a second, small order shows the pre-screen rejecting a swap that creates no MEV.

Accounts are anvil's default dev accounts (publicly known keys, valid only on this local fork).
Limitation: earlier transactions in the historical block are not replayed here (the backtest's
fork test does replay the ones touching these pools), so numbers can differ slightly from it.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

from ..common.chain import Chain, ChainError

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOGS = os.path.join(ROOT, "offchain", "logs")
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
UNI = "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc"
SUSHI = "0x397FF1542f962076d0BFE58eA045FfA2d347ACa0"
LOCAL = "http://127.0.0.1:8545"
SERVICE = "http://127.0.0.1:8600"

# anvil's default dev keys (public; local fork only)
DEV_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",  # 0 deployer / owner / treasury
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",  # 1 auctioneer
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",  # 2 user
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",  # 3 searcher alice
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",  # 4 searcher bob
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",  # 5 searcher carol
]
SEARCHERS = [("alice", 3, 0.50), ("bob", 4, 0.80), ("carol", 5, 0.95)]


def load_env() -> None:
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                if v:
                    os.environ.setdefault(k, v)


def http(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def pick_swap() -> tuple[int, int]:
    """Largest WETH-in swap the backtest auctioned (from backtest/results/rows.csv)."""
    path = os.path.join(ROOT, "backtest", "results", "rows.csv")
    rows = [r for r in csv.DictReader(open(path)) if r["auctioned"] == "True" and r["token_in"].lower() == WETH.lower()]
    best = max(rows, key=lambda r: float(r["net"]))
    return int(best["block"]), int(best["amount_in"])


def wait_for(fn, timeout: float, every: float = 0.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(every)
    raise TimeoutError


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", type=int, help="block of the historical swap")
    ap.add_argument("--amount-in", type=int, help="WETH amount of the historical swap (wei)")
    ap.add_argument("--window", type=float, default=4.0)
    args = ap.parse_args()
    if not os.environ.get("ETH_RPC_URL"):
        sys.exit("set ETH_RPC_URL in .env")
    block, amount_in = (args.block, args.amount_in) if args.block else pick_swap()
    os.makedirs(LOGS, exist_ok=True)
    procs: list[subprocess.Popen] = []
    say = lambda m: print(f"[demo] {m}", flush=True)  # noqa: E731

    try:
        # 1. fork
        say(f"forking mainnet at block {block - 1} (historical swap: {amount_in / 1e18:.2f} WETH -> USDC in block {block})")
        anvil_log = open(os.path.join(LOGS, "anvil.log"), "w")
        procs.append(subprocess.Popen(["anvil", "--fork-url", os.environ["ETH_RPC_URL"], "--fork-block-number",
                                       str(block - 1), "--port", "8545"], stdout=anvil_log, stderr=subprocess.STDOUT))
        chain = Chain(LOCAL)
        wait_for(lambda: chain.block_number() >= block - 1, 120)
        addr = [chain.address_of(k) for k in DEV_KEYS]

        # 2. deploy
        env = dict(os.environ, AUCTIONEER=addr[1], TREASURY=addr[0],
                   SEARCHERS=",".join(addr[i] for _, i, _ in SEARCHERS))
        subprocess.run(["forge", "script", "script/DeployLocal.s.sol", "--rpc-url", LOCAL, "--broadcast",
                        "--private-key", DEV_KEYS[0], "--slow"], cwd=ROOT, env=env, check=True, capture_output=True)
        dep = json.load(open(os.path.join(ROOT, "deployments", "local.json")))
        router, executors = dep["router"], dep["executors"]
        say(f"router {router}; executors {', '.join(executors)}")

        # fund searchers' executors (inventory) and the user
        for (_, i, _), ex in zip(SEARCHERS, executors):
            chain.send(DEV_KEYS[i], WETH, "deposit()", value="500ether")
            chain.send(DEV_KEYS[i], WETH, "transfer(address,uint256)", ex, str(500 * 10**18))
        user, upk = addr[2], DEV_KEYS[2]
        small = 10**18
        chain.send(upk, WETH, "deposit()", value=str(amount_in + small))
        chain.send(upk, WETH, "approve(address,uint256)", router, str(amount_in + small))

        # 3. service + bots
        svc_env = dict(os.environ, AUCTIONEER_PK=DEV_KEYS[1])
        audit = os.path.join(LOGS, "auctions.jsonl")
        if os.path.exists(audit):
            os.remove(audit)
        procs.append(subprocess.Popen([sys.executable, "-m", "offchain.auctioneer.service", "--rpc", LOCAL, "--router", router,
                                       "--pair", f"{UNI}:{SUSHI}", "--window", str(args.window), "--audit-log", audit],
                                      cwd=ROOT, env=svc_env, stdout=open(os.path.join(LOGS, "service.log"), "w"), stderr=subprocess.STDOUT))
        wait_for(lambda: http("GET", f"{SERVICE}/hints") is not None, 30)
        bot_logs = {}
        for (name, i, frac), ex in zip(SEARCHERS, executors):
            bot_logs[name] = os.path.join(LOGS, f"bot_{name}.log")
            procs.append(subprocess.Popen([sys.executable, "-m", "offchain.searcher.bot", "--service", SERVICE, "--rpc", LOCAL,
                                           "--router", router, "--executor", ex, "--bid-fraction", str(frac), "--name", name,
                                           "--max-orders", "1"],
                                          cwd=ROOT, env=dict(os.environ, SEARCHER_PK=DEV_KEYS[i]),
                                          stdout=open(bot_logs[name], "w"), stderr=subprocess.STDOUT))
        time.sleep(2)

        # 4. the user's order (same swap as the historical trader)
        t0, t1, r0, r1 = chain.reserves(UNI)
        r_weth, r_usdc = (r0, r1) if t0.lower() == WETH.lower() else (r1, r0)
        quote = amount_in * 997 * r_usdc // (r_weth * 1000 + amount_in * 997)
        ts = chain.block_field("timestamp")
        order = {"user": user, "tokenIn": WETH, "tokenOut": USDC, "pool": UNI, "amountIn": str(amount_in),
                 "minOut": str(quote * 995 // 1000), "deadline": str(ts + 3600), "nonce": "1", "refundRecipient": user}
        sig = chain.sign_digest(upk, chain.hash_order(router, order))
        oid = http("POST", f"{SERVICE}/orders", {"order": order, "signature": sig})["order_id"]
        say(f"order {oid} submitted; quote {quote / 1e6:,.2f} USDC; waiting for auction and settlement...")

        terminal = ("settled", "invalid", "not_auctionable", "no_winner", "error", "filled")
        status = wait_for(lambda: (lambda s: s if s["status"] in terminal else None)(http("GET", f"{SERVICE}/orders/{oid}")),
                          timeout=1200, every=2)
        say(f"order {oid}: {status['status']} {status.get('detail', '')[:200]}")

        # 5. verify on-chain
        usdc_out = chain.balance_of(USDC, user)
        refund = chain.balance_of(WETH, user) - small  # user wrapped amount_in + small; amount_in was spent
        treasury_weth = chain.balance_of(WETH, addr[0])
        records = [json.loads(l) for l in open(audit)]
        rec = next(r for r in records if r["order_id"] == oid)
        settle_gas = winner_name = settle_tx = None
        for name, path in bot_logs.items():
            m = re.search(r"WON at ([0-9.]+) ETH; settle tx (0x[0-9a-f]+) gasUsed (\d+)", open(path).read())
            if m:
                settle_gas = int(m.group(3))
                winner_name, settle_tx = name, m.group(2)

        # 6. a small order the pre-screen should reject
        order2 = dict(order, amountIn=str(small), minOut="0", nonce="2")
        sig2 = chain.sign_digest(upk, chain.hash_order(router, order2))
        oid2 = http("POST", f"{SERVICE}/orders", {"order": order2, "signature": sig2})["order_id"]
        status2 = wait_for(lambda: (lambda s: s if s["status"] in terminal else None)(http("GET", f"{SERVICE}/orders/{oid2}")),
                           timeout=600, every=2)

        summary = {
            "historical_block": block, "amount_in_weth": amount_in / 1e18,
            "status": status["status"], "est_net_profit_eth": int(status["est_net"]) / 1e18,
            "reserve_eth": int(status["reserve"]) / 1e18,
            "bids": {b["searcher"]: int(b["amount"]) / 1e18 for b in rec["bids"]},
            "winner": status.get("winner"), "winner_name": winner_name,
            "price_eth": int(status.get("price", 0)) / 1e18,
            "user_usdc_out": usdc_out / 1e6, "quote_usdc": quote / 1e6, "user_refund_weth": refund / 1e18,
            "treasury_weth": treasury_weth / 1e18, "settle_gas_used": settle_gas,
            "settle_tx": settle_tx,
            "small_order_status": status2["status"], "small_order_detail": status2.get("detail"),
        }
        json.dump(summary, open(os.path.join(LOGS, "demo_result.json"), "w"), indent=2)
        say("RESULT")
        for k, v in summary.items():
            print(f"  {k:<22} {v}")
        ok = status["status"] == "settled" and usdc_out >= int(order["minOut"]) and refund > 0
        say("PASS" if ok else "CHECK FAILED")
        sys.exit(0 if ok else 1)
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
