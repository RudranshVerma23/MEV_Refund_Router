// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {console} from "forge-std/Test.sol";
import {IUniswapV2Pair} from "../../src/interfaces/IUniswapV2Pair.sol";
import {ReplayHarness} from "../replay/ReplayHarness.sol";

/// @notice Backtest: replay ONE historical Uniswap V2 swap through the router on forked mainnet.
///         Skipped unless ETH_RPC_URL and TX_HASH are set. backtest/run_fork_replays.py calls it
///         once per swap, passing the swap's amounts from backtest/data/swaps.csv.
///
///   ETH_RPC_URL=<archive rpc> TX_HASH=0x.. AMOUNT0_IN=.. AMOUNT1_IN=.. \
///     forge test --match-path test/fork/SettleReplay.t.sol -vv
///
/// Counterfactual: rebuild the pools' state just before the swap, then, instead of the historical
/// tx, execute the same swap (same pool, direction and amount) by a test user through
/// BackrunRouter and measure it with the harness.
///
/// Rebuilding the state, fast mode (used by run_fork_replays.py): set FORK_BLOCK = block - 1,
/// BLOCK_NUMBER, BASE_FEE, and PRIOR_TXS = comma-separated earlier txs of the same block that
/// touched either pool (from their logs). The test forks at the end of the previous block,
/// replays only those txs, then sets the block number and base fee. Pool state is exact; other
/// accounts may differ, which doesn't affect this measurement. Takes seconds.
///
/// Exact mode (FORK_BLOCK unset): createSelectFork(rpc, txHash) replays EVERY earlier tx of the
/// block. It is slow (minutes over a public RPC) and can fail on old blocks, because Foundry
/// re-executes them with current fork rules (e.g. Pectra's calldata gas floor).
///
/// Appends to backtest/results/settle_results.csv (no header; column order = `_csvLine`):
///   txHash, block, baseFee, tokenIn, amountIn, plainOut, routerOut, arbBefore, arbProfit,
///   bid, refund, gasPlainSwap, gasFill, gasSettle, otherPerWeth_e18
contract SettleReplayTest is ReplayHarness {
    address constant WETH = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;
    address constant UNI_V2_USDC_WETH = 0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc;
    address constant SUSHI_USDC_WETH = 0x397FF1542f962076d0BFE58eA045FfA2d347ACa0;
    string constant OUT = "backtest/results/settle_results.csv";

    function test_settleReplay() public {
        string memory rpc = vm.envOr("ETH_RPC_URL", string(""));
        bytes32 txHash = vm.envOr("TX_HASH", bytes32(0));
        if (bytes(rpc).length == 0 || txHash == bytes32(0)) {
            vm.skip(true);
            return;
        }
        address pool = vm.envOr("POOL", UNI_V2_USDC_WETH);
        address partner = vm.envOr("PARTNER_POOL", SUSHI_USDC_WETH);
        uint256 amount0In = vm.envOr("AMOUNT0_IN", uint256(0));
        uint256 amount1In = vm.envOr("AMOUNT1_IN", uint256(0));
        require((amount0In == 0) != (amount1In == 0), "exactly one of AMOUNT0_IN / AMOUNT1_IN must be set");

        _forkJustBefore(rpc, txHash);
        _deployStack(WETH, 10_000 ether); // searcher inventory: large enough to never cap the arbitrage

        address t0 = IUniswapV2Pair(pool).token0();
        address t1 = IUniswapV2Pair(pool).token1();
        ReplayInput memory r = ReplayInput({
            pool: pool,
            partnerPool: partner,
            tokenIn: amount0In > 0 ? t0 : t1,
            tokenOut: amount0In > 0 ? t1 : t0,
            amountIn: amount0In > 0 ? amount0In : amount1In
        });
        (uint256 rWeth, uint256 rOther) = _wethReserves(pool);
        uint256 otherPerWeth = rOther * 1e18 / rWeth; // e.g. USDC (6 dp) per 1 WETH, scaled 1e18

        ReplayResult memory res = _replay(r);

        console.log("block                :", block.number);
        console.log("arb before / profit  :", res.arbBefore, res.arbProfit);
        console.log("bid / refund (wei)   :", res.bid, res.refund);
        console.log("gas plain/fill/settle:", res.gasPlainSwap, res.gasFill, res.gasSettle);
        require(res.routerOut == res.plainOut, "router output differs from plain swap");

        vm.writeLine(OUT, _csvLine(txHash, r, res, otherPerWeth));
    }

    function _forkJustBefore(string memory rpc, bytes32 txHash) internal {
        uint256 forkBlock = vm.envOr("FORK_BLOCK", uint256(0));
        if (forkBlock == 0) {
            vm.createSelectFork(rpc, txHash); // exact mode
            return;
        }
        vm.createSelectFork(rpc, forkBlock); // end of the previous block
        bytes32[] memory prior = vm.envOr("PRIOR_TXS", ",", new bytes32[](0));
        for (uint256 i = 0; i < prior.length; i++) {
            vm.transact(prior[i]); // earlier same-block txs that touched our pools
        }
        vm.roll(vm.envUint("BLOCK_NUMBER"));
        vm.fee(vm.envUint("BASE_FEE"));
    }

    function _csvLine(bytes32 txHash, ReplayInput memory r, ReplayResult memory res, uint256 otherPerWeth)
        internal
        view
        returns (string memory)
    {
        string memory a = string.concat(
            vm.toString(txHash),
            ",",
            vm.toString(block.number),
            ",",
            vm.toString(block.basefee),
            ",",
            vm.toString(r.tokenIn),
            ",",
            vm.toString(r.amountIn),
            ",",
            vm.toString(res.plainOut),
            ",",
            vm.toString(res.routerOut),
            ","
        );
        string memory b = string.concat(
            vm.toString(res.arbBefore),
            ",",
            vm.toString(res.arbProfit),
            ",",
            vm.toString(res.bid),
            ",",
            vm.toString(res.refund),
            ",",
            vm.toString(res.gasPlainSwap),
            ",",
            vm.toString(res.gasFill),
            ",",
            vm.toString(res.gasSettle),
            ",",
            vm.toString(otherPerWeth)
        );
        return string.concat(a, b);
    }
}
