"""Thin wrapper around Foundry's `cast` for everything the service and bots do on-chain.

Why `cast` instead of web3.py: no extra Python dependencies, and EIP-712 digests come from the
router itself (`hashOrder`, `hashAuctionResult`), so Python and Solidity can never disagree on
the hashing. Every call is a subprocess (~0.1-0.5 s), which is fine at auction cadence.

Private keys are passed to cast on the command line; use throwaway keys (the demo uses anvil's
well-known dev keys on a local fork). For anything real, switch to a keystore (`--account`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

ORDER_T = "(address,address,address,address,uint256,uint256,uint256,uint256,address)"
AUCTION_T = "(bytes32,address,address,uint256,uint256)"
ORDER_FIELDS = ["user", "tokenIn", "tokenOut", "pool", "amountIn", "minOut", "deadline", "nonce", "refundRecipient"]
AUCTION_FIELDS = ["orderHash", "winner", "executor", "bid", "validUntilBlock"]


class ChainError(Exception):
    pass


def _cast_bin() -> str:
    found = shutil.which("cast") or os.path.expanduser("~/.foundry/bin/cast")
    return found


def tuple_arg(d: dict, fields: list[str]) -> str:
    return "(" + ",".join(str(d[f]) for f in fields) + ")"


@dataclass
class Chain:
    rpc_url: str

    # ------------------------------------------------------------------ low level
    def _cast(self, *args: str, timeout: int = 300) -> str:
        proc = subprocess.run([_cast_bin(), *args], capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise ChainError((proc.stderr or proc.stdout).strip()[-800:])
        return proc.stdout.strip()

    def call(self, to: str, sig: str, *args: str, sender: str | None = None) -> list:
        """eth_call; returns decoded outputs as a list of strings."""
        extra = ["--from", sender] if sender else []
        out = self._cast("call", to, sig, *args, "--json", "--rpc-url", self.rpc_url, *extra)
        return json.loads(out)

    def send(self, pk: str, to: str, sig: str, *args: str, value: str | None = None) -> dict:
        """Signed transaction; returns the receipt (raises if it reverted)."""
        extra = ["--value", value] if value else []
        out = self._cast("send", to, sig, *args, "--private-key", pk, "--json", "--rpc-url", self.rpc_url, *extra)
        receipt = json.loads(out)
        if str(receipt.get("status")) not in ("1", "0x1"):
            raise ChainError(f"tx reverted: {receipt.get('transactionHash')}")
        return receipt

    def rpc(self, method: str, *params: str) -> str:
        return self._cast("rpc", method, *params, "--rpc-url", self.rpc_url)

    # ------------------------------------------------------------------ helpers
    def address_of(self, pk: str) -> str:
        return self._cast("wallet", "address", "--private-key", pk)

    def sign_digest(self, pk: str, digest: str) -> str:
        """Raw ECDSA over a 32-byte digest (r||s||v), as OpenZeppelin ECDSA.recover expects."""
        return self._cast("wallet", "sign", "--no-hash", "--private-key", pk, digest)

    def sign_message(self, pk: str, message: str) -> str:
        """EIP-191 personal_sign of a text message (used to authenticate bids)."""
        return self._cast("wallet", "sign", "--private-key", pk, message)

    def verify_message(self, address: str, message: str, signature: str) -> bool:
        try:
            self._cast("wallet", "verify", "--address", address, message, signature)
            return True
        except ChainError:
            return False

    def block_number(self) -> int:
        return int(self._cast("block-number", "--rpc-url", self.rpc_url))

    def block_field(self, field: str, block: str = "latest") -> int:
        return int(self._cast("block", block, "--field", field, "--rpc-url", self.rpc_url), 0)

    def base_fee(self) -> int:
        return self.block_field("baseFeePerGas")

    def balance_of(self, token: str, who: str) -> int:
        return int(self.call(token, "balanceOf(address)(uint256)", who)[0])

    def reserves(self, pool: str) -> tuple[str, str, int, int]:
        t0 = self.call(pool, "token0()(address)")[0]
        t1 = self.call(pool, "token1()(address)")[0]
        r = self.call(pool, "getReserves()(uint112,uint112,uint32)")
        return t0, t1, int(r[0]), int(r[1])

    def abi_encode(self, sig: str, *args: str) -> str:
        return self._cast("abi-encode", sig, *args)

    # ------------------------------------------------------------------ router
    def hash_order(self, router: str, order: dict) -> str:
        return self.call(router, f"hashOrder({ORDER_T})(bytes32)", tuple_arg(order, ORDER_FIELDS))[0]

    def hash_auction(self, router: str, result: dict) -> str:
        return self.call(router, f"hashAuctionResult({AUCTION_T})(bytes32)", tuple_arg(result, AUCTION_FIELDS))[0]

    def simulate_settle(self, router: str, order: dict, user_sig: str, result: dict, auc_sig: str, data: str) -> tuple[int, int]:
        """eth_call settle() from the would-be winner: proves the bid would pay. Raises on revert."""
        out = self.call(
            router,
            f"settle({ORDER_T},bytes,{AUCTION_T},bytes,bytes)(uint256,uint256)",
            tuple_arg(order, ORDER_FIELDS), user_sig, tuple_arg(result, AUCTION_FIELDS), auc_sig, data,
            sender=result["winner"],
        )
        return int(out[0]), int(out[1])

    def settle(self, pk: str, router: str, order: dict, user_sig: str, result: dict, auc_sig: str, data: str) -> dict:
        return self.send(
            pk, router, f"settle({ORDER_T},bytes,{AUCTION_T},bytes,bytes)",
            tuple_arg(order, ORDER_FIELDS), user_sig, tuple_arg(result, AUCTION_FIELDS), auc_sig, data,
        )

    def simulate_fill(self, router: str, order: dict, user_sig: str, sender: str) -> int:
        out = self.call(router, f"fill({ORDER_T},bytes)(uint256)", tuple_arg(order, ORDER_FIELDS), user_sig, sender=sender)
        return int(out[0])

    def fill(self, pk: str, router: str, order: dict, user_sig: str) -> dict:
        return self.send(pk, router, f"fill({ORDER_T},bytes)", tuple_arg(order, ORDER_FIELDS), user_sig)
