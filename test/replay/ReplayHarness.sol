// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BackrunRouter} from "../../src/BackrunRouter.sol";
import {ReferenceArbExecutor} from "../../src/executors/ReferenceArbExecutor.sol";
import {IUniswapV2Pair} from "../../src/interfaces/IUniswapV2Pair.sol";
import {UniV2Math} from "../../src/libraries/UniV2Math.sol";

/// @notice Counterfactual replay of ONE swap through the router, on whatever chain state is
///         active (a mainnet fork in SettleReplay.t.sol, mock pools in ReplayHarnessMock.t.sol).
///
/// From the same starting state, run four variants and roll back after each one:
///   1. plain swap  : user sends tokens to the pair and calls pair.swap (baseline, no router)
///   2. fill        : the same order through BackrunRouter.fill (no backrun): router overhead
///   3. probe       : settle with bid = 0, to measure the arbitrage profit the swap created
///   4. measured    : settle with bid = BID_SHARE of that profit: gas and refund of the full path
///
/// Gas: execution gas measured with gasleft() around the call. It EXCLUDES the 21,000 intrinsic
/// gas and calldata cost of a real transaction; backtest/analyze.py adds those. Before each
/// variant every touched account is marked cold (vm.cool), so warm-storage discounts from
/// earlier variants don't make later ones look cheaper.
abstract contract ReplayHarness is Test {
    struct ReplayInput {
        address pool; // the pool the user swaps in
        address partnerPool; // the other pool for the same pair: where the arbitrage closes
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
    }

    struct ReplayResult {
        uint256 plainOut; // user output, plain swap
        uint256 routerOut; // user output via the router (must equal plainOut)
        uint256 arbBefore; // arbitrage that already existed BEFORE the swap (not created by the user)
        uint256 arbProfit; // executor profit when backrunning the swap (includes arbBefore)
        uint256 bid; // bid used in the measured run
        uint256 refund; // refund paid to the user in the measured run
        uint256 gasPlainSwap;
        uint256 gasFill;
        uint256 gasSettle; // 0 if there was no profitable arbitrage
    }

    uint256 internal constant USER_PK = 0xA11CE;
    uint256 internal constant AUCTIONEER_PK = 0xA0C7;
    uint16 internal constant REFUND_BPS = 9000;
    uint256 internal constant BID_SHARE_BPS = 9000; // measured run bids 90% of the probe profit
    uint32 internal constant BACKRUN_GAS_CAP = 1_000_000;

    BackrunRouter internal router;
    ReferenceArbExecutor internal executor;
    IERC20 internal weth;
    address internal searcher = makeAddr("searcher");
    address internal treasury = makeAddr("treasury");

    function _deployStack(address _weth, uint256 executorInventory) internal {
        weth = IERC20(_weth);
        router = new BackrunRouter(weth, vm.addr(AUCTIONEER_PK), treasury, REFUND_BPS, BACKRUN_GAS_CAP, address(this));
        executor = new ReferenceArbExecutor(address(router), weth, searcher);
        deal(_weth, address(executor), executorInventory);
    }

    function _replay(ReplayInput memory r) internal returns (ReplayResult memory res) {
        address user = vm.addr(USER_PK);
        deal(r.tokenIn, user, r.amountIn);
        vm.prank(user);
        IERC20(r.tokenIn).approve(address(router), r.amountIn);

        res.arbBefore = _bestArb(r.pool, r.partnerPool);
        uint256 snap = vm.snapshotState();

        // 1. plain swap (baseline)
        (res.plainOut, res.gasPlainSwap) = _plainSwap(user, r);
        vm.revertToState(snap);

        // 2. router fill (no backrun)
        BackrunRouter.Order memory o = _order(user, r, res.plainOut);
        bytes memory userSig = _sign(USER_PK, router.hashOrder(o));
        _coolAll(r, user);
        vm.prank(vm.addr(AUCTIONEER_PK));
        uint256 g = gasleft();
        res.routerOut = router.fill(o, userSig);
        res.gasFill = g - gasleft();
        vm.revertToState(snap);

        // 3. probe: bid 0, measure the profit
        bytes memory data = abi.encode(r.pool, r.partnerPool);
        uint256 inventory = weth.balanceOf(address(executor));
        _settle(o, userSig, 0, data);
        res.arbProfit = weth.balanceOf(address(executor)) - inventory;
        vm.revertToState(snap);

        // 4. measured run (only if there is something to bid)
        if (res.arbProfit > 0) {
            res.bid = (res.arbProfit * BID_SHARE_BPS) / 10_000;
            _coolAll(r, user);
            (res.gasSettle, res.refund) = _settle(o, userSig, res.bid, data);
            vm.revertToState(snap);
        }
    }

    // -------------------------------------------------------------------------------------------

    function _plainSwap(address user, ReplayInput memory r) internal returns (uint256 out, uint256 gasUsed) {
        (uint256 amount0Out, uint256 amount1Out) = _swapOutputs(r);
        uint256 balBefore = IERC20(r.tokenOut).balanceOf(user);

        _coolAll(r, user);
        vm.startPrank(user);
        uint256 g = gasleft();
        IERC20(r.tokenIn).transfer(r.pool, r.amountIn);
        IUniswapV2Pair(r.pool).swap(amount0Out, amount1Out, user, "");
        gasUsed = g - gasleft();
        vm.stopPrank();
        out = IERC20(r.tokenOut).balanceOf(user) - balBefore;
    }

    /// @dev The (amount0Out, amount1Out) arguments of pair.swap for selling amountIn of tokenIn.
    function _swapOutputs(ReplayInput memory r) internal view returns (uint256, uint256) {
        IUniswapV2Pair pair = IUniswapV2Pair(r.pool);
        bool zeroForOne = pair.token0() == r.tokenIn;
        (uint112 r0, uint112 r1,) = pair.getReserves();
        (uint256 rIn, uint256 rOut) = zeroForOne ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
        uint256 quote = UniV2Math.getAmountOut(r.amountIn, rIn, rOut);
        return zeroForOne ? (uint256(0), quote) : (quote, uint256(0));
    }

    function _settle(BackrunRouter.Order memory o, bytes memory userSig, uint256 bid, bytes memory data)
        internal
        returns (uint256 gasUsed, uint256 refund)
    {
        BackrunRouter.AuctionResult memory a = BackrunRouter.AuctionResult({
            orderHash: router.hashOrder(o),
            winner: searcher,
            executor: address(executor),
            bid: bid,
            validUntilBlock: block.number + 2
        });
        bytes memory aucSig = _sign(AUCTIONEER_PK, router.hashAuctionResult(a));
        vm.prank(searcher);
        uint256 g = gasleft();
        (, refund) = router.settle(o, userSig, a, aucSig, data);
        gasUsed = g - gasleft();
    }

    function _order(address user, ReplayInput memory r, uint256 minOut)
        internal
        view
        returns (BackrunRouter.Order memory)
    {
        return BackrunRouter.Order({
            user: user,
            tokenIn: r.tokenIn,
            tokenOut: r.tokenOut,
            pool: r.pool,
            amountIn: r.amountIn,
            minOut: minOut, // exactly the plain-swap output: the router must not do worse
            deadline: block.timestamp + 600,
            nonce: 1,
            refundRecipient: user
        });
    }

    function _sign(uint256 pk, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 rr, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(rr, s, v);
    }

    function _coolAll(ReplayInput memory r, address user) internal {
        vm.cool(address(router));
        vm.cool(address(executor));
        vm.cool(r.pool);
        vm.cool(r.partnerPool);
        vm.cool(r.tokenIn);
        vm.cool(r.tokenOut);
        vm.cool(address(weth));
        vm.cool(user);
        vm.cool(treasury);
    }

    /// @dev Best existing WETH cycle between the two pools (either direction), in WETH.
    function _bestArb(address p1, address p2) internal view returns (uint256) {
        return _max(_cycleProfit(p1, p2), _cycleProfit(p2, p1));
    }

    function _cycleProfit(address pool1, address pool2) internal view returns (uint256) {
        (uint256 x1, uint256 y1) = _wethReserves(pool1);
        (uint256 x2, uint256 y2) = _wethReserves(pool2);
        uint256 dx = UniV2Math.optimalArbInput(x1, y1, y2, x2);
        if (dx == 0) return 0;
        int256 p = UniV2Math.cycleProfit(dx, x1, y1, y2, x2);
        return p > 0 ? uint256(p) : 0;
    }

    function _wethReserves(address p) internal view returns (uint256 rWeth, uint256 rOther) {
        (uint112 r0, uint112 r1,) = IUniswapV2Pair(p).getReserves();
        (rWeth, rOther) =
            IUniswapV2Pair(p).token0() == address(weth) ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
    }

    function _max(uint256 a, uint256 b) internal pure returns (uint256) {
        return a > b ? a : b;
    }
}
