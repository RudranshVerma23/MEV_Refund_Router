// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";

import {IUniswapV2Pair} from "./interfaces/IUniswapV2Pair.sol";
import {IBackrunner} from "./interfaces/IBackrunner.sol";
import {UniV2Math} from "./libraries/UniV2Math.sol";

/// @title BackrunRouter
/// @notice Executes a user's signed swap order and, in the SAME transaction, the backrun bought
///         by the auction winner. It enforces the winner's payment on-chain and refunds a share
///         of it to the user.
///
/// Flow (option C, "signed intent, winner submits"):
///   1. The user signs an EIP-712 `Order` and approves the router for exactly `amountIn`.
///   2. The off-chain auctioneer simulates the order, runs a sealed-bid first-price auction and
///      signs an `AuctionResult` naming the winner, their executor and the bid.
///   3. The winner calls `settle`. The router checks both signatures, swaps for the user
///      (output >= minOut), calls the winner's executor, checks that >= bid arrived, then pays
///      `refundBps` of it to the user and the rest to the treasury.
///   If any check fails, the whole transaction reverts. The user paid no gas (the winner did),
///   so they lose nothing; the auctioneer can re-auction the order or use `fill`.
///
/// Version 1 scope: one Uniswap V2-style pool per order; the bid is paid in `bidToken` (WETH).
contract BackrunRouter is EIP712, Ownable, ReentrancyGuardTransient {
    using SafeERC20 for IERC20;

    // ---------------------------------------------------------------------------------------------
    // Types
    // ---------------------------------------------------------------------------------------------

    /// @dev All fields are static types: `abi.encode(TYPEHASH, struct)` then gives exactly the
    ///      EIP-712 `encodeData`, so the field order here MUST match the typehash string.
    struct Order {
        address user; // signer, pays `amountIn`, receives the output
        address tokenIn;
        address tokenOut;
        address pool; // Uniswap V2-style pair to swap in
        uint256 amountIn;
        uint256 minOut; // slippage protection (also caps how much a sandwich can take)
        uint256 deadline; // unix timestamp
        uint256 nonce; // per-user, single use
        address refundRecipient; // receives the MEV refund (usually == user)
    }

    struct AuctionResult {
        bytes32 orderHash; // hashOrder(order): binds the result to one order
        address winner; // only this address may call settle
        address executor; // winner's IBackrunner contract
        uint256 bid; // minimum payment in bidToken
        uint256 validUntilBlock; // auction results go stale quickly
    }

    bytes32 public constant ORDER_TYPEHASH = keccak256(
        "Order(address user,address tokenIn,address tokenOut,address pool,uint256 amountIn,uint256 minOut,uint256 deadline,uint256 nonce,address refundRecipient)"
    );
    bytes32 public constant AUCTION_TYPEHASH = keccak256(
        "AuctionResult(bytes32 orderHash,address winner,address executor,uint256 bid,uint256 validUntilBlock)"
    );
    uint256 public constant BPS = 10_000;

    // ---------------------------------------------------------------------------------------------
    // State
    // ---------------------------------------------------------------------------------------------

    IERC20 public immutable bidToken;
    address public auctioneer;
    address public treasury;
    uint16 public refundBps; // share of the winner's payment refunded to the user, e.g. 9000 = 90%
    uint32 public backrunGasCap; // most gas forwarded to the executor callback

    mapping(address user => mapping(uint256 nonce => bool)) public nonceUsed;

    // ---------------------------------------------------------------------------------------------
    // Events & errors
    // ---------------------------------------------------------------------------------------------

    event Settled(
        bytes32 indexed orderHash,
        address indexed user,
        address indexed winner,
        uint256 amountOut,
        uint256 paid,
        uint256 refund
    );
    event Filled(bytes32 indexed orderHash, address indexed user, uint256 amountOut);
    event NonceInvalidated(address indexed user, uint256 nonce);
    event AuctioneerSet(address auctioneer);
    event TreasurySet(address treasury);
    event RefundBpsSet(uint16 refundBps);
    event BackrunGasCapSet(uint32 gasCap);

    error Expired();
    error BadUserSignature();
    error BadAuctioneerSignature();
    error NonceAlreadyUsed();
    error OrderMismatch();
    error NotWinner();
    error NotAuctioneerOrUser();
    error BadPool();
    error InsufficientOutput(uint256 amountOut, uint256 minOut);
    error Underpaid(uint256 paid, uint256 bid);
    error BadParam();

    constructor(
        IERC20 _bidToken,
        address _auctioneer,
        address _treasury,
        uint16 _refundBps,
        uint32 _backrunGasCap,
        address _owner
    ) EIP712("BackrunRouter", "1") Ownable(_owner) {
        if (address(_bidToken) == address(0) || _auctioneer == address(0) || _treasury == address(0)) {
            revert BadParam();
        }
        if (_refundBps > BPS) revert BadParam();
        bidToken = _bidToken;
        auctioneer = _auctioneer;
        treasury = _treasury;
        refundBps = _refundBps;
        backrunGasCap = _backrunGasCap;
    }

    // ---------------------------------------------------------------------------------------------
    // Hashing (the off-chain auctioneer must produce exactly these digests)
    // ---------------------------------------------------------------------------------------------

    function hashOrder(Order calldata o) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(ORDER_TYPEHASH, o)));
    }

    function hashAuctionResult(AuctionResult calldata a) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(AUCTION_TYPEHASH, a)));
    }

    // ---------------------------------------------------------------------------------------------
    // Main entry points
    // ---------------------------------------------------------------------------------------------

    /// @notice Swap + auctioned backrun + refund, atomically. Callable only by the auction winner.
    function settle(
        Order calldata o,
        bytes calldata userSig,
        AuctionResult calldata a,
        bytes calldata auctioneerSig,
        bytes calldata backrunData
    ) external nonReentrant returns (uint256 amountOut, uint256 refund) {
        if (block.number > a.validUntilBlock) revert Expired();
        // Stops anyone who copies the order + signatures from the mempool from executing them.
        if (msg.sender != a.winner) revert NotWinner();

        bytes32 orderHash = _consumeOrder(o, userSig);
        if (a.orderHash != orderHash) revert OrderMismatch();
        // OZ ECDSA rejects malleable (high-s) signatures; see Mastering Ethereum ch. 9, "Signature Replay Attack".
        if (ECDSA.recover(hashAuctionResult(a), auctioneerSig) != auctioneer) revert BadAuctioneerSignature();

        // 1. The user's swap. Output goes straight to the user and is checked against minOut.
        amountOut = _swapAndDeliver(o);

        // 2. The backrun. The executor must transfer >= bid of bidToken to this contract.
        //    Measure what actually arrived instead of trusting a return value or an on-chain price.
        uint256 balanceBefore = bidToken.balanceOf(address(this));
        IBackrunner(a.executor).backrun{gas: backrunGasCap}(msg.sender, orderHash, a.bid, backrunData);
        uint256 paid = bidToken.balanceOf(address(this)) - balanceBefore;
        if (paid < a.bid) revert Underpaid(paid, a.bid);

        // 3. Refund. Any amount paid above the bid is shared in the same proportions.
        refund = (paid * refundBps) / BPS;
        if (refund > 0) bidToken.safeTransfer(o.refundRecipient, refund);
        if (paid > refund) bidToken.safeTransfer(treasury, paid - refund);

        emit Settled(orderHash, o.user, msg.sender, amountOut, paid, refund);
    }

    /// @notice Plain swap with no backrun: the fallback when nobody bids or the MEV is too small.
    /// @dev Restricted to the auctioneer (as relayer) or the user. If anyone could fill orders
    ///      without a backrun, a searcher could fill the order and then backrun it in the next
    ///      transaction without paying anything.
    function fill(Order calldata o, bytes calldata userSig) external nonReentrant returns (uint256 amountOut) {
        if (msg.sender != auctioneer && msg.sender != o.user) revert NotAuctioneerOrUser();
        bytes32 orderHash = _consumeOrder(o, userSig);
        amountOut = _swapAndDeliver(o);
        emit Filled(orderHash, o.user, amountOut);
    }

    /// @notice Lets a user cancel a signed order that has not been executed yet.
    function invalidateNonce(uint256 nonce) external {
        nonceUsed[msg.sender][nonce] = true;
        emit NonceInvalidated(msg.sender, nonce);
    }

    // ---------------------------------------------------------------------------------------------
    // Internals
    // ---------------------------------------------------------------------------------------------

    function _consumeOrder(Order calldata o, bytes calldata userSig) internal returns (bytes32 orderHash) {
        if (block.timestamp > o.deadline) revert Expired();
        if (nonceUsed[o.user][o.nonce]) revert NonceAlreadyUsed();
        orderHash = hashOrder(o);
        // SignatureChecker supports both EOAs (ECDSA) and smart-contract wallets (ERC-1271).
        if (!SignatureChecker.isValidSignatureNow(o.user, orderHash, userSig)) revert BadUserSignature();
        nonceUsed[o.user][o.nonce] = true; // effects before interactions
    }

    /// @dev Split into small helpers to stay within the EVM's 16 reachable stack slots
    ///      (avoids "stack too deep" without needing the via-IR pipeline).
    function _swapAndDeliver(Order calldata o) internal returns (uint256 amountOut) {
        (bool zeroForOne, uint256 reserveIn, uint256 reserveOut) = _poolInfo(o);

        // Pull the input straight into the pair (the user approved the router for amountIn).
        IERC20(o.tokenIn).safeTransferFrom(o.user, o.pool, o.amountIn);
        // Use what the pair actually received, so fee-on-transfer tokens don't break the maths.
        uint256 quoted = UniV2Math.getAmountOut(IERC20(o.tokenIn).balanceOf(o.pool) - reserveIn, reserveIn, reserveOut);

        amountOut = _pairSwap(o.pool, o.tokenOut, o.user, zeroForOne, quoted);
        if (amountOut < o.minOut) revert InsufficientOutput(amountOut, o.minOut);
    }

    /// @dev Swap direction and reserves oriented as (in, out). Reverts if the pool doesn't trade this pair.
    function _poolInfo(Order calldata o)
        internal
        view
        returns (bool zeroForOne, uint256 reserveIn, uint256 reserveOut)
    {
        IUniswapV2Pair pair = IUniswapV2Pair(o.pool);
        address token0 = pair.token0();
        address token1 = pair.token1();
        if (o.tokenIn == token0 && o.tokenOut == token1) zeroForOne = true;
        else if (o.tokenIn == token1 && o.tokenOut == token0) zeroForOne = false;
        else revert BadPool();

        (uint112 r0, uint112 r1,) = pair.getReserves();
        (reserveIn, reserveOut) = zeroForOne ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
    }

    /// @dev Executes the pair swap with output to `to`; returns what `to` actually received.
    function _pairSwap(address pool, address tokenOut, address to, bool zeroForOne, uint256 quoted)
        internal
        returns (uint256 received)
    {
        uint256 balanceBefore = IERC20(tokenOut).balanceOf(to);
        IUniswapV2Pair(pool).swap(zeroForOne ? 0 : quoted, zeroForOne ? quoted : 0, to, "");
        received = IERC20(tokenOut).balanceOf(to) - balanceBefore;
    }

    // ---------------------------------------------------------------------------------------------
    // Admin
    // ---------------------------------------------------------------------------------------------

    function setAuctioneer(address _auctioneer) external onlyOwner {
        if (_auctioneer == address(0)) revert BadParam();
        auctioneer = _auctioneer;
        emit AuctioneerSet(_auctioneer);
    }

    function setTreasury(address _treasury) external onlyOwner {
        if (_treasury == address(0)) revert BadParam();
        treasury = _treasury;
        emit TreasurySet(_treasury);
    }

    function setRefundBps(uint16 _refundBps) external onlyOwner {
        if (_refundBps > BPS) revert BadParam();
        refundBps = _refundBps;
        emit RefundBpsSet(_refundBps);
    }

    function setBackrunGasCap(uint32 _gasCap) external onlyOwner {
        backrunGasCap = _gasCap;
        emit BackrunGasCapSet(_gasCap);
    }
}
