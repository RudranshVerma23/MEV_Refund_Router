"""Auctioneer logic against a fake chain (no RPC, no Foundry needed)."""

import json
import os
import tempfile
import threading
import time
import unittest

from offchain.auctioneer.detector import estimate_created_arb
from offchain.auctioneer.models import bid_message
from offchain.auctioneer.service import Auctioneer, Config
from offchain.common.chain import ChainError

E18, E6 = 10**18, 10**6
WETH, USDC = "0x" + "aa" * 20, "0x" + "bb" * 20
POOL_A, POOL_B = "0x" + "0a" * 20, "0x" + "0b" * 20
ROUTER = "0x" + "99" * 20


class FakeChain:
    """Pools as in the worked example: A = 100 WETH / 300k USDC, B = 1,000 WETH / 3M USDC."""

    def __init__(self, base_fee=10**9):
        self.res = {POOL_A: (WETH, USDC, 100 * E18, 300_000 * E6), POOL_B: (WETH, USDC, 1_000 * E18, 3_000_000 * E6)}
        self.fee = base_fee
        self.fill_ok = True
        self.can_pay: dict[str, int] = {}  # searcher -> max bid its backrun can actually pay
        self.filled = []

    def address_of(self, pk): return "0xauctioneer"
    def simulate_fill(self, router, order, sig, sender):
        if not self.fill_ok:
            raise ChainError("ERC20InsufficientAllowance")
        return 1
    def hash_order(self, router, order): return "0xorderhash" + order["nonce"]
    def hash_auction(self, router, result): return "0xdigest-" + result["winner"] + "-" + result["bid"]
    def sign_digest(self, pk, digest): return "sig(" + digest + ")"
    def reserves(self, pool): return self.res[pool]
    def base_fee(self): return self.fee
    def block_number(self): return 100
    def verify_message(self, address, message, signature): return signature == f"signed({address}:{message})"
    def simulate_settle(self, router, order, user_sig, result, auc_sig, data):
        if int(result["bid"]) > self.can_pay.get(result["winner"], 0):
            raise ChainError("Unprofitable")
        return 1, 1
    def call(self, *a, **k): return ["false"]
    def fill(self, pk, router, order, sig):
        self.filled.append(order["nonce"])
        return {"transactionHash": "0xfill"}


def order(amount_in=10 * E18, nonce="1"):
    return {"user": "0xuser", "tokenIn": WETH, "tokenOut": USDC, "pool": POOL_A, "amountIn": str(amount_in),
            "minOut": "0", "deadline": "9999999999", "nonce": nonce, "refundRecipient": "0xuser"}


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.chain = FakeChain()
        self.cfg = Config(router=ROUTER, weth=WETH, auctioneer_pk="k", partners={POOL_A: [POOL_B], POOL_B: [POOL_A]},
                          window_s=0.4, audit_log=os.path.join(self.tmp, "a.jsonl"))
        self.auc = Auctioneer(self.cfg, self.chain)

    def bid(self, oid, searcher, amount, sig=None):
        data = "0xdata"
        msg = bid_message(oid, "0xexec-" + searcher, amount, data)
        return self.auc.post_bid({"order_id": oid, "searcher": searcher, "executor": "0xexec-" + searcher,
                                  "amount": str(amount), "backrun_data": data,
                                  "signature": sig or f"signed({searcher}:{msg})"})

    def wait(self, st, statuses, timeout=5):
        end = time.time() + timeout
        while st.status not in statuses and time.time() < end:
            time.sleep(0.02)
        self.assertIn(st.status, statuses)

    def test_detector_matches_worked_example(self):
        profit, partner = estimate_created_arb(order(), self.chain.res, [POOL_B], WETH)
        self.assertEqual(partner, POOL_B)
        self.assertEqual(profit, 781513749829168636)  # same as the Solidity and brute-force results

    def test_full_auction_highest_valid_bid_wins(self):
        self.chain.can_pay = {"alice": int(0.5 * E18), "bob": int(0.7 * E18), "carol": int(0.1 * E18)}
        st = self.auc.submit_order(order(), "usersig")
        self.wait(st, ("open",))
        self.assertTrue(self.bid(st.order_id, "alice", int(0.4 * E18))[0])
        self.assertTrue(self.bid(st.order_id, "bob", int(0.6 * E18))[0])
        # carol bids highest but her backrun can't pay it: simulation fails, she must not win
        self.assertTrue(self.bid(st.order_id, "carol", int(0.75 * E18))[0])
        # a forged bid in alice's name is rejected
        ok, why = self.bid(st.order_id, "alice", int(0.76 * E18), sig="forged")
        self.assertFalse(ok)
        self.assertEqual(why, "bad bid signature")
        self.wait(st, ("awarded",))
        self.assertEqual(st.winner.searcher, "bob")
        self.assertEqual(st.result["bid"], str(int(0.6 * E18)))
        # only the winner gets the order and signatures
        won = self.auc.result_for(st.order_id, "bob")
        self.assertEqual(won["status"], "won")
        self.assertEqual(won["user_signature"], "usersig")
        self.assertEqual(self.auc.result_for(st.order_id, "alice"), {"status": "lost"})
        # every bid is in the audit log (written right after the award)
        end = time.time() + 5
        while not os.path.exists(self.cfg.audit_log) and time.time() < end:
            time.sleep(0.02)
        rec = json.loads(open(self.cfg.audit_log).read().splitlines()[-1])
        self.assertEqual({b["searcher"] for b in rec["bids"]}, {"alice", "bob", "carol"})
        self.assertEqual(rec["winner"], "bob")

    def test_hint_hides_user_and_signature(self):
        st = self.auc.submit_order(order(), "usersig")
        self.wait(st, ("open",))
        hint = self.auc.open_hints(0)[0]
        self.assertNotIn("usersig", json.dumps(hint))
        self.assertNotIn("0xuser", json.dumps(hint))
        self.assertNotIn("minOut", hint)

    def test_reserve_and_late_bids(self):
        self.chain.can_pay = {"alice": E18}
        st = self.auc.submit_order(order(), "usersig")
        self.wait(st, ("open",))
        self.assertTrue(self.bid(st.order_id, "alice", 1)[0])  # accepted but below reserve
        self.wait(st, ("no_winner",))
        self.assertFalse(self.bid(st.order_id, "alice", E18)[0])  # auction closed

    def test_small_swap_not_auctioned(self):
        st = self.auc.submit_order(order(amount_in=E18 // 100), "usersig", background=False)
        self.assertEqual(st.status, "not_auctionable")
        self.assertEqual(self.auc.open_hints(0), [])
        self.assertEqual(self.chain.filled, [])  # not relayed unless configured

    def test_relay_fill_when_configured(self):
        self.cfg.relay_fill = True
        st = self.auc.submit_order(order(amount_in=E18 // 100), "usersig", background=False)
        self.assertEqual(st.status, "filled")
        self.assertEqual(self.chain.filled, ["1"])

    def test_high_gas_blocks_auction(self):
        self.chain.fee = 10**13  # 10,000 gwei: settle gas would exceed the ~0.78 ETH arbitrage
        st = self.auc.submit_order(order(), "usersig", background=False)
        self.assertEqual(st.status, "not_auctionable")

    def test_invalid_order_rejected(self):
        self.chain.fill_ok = False
        st = self.auc.submit_order(order(), "usersig", background=False)
        self.assertEqual(st.status, "invalid")
        self.assertIn("would not execute", st.detail)


if __name__ == "__main__":
    unittest.main()
