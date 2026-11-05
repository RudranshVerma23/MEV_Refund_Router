// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script, console} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BackrunRouter} from "../src/BackrunRouter.sol";
import {ReferenceArbExecutor} from "../src/executors/ReferenceArbExecutor.sol";

/// @notice Deploys the router and one ReferenceArbExecutor per searcher (for the local fork demo).
///   AUCTIONEER=0x.. TREASURY=0x.. SEARCHERS=0xA,0xB,0xC \
///   forge script script/DeployLocal.s.sol --rpc-url http://127.0.0.1:8545 --broadcast --private-key <deployer>
/// Writes the addresses to deployments/local.json.
contract DeployLocal is Script {
    address constant WETH = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;

    function run() external {
        address auctioneer = vm.envAddress("AUCTIONEER");
        address treasury = vm.envAddress("TREASURY");
        address[] memory searchers = vm.envAddress("SEARCHERS", ",");
        uint16 refundBps = uint16(vm.envOr("REFUND_BPS", uint256(9000)));

        vm.startBroadcast();
        BackrunRouter router =
            new BackrunRouter(IERC20(WETH), auctioneer, treasury, refundBps, 1_000_000, msg.sender);
        address[] memory executors = new address[](searchers.length);
        for (uint256 i = 0; i < searchers.length; i++) {
            executors[i] = address(new ReferenceArbExecutor(address(router), IERC20(WETH), searchers[i]));
        }
        vm.stopBroadcast();

        string memory obj = "deployment";
        vm.serializeAddress(obj, "executors", executors);
        string memory json = vm.serializeAddress(obj, "router", address(router));
        vm.writeJson(json, "deployments/local.json");
        console.log("router", address(router));
    }
}
