"""Minimal JSON-RPC client (standard library only) shared by the backtest scripts."""

import json
import os
import time
import urllib.error
import urllib.request

UNI_V2_USDC_WETH = "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc"
SUSHI_USDC_WETH = "0x397FF1542f962076d0BFE58eA045FfA2d347ACa0"
# keccak256("Swap(address,uint256,uint256,uint256,uint256,address)")
SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"


class RpcError(Exception):
    pass


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from .env into os.environ (without overriding variables already set)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if value.strip():
                    os.environ.setdefault(key.strip(), value.strip())


def call(url: str, method: str, params: list, retries: int = 4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(retries):
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json", "User-Agent": "mev-refund-router-backtest"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.load(resp)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise RpcError(str(e)) from e
            time.sleep(2**attempt)
            continue
        if "error" in out:
            msg = str(out["error"])
            if "rate" in msg.lower() and attempt < retries - 1:  # back off on rate limits
                time.sleep(2**attempt)
                continue
            raise RpcError(msg)
        return out["result"]
    raise RpcError("retries exhausted")


def block_context(url: str, block: int, tx_index: int, pools: list[str]) -> dict:
    """What SettleReplay needs to rebuild the pools' state just before tx `tx_index` of `block`.

    PRIOR_TXS = earlier txs of the same block that emitted any log from one of `pools`.
    """
    header = call(url, "eth_getBlockByNumber", [hex(block), False])
    prior: list[tuple[int, str]] = []
    for pool in pools:
        for lg in call(url, "eth_getLogs", [{"address": pool, "fromBlock": hex(block), "toBlock": hex(block)}]):
            idx = int(lg["transactionIndex"], 16)
            if idx < tx_index:
                prior.append((idx, lg["transactionHash"]))
    ordered = [h for _, h in sorted(set(prior))]  # replay in block order
    return {
        "FORK_BLOCK": str(block - 1),
        "BLOCK_NUMBER": str(block),
        "BASE_FEE": str(int(header["baseFeePerGas"], 16)),
        "PRIOR_TXS": ",".join(ordered),
    }
