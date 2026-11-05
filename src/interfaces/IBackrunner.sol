// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Implemented by a searcher's executor contract. The router calls it right after
/// the user's swap, inside the same transaction.
interface IBackrunner {
    /// @param winner    The auction winner (the address that called `settle`). An executor MUST
    ///                  check this is its owner, otherwise anyone could win an auction that names
    ///                  someone else's executor and make it pay the bid.
    /// @param orderHash EIP-712 digest of the user's order being backrun.
    /// @param bid       Amount of the router's bid token the executor must transfer to the router
    ///                  (msg.sender) before returning. The router checks the balance change.
    /// @param data      Opaque strategy data chosen by the searcher (e.g. which pools to arbitrage).
    function backrun(address winner, bytes32 orderHash, uint256 bid, bytes calldata data) external;
}
