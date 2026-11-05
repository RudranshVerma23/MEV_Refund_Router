// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IUniswapV2Pair} from "../../src/interfaces/IUniswapV2Pair.sol";
import {IBackrunner} from "../../src/interfaces/IBackrunner.sol";
import {BackrunRouter} from "../../src/BackrunRouter.sol";

contract MockERC20 is ERC20 {
    uint8 private immutable _dec;

    constructor(string memory n, string memory s, uint8 d) ERC20(n, s) {
        _dec = d;
    }

    function decimals() public view override returns (uint8) {
        return _dec;
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

/// @notice Minimal Uniswap V2 pair: same swap and fee check as the real pair, without LP tokens or the oracle.
contract MockUniswapV2Pair is IUniswapV2Pair {
    address public token0;
    address public token1;
    uint112 private reserve0;
    uint112 private reserve1;

    constructor(address a, address b) {
        (token0, token1) = a < b ? (a, b) : (b, a);
    }

    function getReserves() external view returns (uint112, uint112, uint32) {
        return (reserve0, reserve1, uint32(block.timestamp));
    }

    /// @notice Set reserves to the current balances (call after funding the pair).
    function sync() external {
        reserve0 = uint112(IERC20(token0).balanceOf(address(this)));
        reserve1 = uint112(IERC20(token1).balanceOf(address(this)));
    }

    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata) external {
        require(amount0Out > 0 || amount1Out > 0, "INSUFFICIENT_OUTPUT");
        (uint256 r0, uint256 r1) = (reserve0, reserve1);
        require(amount0Out < r0 && amount1Out < r1, "INSUFFICIENT_LIQUIDITY");

        if (amount0Out > 0) IERC20(token0).transfer(to, amount0Out);
        if (amount1Out > 0) IERC20(token1).transfer(to, amount1Out);

        uint256 b0 = IERC20(token0).balanceOf(address(this));
        uint256 b1 = IERC20(token1).balanceOf(address(this));
        uint256 in0 = b0 > r0 - amount0Out ? b0 - (r0 - amount0Out) : 0;
        uint256 in1 = b1 > r1 - amount1Out ? b1 - (r1 - amount1Out) : 0;
        require(in0 > 0 || in1 > 0, "INSUFFICIENT_INPUT");

        // 0.3% fee on the input, then x*y >= k (same check as UniswapV2Pair.swap)
        uint256 b0Adj = b0 * 1000 - in0 * 3;
        uint256 b1Adj = b1 * 1000 - in1 * 3;
        require(b0Adj * b1Adj >= r0 * r1 * 1_000_000, "K");

        reserve0 = uint112(b0);
        reserve1 = uint112(b1);
    }
}

/// @notice Pays less than the bid, to test the router's Underpaid check.
contract UnderpayingExecutor is IBackrunner {
    IERC20 public immutable weth;

    constructor(IERC20 _weth) {
        weth = _weth;
    }

    function backrun(address, bytes32, uint256 bid, bytes calldata) external {
        weth.transfer(msg.sender, bid - 1);
    }
}

/// @notice Tries to re-enter settle from inside the callback.
contract ReentrantExecutor is IBackrunner {
    bytes public payload;

    function setPayload(bytes calldata p) external {
        payload = p;
    }

    function backrun(address, bytes32, uint256, bytes calldata) external {
        (bool ok, bytes memory ret) = msg.sender.call(payload);
        if (!ok) {
            assembly {
                revert(add(ret, 32), mload(ret)) // bubble up the router's revert reason
            }
        }
    }
}
