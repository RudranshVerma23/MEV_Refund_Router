// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

import {IBackrunner} from "../interfaces/IBackrunner.sol";
import {IUniswapV2Pair} from "../interfaces/IUniswapV2Pair.sol";
import {UniV2Math} from "../libraries/UniV2Math.sol";

/// @title ReferenceArbExecutor
/// @notice An example searcher used for tests and the backtest. It arbitrages two V2 pools that
///         both trade WETH against the same token, starting and ending in WETH, then pays the bid.
/// @dev It recomputes the optimal size ON-CHAIN at execution time, because reserves may have changed
///      since the auction was simulated (the "winner's curse"). It uses its own WETH inventory.
///      TODO: replace the inventory with a V2 flash swap so it needs no capital.
contract ReferenceArbExecutor is IBackrunner, Ownable {
    using SafeERC20 for IERC20;

    address public immutable router;
    IERC20 public immutable weth;

    error OnlyRouter();
    error NotMyAuction();
    error Unprofitable(uint256 profit, uint256 bid);

    constructor(address _router, IERC20 _weth, address _owner) Ownable(_owner) {
        router = _router;
        weth = _weth;
    }

    /// @param data abi.encode(address poolA, address poolB): two V2 pools for WETH/<same token>.
    function backrun(address winner, bytes32, uint256 bid, bytes calldata data) external override {
        if (msg.sender != router) revert OnlyRouter();
        // Without this check, someone could win an auction that names THIS executor and spend our inventory.
        if (winner != owner()) revert NotMyAuction();

        (address poolA, address poolB) = abi.decode(data, (address, address));
        uint256 startBalance = weth.balanceOf(address(this));

        // Try both directions (WETH in at A and out at B, or the reverse). At most one is profitable.
        uint256 dxAB = _optimalInput(poolA, poolB, startBalance);
        if (dxAB > 0) {
            _executeCycle(poolA, poolB, dxAB);
        } else {
            uint256 dxBA = _optimalInput(poolB, poolA, startBalance);
            if (dxBA > 0) _executeCycle(poolB, poolA, dxBA);
        }

        uint256 endBalance = weth.balanceOf(address(this));
        uint256 profit = endBalance > startBalance ? endBalance - startBalance : 0;
        if (profit < bid) revert Unprofitable(profit, bid);
        weth.safeTransfer(router, bid);
    }

    /// @notice Owner withdraws profit or inventory.
    function withdraw(IERC20 token, uint256 amount, address to) external onlyOwner {
        token.safeTransfer(to, amount);
    }

    // ---------------------------------------------------------------------------------------------

    /// @dev Reserves oriented for a WETH -> other swap in pool `p`: (wethReserve, otherReserve).
    function _wethReserves(address p) internal view returns (uint256 rWeth, uint256 rOther, bool wethIs0) {
        IUniswapV2Pair pair = IUniswapV2Pair(p);
        (uint112 r0, uint112 r1,) = pair.getReserves();
        wethIs0 = pair.token0() == address(weth);
        (rWeth, rOther) = wethIs0 ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
    }

    /// @dev Cycle: WETH -> other in pool1, other -> WETH in pool2. Capped by inventory.
    function _optimalInput(address pool1, address pool2, uint256 inventory) internal view returns (uint256 dx) {
        (uint256 x1, uint256 y1,) = _wethReserves(pool1);
        (uint256 x2, uint256 y2,) = _wethReserves(pool2);
        dx = UniV2Math.optimalArbInput(x1, y1, y2, x2);
        if (dx > inventory) dx = inventory;
        if (dx > 0 && UniV2Math.cycleProfit(dx, x1, y1, y2, x2) <= 0) dx = 0;
    }

    function _executeCycle(address pool1, address pool2, uint256 dx) internal {
        (uint256 x1, uint256 y1, bool wethIs0In1) = _wethReserves(pool1);
        (uint256 x2, uint256 y2, bool wethIs0In2) = _wethReserves(pool2);

        uint256 dy = UniV2Math.getAmountOut(dx, x1, y1);
        uint256 back = UniV2Math.getAmountOut(dy, y2, x2);

        weth.safeTransfer(pool1, dx);
        // pool1: WETH in, other out, sent straight to pool2 (saves one token transfer).
        IUniswapV2Pair(pool1).swap(wethIs0In1 ? 0 : dy, wethIs0In1 ? dy : 0, pool2, "");
        // pool2: other in, WETH out, back to this contract.
        IUniswapV2Pair(pool2).swap(wethIs0In2 ? back : 0, wethIs0In2 ? 0 : back, address(this), "");
    }
}
