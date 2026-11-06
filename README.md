# MEV-refunding backrun router

A router that runs a user's swap and an **auctioned backrun in the same transaction**, enforces
the winner's payment on-chain and refunds part of it to the user. It is evaluated by replaying
historical swaps on forked mainnet.

## Design in one paragraph

The user signs an EIP-712 **Order** (they pay no gas). An off-chain **auctioneer** simulates it. If
the swap creates an arbitrage worth more than its gas, the auctioneer runs a **sealed-bid
first-price auction** among searchers and signs an **AuctionResult** naming the winner, their
executor and the bid. The **winner submits** `BackrunRouter.settle(...)`. In one transaction the
router:
1. checks both signatures, the nonce and the deadlines;
2. swaps for the user (output ≥ `minOut`);
3. calls the winner's executor;
4. checks that ≥ `bid` WETH arrived;
5. pays `refundBps` of it to the user and the rest to the treasury.

If anything fails, the whole transaction reverts and the user loses nothing. If nobody bids, the
auctioneer calls `fill(...)` for a plain swap.

## Layout

```
src/
  BackrunRouter.sol              settle (swap + backrun + refund), fill (fallback), admin
  executors/ReferenceArbExecutor.sol   example searcher: 2-pool WETH arbitrage, sized on-chain
  interfaces/IBackrunner.sol     what a searcher's executor implements
  interfaces/IUniswapV2Pair.sol
  libraries/UniV2Math.sol        getAmountOut + closed-form optimal arbitrage size
test/
  BackrunRouter.t.sol            unit + fuzz tests on mock pools (no RPC)
  mocks/Mocks.sol                ERC20, a real x*y=k pair with the 0.3% fee, malicious executors
  replay/ReplayHarness.sol       counterfactual replay of one swap: plain vs fill vs settle, gas + refund
  replay/ReplayHarnessMock.t.sol the harness on mock pools (runs without an RPC)
  fork/SettleReplay.t.sol        the harness on forked mainnet for one historical swap
backtest/
  rpc.py                         minimal JSON-RPC client + per-swap fork context (stdlib only)
  collect_swaps.py               pull Swap events from an archive RPC -> data/swaps_2024.csv
  sample_swaps.py                user swaps only, stratified by size, with weights -> data/sample_1000.csv
  run_fork_replays.py            run SettleReplay for each swap -> results/settle_results.csv
  analyze.py                     refund + gas statistics from measured results (bid model is an assumption)
  plots.py                       report figures (SVG) -> docs/figures/
script/
  DeployLocal.s.sol              deploy router + one executor per searcher (local fork demo)
offchain/
  common/chain.py                calls, transactions and signing via Foundry's `cast`
  auctioneer/service.py          HTTP service: validate -> detect -> sealed-bid auction -> award -> audit log
  auctioneer/detector.py         arbitrage created by an order (pre-screen against settle() gas)
  auctioneer/auction.py          first-price sealed-bid auction with reserve and simulation gating
  auctioneer/models.py           bid type + the message searchers sign
  auctioneer/v2math.py           Python mirror of UniV2Math
  searcher/bot.py                example searcher: values hints, bids a fraction, settles if it wins
  demo/run_demo.py               end-to-end run on a mainnet fork (real historical swap)
  tests/                         Python unit tests (math, auction, service against a fake chain)
docs/
  REPORT.md                      the write-up: design, results, gas incidence, incentive compatibility
  figures/                       figures used in the report
```

## Setup

1. Install Foundry (https://book.getfoundry.sh/getting-started/installation).
2. Fetch the Solidity dependencies (git submodules: forge-std v1.9.7, OpenZeppelin v5.4.0):
   ```bash
   git submodule update --init --recursive
   ```
   OpenZeppelin ≥ 5.1 is required for `ReentrancyGuardTransient`.
3. Python 3.10+ with no extra packages: the scripts use the standard library and Foundry's `cast`.
4. `cp .env.example .env` and set `ETH_RPC_URL` to an **archive** node. `https://eth.drpc.org` works
   without a key (rate-limited). For the full run, use a free keyed endpoint (Alchemy, Infura or dRPC).
   Both forge and the backtest scripts read `.env`.

## Run

```bash
forge test -vv                                     # unit, fuzz and harness tests (fork test is skipped)
python -m unittest discover -s offchain/tests -t .  # Python tests
python -m offchain.demo.run_demo                    # whole system end to end on a mainnet fork
```

The demo forks mainnet before a large historical swap, deploys the contracts, starts the
auctioneer and three searcher bots (bidding 50/80/95% of their value), submits the user's signed
order, and checks every balance on-chain. Logs and a JSON summary go to `offchain/logs/`.

Backtest (reads `ETH_RPC_URL` from `.env`):
```bash
# 1. every Uniswap V2 USDC/WETH swap in 2024 (100-block log queries, 16 in parallel)
python backtest/collect_swaps.py --from-block 18900000 --to-block 21499999 --threads 16 --out backtest/data/swaps_2024.csv
# 2. 1,000 user swaps, stratified by size, with weights back to the full year
python backtest/sample_swaps.py --swaps backtest/data/swaps_2024.csv --n 1000 --out backtest/data/sample_1000.csv
# 3. replay each through the router on a fork (resumable)
python backtest/run_fork_replays.py --swaps backtest/data/sample_1000.csv --limit 1000 --workers 8
# 4. results: overall, per size bucket, and weighted to the whole year
python backtest/analyze.py --sample backtest/data/sample_1000.csv --rows-out backtest/results/rows.csv
# 5. figures for the report
python backtest/plots.py
```

Sampling: most swaps are small and create no arbitrage above gas, so the sample is stratified
by USD size (<1k, 1k-10k, 10k-50k, 50k-250k, >=250k) with quotas weighted toward large swaps.
Each sampled swap carries a weight (how many user swaps of the year it represents), so totals
are unbiased. Only txs sent to known user routers or aggregators count as user swaps;
arbitrage bots' own swaps are excluded (`ALLOWED_TO` in `sample_swaps.py`).

## How a swap is replayed

`test/fork/SettleReplay.t.sol` forks mainnet just before a historical swap (with every earlier
transaction of that block replayed). Instead of the historical transaction, a test user makes the
same swap (same pool, direction and amount) four ways, rolling back after each:

1. plain pair swap (baseline);
2. `router.fill` (router, no backrun): gives the router overhead;
3. `router.settle` with bid 0: measures the arbitrage profit the swap created;
4. `router.settle` with bid = 90% of that profit: gas and refund of the full path.

Gas is execution gas measured around each call, with every touched account reset to cold first
(`vm.cool`). Intrinsic (21,000) and calldata gas are added in `analyze.py`.

## Security properties (and the tests that check them)

| Property | Test |
|---|---|
| User gets ≥ `minOut`; with a zero bid, exactly the V2 quote | `test_settle_swapsBackrunsAndRefunds`, `testFuzz_settle_userGetsQuote` |
| Refund = `refundBps` × payment; router keeps nothing | `test_settle_swapsBackrunsAndRefunds` |
| Order executes at most once; user can cancel | `test_revert_replay`, `test_userCanCancel` |
| Only the auction winner can settle (no copying from the mempool) | `test_revert_notWinner` |
| Auction result must be signed by the auctioneer and fresh | `test_revert_fakeAuctioneer`, `test_revert_auctionExpired` |
| Underpayment reverts everything; the user is untouched | `test_revert_underpaid`, `test_revert_bidAboveProfit_userUntouched` |
| An executor can't be used by someone else's auction | `test_revert_executorOwnedBySomeoneElse` |
| No reentrancy through the callback | `test_revert_reentrancy` |
| Plain fills only by the auctioneer or the user | `test_fill_onlyAuctioneerOrUser` |
| Router output equals a plain swap's output | `test_replay_*` (and checked on every fork replay) |

## Author
This work is done by [Rudransh Verma](https://github.com/RudranshVerma23) and [Suryansh Verma](https://github.com/sury666) for CS731 course project.