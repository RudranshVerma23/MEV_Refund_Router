// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

/// @notice Constant-product (x*y=k) maths for Uniswap V2-style pools with a 0.3% fee.
library UniV2Math {
    uint256 internal constant FEE_NUM = 997; // gamma = 997/1000
    uint256 internal constant FEE_DEN = 1000;

    /// @notice Output for `amountIn` sold into a pool with the given reserves (same formula as UniswapV2Library).
    function getAmountOut(uint256 amountIn, uint256 reserveIn, uint256 reserveOut) internal pure returns (uint256) {
        if (amountIn == 0 || reserveIn == 0 || reserveOut == 0) return 0;
        uint256 inWithFee = amountIn * FEE_NUM;
        return (inWithFee * reserveOut) / (reserveIn * FEE_DEN + inWithFee);
    }

    /// @notice Profit-maximising input for a two-pool cycle X -> Y (pool 1) -> X (pool 2).
    /// @dev Chaining the two swaps gives out(dx) = a*dx / (b + c*dx) with
    ///        a = g^2 * x2 * y1,  b = x1 * y2,  c = g * (y2 + g * y1),   g = 997/1000.
    ///      Profit out(dx) - dx is concave; setting its derivative to zero gives
    ///        dx* = (sqrt(a*b) - b) / c,
    ///      which is positive only when pool 2 values Y more (in X) than pool 1 does.
    ///      Multiplying through by FEE_DEN^2 keeps the maths in integers:
    ///        dx* = (g*d*sqrt(x1*y1*x2*y2) - d^2*x1*y2) / (g*(d*y2 + g*y1)).
    ///      sqrt(x1*y1*x2*y2) is computed as sqrt(x1*y1)*sqrt(x2*y2) so that it cannot overflow:
    ///      reserves are uint112, so each product is < 2^224.
    /// @param x1 reserve of X in pool 1   @param y1 reserve of Y in pool 1
    /// @param y2 reserve of Y in pool 2   @param x2 reserve of X in pool 2
    /// @return dx Optimal amount of X to put in (0 if there is no profitable arbitrage).
    function optimalArbInput(uint256 x1, uint256 y1, uint256 y2, uint256 x2) internal pure returns (uint256 dx) {
        if (x1 == 0 || y1 == 0 || y2 == 0 || x2 == 0) return 0;
        uint256 s = Math.sqrt(x1 * y1) * Math.sqrt(x2 * y2);
        uint256 lhs = FEE_NUM * FEE_DEN * s;
        uint256 rhs = FEE_DEN * FEE_DEN * x1 * y2;
        if (lhs <= rhs) return 0;
        dx = (lhs - rhs) / (FEE_NUM * (FEE_DEN * y2 + FEE_NUM * y1));
    }

    /// @notice Profit (in X) of sending `dx` around the cycle; negative if it loses money.
    function cycleProfit(uint256 dx, uint256 x1, uint256 y1, uint256 y2, uint256 x2) internal pure returns (int256) {
        uint256 dy = getAmountOut(dx, x1, y1);
        uint256 back = getAmountOut(dy, y2, x2);
        return int256(back) - int256(dx);
    }
}
