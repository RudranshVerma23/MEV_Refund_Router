// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice The subset of the Uniswap V2 pair interface the router and executor need.
/// Sushiswap and most V2 forks expose the same functions.
interface IUniswapV2Pair {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function getReserves() external view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast);

    /// @dev Optimistic transfer: the pair sends the outputs first, then checks that
    /// the constant-product invariant (after the 0.3% fee) still holds using its balances.
    /// The input tokens must already have been transferred to the pair.
    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
}
