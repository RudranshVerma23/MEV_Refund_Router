// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {console} from "forge-std/Test.sol";
import {ReplayHarness} from "./ReplayHarness.sol";
import {MockERC20, MockUniswapV2Pair} from "../mocks/Mocks.sol";

/// @notice Runs the replay harness on mock pools, so the fork backtest's measuring logic is
///         tested without an RPC. Same setup as the worked example (A: 100 WETH / 300k USDC,
///         B: 1,000 WETH / 3M USDC).
contract ReplayHarnessMockTest is ReplayHarness {
    MockERC20 usdc;
    MockUniswapV2Pair poolA;
    MockUniswapV2Pair poolB;

    function setUp() public {
        MockERC20 w = new MockERC20("Wrapped Ether", "WETH", 18);
        usdc = new MockERC20("USD Coin", "USDC", 6);
        poolA = _pool(w, 100 ether, 300_000e6);
        poolB = _pool(w, 1_000 ether, 3_000_000e6);
        _deployStack(address(w), 100 ether);
    }

    /// User sells 10 WETH in A: same numbers as BackrunRouterTest's happy path.
    function test_replay_sellWeth() public {
        ReplayResult memory res = _replay(
            ReplayInput({
                pool: address(poolA),
                partnerPool: address(poolB),
                tokenIn: address(weth),
                tokenOut: address(usdc),
                amountIn: 10 ether
            })
        );
        _log(res);
        assertEq(res.routerOut, res.plainOut, "router output differs from plain swap");
        assertEq(res.arbBefore, 0, "pools started aligned");
        assertEq(res.arbProfit, 781513749829168636, "matches the brute-force optimum");
        assertEq(res.bid, res.arbProfit * 9 / 10);
        assertEq(res.refund, res.bid * 9 / 10);
        assertGt(res.gasFill, res.gasPlainSwap, "router must cost more than a bare pair swap");
        assertGt(res.gasSettle, res.gasFill, "backrun adds gas");
    }

    /// User buys WETH with USDC in A: arbitrage runs in the opposite direction.
    function test_replay_buyWeth() public {
        ReplayResult memory res = _replay(
            ReplayInput({
                pool: address(poolA),
                partnerPool: address(poolB),
                tokenIn: address(usdc),
                tokenOut: address(weth),
                amountIn: 30_000e6
            })
        );
        _log(res);
        assertEq(res.routerOut, res.plainOut);
        assertGt(res.arbProfit, 0, "buying WETH in A should also create an arbitrage");
        assertGt(res.refund, 0);
    }

    /// A tiny swap: the arbitrage doesn't beat the 0.3% fees, so there's nothing to auction.
    function test_replay_tinySwap_noArb() public {
        ReplayResult memory res = _replay(
            ReplayInput({
                pool: address(poolA),
                partnerPool: address(poolB),
                tokenIn: address(weth),
                tokenOut: address(usdc),
                amountIn: 0.01 ether
            })
        );
        assertEq(res.arbProfit, 0);
        assertEq(res.gasSettle, 0, "no measured settle without profit");
        assertEq(res.routerOut, res.plainOut);
    }

    function _pool(MockERC20 w, uint256 wethAmt, uint256 usdcAmt) internal returns (MockUniswapV2Pair p) {
        p = new MockUniswapV2Pair(address(w), address(usdc));
        w.mint(address(p), wethAmt);
        usdc.mint(address(p), usdcAmt);
        p.sync();
    }

    function _log(ReplayResult memory res) internal pure {
        console.log("plainOut / routerOut :", res.plainOut, res.routerOut);
        console.log("arb profit (wei)     :", res.arbProfit);
        console.log("bid / refund (wei)   :", res.bid, res.refund);
        console.log("gas plain/fill/settle:", res.gasPlainSwap, res.gasFill, res.gasSettle);
    }
}
