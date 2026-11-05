// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";

import {BackrunRouter} from "../src/BackrunRouter.sol";
import {ReferenceArbExecutor} from "../src/executors/ReferenceArbExecutor.sol";
import {UniV2Math} from "../src/libraries/UniV2Math.sol";
import {MockERC20, MockUniswapV2Pair, UnderpayingExecutor, ReentrantExecutor} from "./mocks/Mocks.sol";

/// @notice Unit tests on mock pools (no RPC needed).
/// Setup mirrors the worked example: pool A is shallow (100 WETH / 300k USDC) and pool B is
/// deep (1,000 WETH / 3M USDC). The user sells 10 WETH into A, which makes WETH cheap in A
/// and creates a WETH -> USDC (B) -> WETH (A) arbitrage.
contract BackrunRouterTest is Test {
    MockERC20 weth;
    MockERC20 usdc;
    MockUniswapV2Pair poolA;
    MockUniswapV2Pair poolB;
    BackrunRouter router;
    ReferenceArbExecutor executor;

    uint256 userPk = 0xA11CE;
    uint256 auctioneerPk = 0xA0C7;
    address user;
    address auctioneer;
    address searcher = makeAddr("searcher");
    address treasury = makeAddr("treasury");
    address owner = makeAddr("owner");

    uint16 constant REFUND_BPS = 9000; // 90% to the user, like MEV-Share's default
    uint256 constant AMOUNT_IN = 10 ether;
    uint256 constant BID = 0.3 ether;

    function setUp() public {
        user = vm.addr(userPk);
        auctioneer = vm.addr(auctioneerPk);

        weth = new MockERC20("Wrapped Ether", "WETH", 18);
        usdc = new MockERC20("USD Coin", "USDC", 6);

        poolA = _makePool(100 ether, 300_000e6);
        poolB = _makePool(1_000 ether, 3_000_000e6);

        router = new BackrunRouter(IERC20(address(weth)), auctioneer, treasury, REFUND_BPS, 500_000, owner);
        executor = new ReferenceArbExecutor(address(router), IERC20(address(weth)), searcher);
        weth.mint(address(executor), 100 ether); // searcher's inventory

        weth.mint(user, AMOUNT_IN);
        vm.prank(user);
        weth.approve(address(router), AMOUNT_IN); // exact approval, not unlimited
    }

    // ------------------------------------------------------------------ happy path

    function test_settle_swapsBackrunsAndRefunds() public {
        BackrunRouter.Order memory o = _order(1);
        BackrunRouter.AuctionResult memory a = _auction(o, searcher, address(executor), BID);
        uint256 searcherInvBefore = weth.balanceOf(address(executor));

        (uint256 out, uint256 refund) =
            _settle(searcher, o, a, auctioneerPk, abi.encode(address(poolA), address(poolB)));

        assertGe(out, o.minOut, "user got less than minOut");
        assertEq(usdc.balanceOf(user), out, "user output");
        assertEq(refund, BID * REFUND_BPS / 10_000, "refund amount");
        assertEq(weth.balanceOf(user), refund, "user refund received");
        assertEq(weth.balanceOf(treasury), BID - refund, "treasury share");
        assertEq(weth.balanceOf(address(router)), 0, "router keeps nothing");
        assertEq(usdc.balanceOf(address(router)), 0, "router keeps nothing");
        assertTrue(router.nonceUsed(user, 1));

        uint256 searcherProfit = weth.balanceOf(address(executor)) - searcherInvBefore;
        console.log("arb gross profit (WETH wei):", searcherProfit + BID);
        console.log("searcher net after bid      :", searcherProfit);
        console.log("refund to user              :", refund);

        // After the backrun the two pools should be priced (almost) the same again. They can differ
        // by up to the two 0.3% fees (~0.6%): past that point another arbitrage is no longer profitable.
        uint256 pA = _priceUsdcPerWeth(poolA);
        uint256 pB = _priceUsdcPerWeth(poolB);
        assertApproxEqRel(pA, pB, 0.01e18, "pools not realigned"); // within 1%
    }

    // ------------------------------------------------------------------ failure modes

    function test_revert_replay() public {
        BackrunRouter.Order memory o = _order(1);
        BackrunRouter.AuctionResult memory a = _auction(o, searcher, address(executor), BID);
        bytes memory us = _signOrder(o);
        bytes memory as_ = _signAuction(a, auctioneerPk);
        bytes memory data = abi.encode(address(poolA), address(poolB));

        weth.mint(user, AMOUNT_IN);
        vm.prank(user);
        weth.approve(address(router), 2 * AMOUNT_IN);

        vm.prank(searcher);
        router.settle(o, us, a, as_, data);
        vm.prank(searcher);
        vm.expectRevert(BackrunRouter.NonceAlreadyUsed.selector);
        router.settle(o, us, a, as_, data);
    }

    function test_revert_notWinner() public {
        BackrunRouter.Order memory o = _order(1);
        BackrunRouter.AuctionResult memory a = _auction(o, searcher, address(executor), BID);
        address copycat = makeAddr("copycat");
        _settleExpectRevert(
            copycat,
            o,
            a,
            auctioneerPk,
            abi.encode(address(poolA), address(poolB)),
            abi.encodePacked(BackrunRouter.NotWinner.selector)
        );
    }

    function test_revert_fakeAuctioneer() public {
        BackrunRouter.Order memory o = _order(1);
        BackrunRouter.AuctionResult memory a = _auction(o, searcher, address(executor), BID);
        _settleExpectRevert(
            searcher,
            o,
            a,
            0xBAD,
            abi.encode(address(poolA), address(poolB)),
            abi.encodePacked(BackrunRouter.BadAuctioneerSignature.selector)
        );
    }

    function test_revert_auctionExpired() public {
        BackrunRouter.Order memory o = _order(1);
        BackrunRouter.AuctionResult memory a = _auction(o, searcher, address(executor), BID);
        vm.roll(a.validUntilBlock + 1);
        _settleExpectRevert(
            searcher,
            o,
            a,
            auctioneerPk,
            abi.encode(address(poolA), address(poolB)),
            abi.encodePacked(BackrunRouter.Expired.selector)
        );
    }

    /// The winner's executor can't make the promised profit, so the whole tx reverts and the user is untouched.
    function test_revert_bidAboveProfit_userUntouched() public {
        BackrunRouter.Order memory o = _order(1);
        BackrunRouter.AuctionResult memory a = _auction(o, searcher, address(executor), 50 ether);
        _settleExpectRevert(searcher, o, a, auctioneerPk, abi.encode(address(poolA), address(poolB)), "");
        assertEq(weth.balanceOf(user), AMOUNT_IN, "user lost funds");
        assertFalse(router.nonceUsed(user, 1), "order consumed despite revert");
    }

    function test_revert_underpaid() public {
        UnderpayingExecutor bad = new UnderpayingExecutor(IERC20(address(weth)));
        weth.mint(address(bad), 1 ether);
        BackrunRouter.Order memory o = _order(1);
        BackrunRouter.AuctionResult memory a = _auction(o, searcher, address(bad), BID);
        _settleExpectRevert(
            searcher, o, a, auctioneerPk, "", abi.encodeWithSelector(BackrunRouter.Underpaid.selector, BID - 1, BID)
        );
    }

    /// An attacker wins an auction that names SOMEONE ELSE's executor. The executor must refuse.
    function test_revert_executorOwnedBySomeoneElse() public {
        address attacker = makeAddr("attacker");
        BackrunRouter.Order memory o = _order(1);
        BackrunRouter.AuctionResult memory a = _auction(o, attacker, address(executor), BID);
        _settleExpectRevert(
            attacker,
            o,
            a,
            auctioneerPk,
            abi.encode(address(poolA), address(poolB)),
            abi.encodePacked(ReferenceArbExecutor.NotMyAuction.selector)
        );
    }

    function test_revert_reentrancy() public {
        ReentrantExecutor re = new ReentrantExecutor();
        BackrunRouter.Order memory o = _order(1);
        BackrunRouter.AuctionResult memory a = _auction(o, searcher, address(re), BID);
        bytes memory us = _signOrder(o);
        bytes memory as_ = _signAuction(a, auctioneerPk);
        re.setPayload(abi.encodeCall(BackrunRouter.fill, (o, us)));
        vm.prank(searcher);
        vm.expectRevert(ReentrancyGuardTransient.ReentrancyGuardReentrantCall.selector);
        router.settle(o, us, a, as_, "");
    }

    function test_fill_onlyAuctioneerOrUser() public {
        BackrunRouter.Order memory o = _order(1);
        bytes memory us = _signOrder(o);
        vm.prank(searcher);
        vm.expectRevert(BackrunRouter.NotAuctioneerOrUser.selector);
        router.fill(o, us);

        vm.prank(auctioneer);
        uint256 out = router.fill(o, us);
        assertGe(out, o.minOut);
    }

    function test_userCanCancel() public {
        BackrunRouter.Order memory o = _order(7);
        vm.prank(user);
        router.invalidateNonce(7);
        bytes memory us = _signOrder(o);
        vm.prank(auctioneer);
        vm.expectRevert(BackrunRouter.NonceAlreadyUsed.selector);
        router.fill(o, us);
    }

    // ------------------------------------------------------------------ fuzz

    /// For any swap size, with a zero bid, the user gets exactly the V2 quote (>= minOut)
    /// and the router keeps nothing.
    function testFuzz_settle_userGetsQuote(uint256 amountIn) public {
        amountIn = bound(amountIn, 0.01 ether, 30 ether);
        weth.mint(user, amountIn);
        vm.prank(user);
        weth.approve(address(router), amountIn);

        (uint112 r0, uint112 r1,) = poolA.getReserves();
        (uint256 rIn, uint256 rOut) = poolA.token0() == address(weth) ? (r0, r1) : (r1, r0);
        uint256 quote = UniV2Math.getAmountOut(amountIn, rIn, rOut);

        BackrunRouter.Order memory o = _order(1);
        o.amountIn = amountIn;
        o.minOut = quote;
        BackrunRouter.AuctionResult memory a = _auction(o, searcher, address(executor), 0);

        (uint256 out,) = _settle(searcher, o, a, auctioneerPk, abi.encode(address(poolA), address(poolB)));
        assertEq(out, quote);
        assertEq(weth.balanceOf(address(router)), 0);
    }

    // ------------------------------------------------------------------ helpers

    /// @dev Signatures are computed BEFORE vm.prank / vm.expectRevert: both cheatcodes apply to the
    ///      next external call, and _signOrder/_signAuction call router.hashOrder externally.
    function _settle(
        address who,
        BackrunRouter.Order memory o,
        BackrunRouter.AuctionResult memory a,
        uint256 auctioneerKey,
        bytes memory data
    ) internal returns (uint256, uint256) {
        bytes memory us = _signOrder(o);
        bytes memory as_ = _signAuction(a, auctioneerKey);
        vm.prank(who);
        return router.settle(o, us, a, as_, data);
    }

    /// @dev `err` empty = expect any revert; otherwise the exact revert data.
    function _settleExpectRevert(
        address who,
        BackrunRouter.Order memory o,
        BackrunRouter.AuctionResult memory a,
        uint256 auctioneerKey,
        bytes memory data,
        bytes memory err
    ) internal {
        bytes memory us = _signOrder(o);
        bytes memory as_ = _signAuction(a, auctioneerKey);
        vm.prank(who);
        if (err.length == 0) vm.expectRevert();
        else vm.expectRevert(err);
        router.settle(o, us, a, as_, data);
    }

    function _makePool(uint256 wethAmt, uint256 usdcAmt) internal returns (MockUniswapV2Pair p) {
        p = new MockUniswapV2Pair(address(weth), address(usdc));
        weth.mint(address(p), wethAmt);
        usdc.mint(address(p), usdcAmt);
        p.sync();
    }

    function _order(uint256 nonce) internal view returns (BackrunRouter.Order memory o) {
        (uint112 r0, uint112 r1,) = poolA.getReserves();
        (uint256 rIn, uint256 rOut) = poolA.token0() == address(weth) ? (r0, r1) : (r1, r0);
        uint256 quote = UniV2Math.getAmountOut(AMOUNT_IN, rIn, rOut);
        o = BackrunRouter.Order({
            user: user,
            tokenIn: address(weth),
            tokenOut: address(usdc),
            pool: address(poolA),
            amountIn: AMOUNT_IN,
            minOut: quote * 99 / 100, // 1% slippage tolerance
            deadline: block.timestamp + 60,
            nonce: nonce,
            refundRecipient: user
        });
    }

    function _auction(BackrunRouter.Order memory o, address winner, address exec, uint256 bid)
        internal
        view
        returns (BackrunRouter.AuctionResult memory)
    {
        return BackrunRouter.AuctionResult({
            orderHash: router.hashOrder(o), winner: winner, executor: exec, bid: bid, validUntilBlock: block.number + 2
        });
    }

    function _signOrder(BackrunRouter.Order memory o) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(userPk, router.hashOrder(o));
        return abi.encodePacked(r, s, v);
    }

    function _signAuction(BackrunRouter.AuctionResult memory a, uint256 pk) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, router.hashAuctionResult(a));
        return abi.encodePacked(r, s, v);
    }

    /// USDC (6 decimals) per 1 WETH, scaled by 1e18.
    function _priceUsdcPerWeth(MockUniswapV2Pair p) internal view returns (uint256) {
        (uint112 r0, uint112 r1,) = p.getReserves();
        (uint256 rw, uint256 ru) = p.token0() == address(weth) ? (r0, r1) : (r1, r0);
        return ru * 1e18 / rw;
    }
}
