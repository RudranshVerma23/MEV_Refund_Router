"""Off-chain auctioneer: order intake, detection, sealed-bid auction, signing, audit log.

Run (from the project root):
    python -m offchain.auctioneer.service --rpc http://127.0.0.1:8545 --router 0x... \
        --weth 0xC02a... --pair 0xPOOL:0xPARTNER [--port 8600]
    # the auctioneer key is read from the AUCTIONEER_PK environment variable

HTTP API (JSON):
    POST /orders          {"order": {...}, "signature": "0x.."}      -> {"order_id", "status"}
    GET  /orders/<id>                                                -> public status
    GET  /hints?after=<n>                                           -> open auctions (hints only)
    POST /bids            {"order_id","searcher","executor","amount","backrun_data","signature"}
    GET  /results/<id>?searcher=<addr>                               -> full package for the winner only

Life of an order:
  1. validate: eth_call router.fill(order) from the auctioneer. This proves the signature,
     approval, balance and minOut in one step, without changing state;
  2. detect: estimate the arbitrage the swap creates (detector.py) and subtract the gas of a
     settle() transaction. If that isn't positive, the order is NOT auctioned (status
     "not_auctionable"): the user should send a plain swap (or, with --relay-fill, the
     auctioneer relays fill() and pays its gas);
  3. auction: publish a hint (pool, tokens, size; never the signature, minOut or user),
     collect authenticated sealed bids for --window seconds, then run
     auction.run_first_price_auction with a reserve of --reserve-frac x the estimated net profit;
  4. award: sign AuctionResult(orderHash, winner, executor, bid, validUntilBlock) and hand the
     order + both signatures to the winner only, who submits settle() and pays the gas;
  5. audit: append every bid, the reserve, the winner and the outcome to --audit-log.

Trust: the router enforces payment and the refund, so the auctioneer can't steal funds or
shrink the refund. It IS trusted to pick the highest valid bid, to keep bids sealed and to
not censor orders; the audit log exists so that this can be checked afterwards.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..common.chain import Chain, ChainError
from .auction import run_first_price_auction
from .detector import estimate_created_arb
from .models import Bid, bid_message


@dataclass
class Config:
    router: str
    weth: str
    auctioneer_pk: str
    partners: dict[str, list[str]]  # pool -> partner pools trading the same pair
    settle_tx_gas: int = 390_000  # measured: ~355k execution + intrinsic + calldata (backtest median)
    tip_wei: int = 1_000_000_000
    min_net_wei: int = 0
    reserve_frac: float = 0.1  # reserve price = this x estimated net profit
    window_s: float = 3.0
    valid_blocks: int = 5
    relay_fill: bool = False
    audit_log: str = "offchain/logs/auctions.jsonl"


@dataclass
class OrderState:
    order_id: str
    order: dict
    signature: str
    status: str = "received"  # received|invalid|not_auctionable|open|awarded|no_winner|settled|filled|error
    detail: str = ""
    order_hash: str = ""
    est_profit: int = 0
    est_net: int = 0
    reserve: int = 0
    partner: str | None = None
    close_at: float = 0.0
    bids: dict[str, Bid] = field(default_factory=dict)  # searcher -> latest bid (sealed)
    winner: Bid | None = None
    result: dict | None = None
    result_sig: str = ""


class Auctioneer:
    """Core logic. The chain is injected so tests can use a fake."""

    def __init__(self, cfg: Config, chain: Chain):
        self.cfg = cfg
        self.chain = chain
        self.address = chain.address_of(cfg.auctioneer_pk)
        self.orders: dict[str, OrderState] = {}
        self.hints: list[dict] = []
        self.lock = threading.Lock()
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ intake
    def submit_order(self, order: dict, signature: str, background: bool = True) -> OrderState:
        order = {k: (str(v) if isinstance(v, int) else v) for k, v in order.items()}
        st = OrderState(order_id=f"o{next(self._ids)}", order=order, signature=signature)
        with self.lock:
            self.orders[st.order_id] = st
        if background:
            threading.Thread(target=self._process, args=(st,), daemon=True).start()
        else:
            self._process(st)
        return st

    def _process(self, st: OrderState) -> None:
        try:
            self._validate(st)
            if st.status == "invalid":
                return
            self._detect(st)
            if st.status == "not_auctionable":
                self._maybe_relay(st)
                return
            self._run_auction(st)
        except Exception as e:  # keep the service alive; record the failure
            st.status, st.detail = "error", f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            self._audit(st)

    def _validate(self, st: OrderState) -> None:
        o = st.order
        if o["pool"].lower() not in self.cfg.partners:
            st.status, st.detail = "invalid", "unsupported pool"
            return
        try:
            self.chain.simulate_fill(self.cfg.router, o, st.signature, sender=self.address)
        except ChainError as e:
            st.status, st.detail = "invalid", f"order would not execute: {str(e)[-200:]}"
            return
        st.order_hash = self.chain.hash_order(self.cfg.router, o)

    def _detect(self, st: OrderState) -> None:
        o = st.order
        partners = self.cfg.partners[o["pool"].lower()]
        reserves = {p: self.chain.reserves(p) for p in [o["pool"], *partners]}
        st.est_profit, st.partner = estimate_created_arb(o, reserves, partners, self.cfg.weth)
        gas_cost = self.cfg.settle_tx_gas * (self.chain.base_fee() + self.cfg.tip_wei)
        st.est_net = st.est_profit - gas_cost
        if st.partner is None or st.est_net <= self.cfg.min_net_wei:
            st.status = "not_auctionable"
            st.detail = f"estimated arbitrage {st.est_profit} wei < settle gas {gas_cost} wei"
            return
        st.reserve = int(st.est_net * self.cfg.reserve_frac)

    def _maybe_relay(self, st: OrderState) -> None:
        if not self.cfg.relay_fill:
            st.detail += "; send a plain swap instead"
            return
        receipt = self.chain.fill(self.cfg.auctioneer_pk, self.cfg.router, st.order, st.signature)
        st.status, st.detail = "filled", f"relayed fill() tx {receipt['transactionHash']}"

    # ------------------------------------------------------------------ auction
    def _run_auction(self, st: OrderState) -> None:
        o = st.order
        st.close_at = time.time() + self.cfg.window_s
        hint = {
            "seq": 0,  # set under the lock below
            "order_id": st.order_id,
            "pool": o["pool"],
            "partner_hint": st.partner,
            "tokenIn": o["tokenIn"],
            "tokenOut": o["tokenOut"],
            "amountIn": o["amountIn"],  # size disclosed: searchers can't value the backrun without it
            "reserve": str(st.reserve),
            "close_at": st.close_at,
        }
        with self.lock:
            hint["seq"] = len(self.hints) + 1
            st.status = "open"
            self.hints.append(hint)
        time.sleep(max(0.0, st.close_at - time.time()))
        with self.lock:
            st.status = "closed"
            bids = list(st.bids.values())

        def simulate(bid: Bid) -> bool:
            result, sig = self._sign_result(st, bid)
            try:
                self.chain.simulate_settle(self.cfg.router, o, st.signature, result, sig, bid.backrun_data)
                return True
            except ChainError as e:
                st.detail += f" | {bid.searcher[:10]} failed simulation: {str(e)[-120:]}"
                return False

        winner = run_first_price_auction(bids, st.reserve, simulate)
        if winner is None:
            st.status = "no_winner"
            self._maybe_relay(st)
            return
        st.result, st.result_sig = self._sign_result(st, winner)
        st.winner = winner
        st.status = "awarded"

    def _sign_result(self, st: OrderState, bid: Bid) -> tuple[dict, str]:
        result = {
            "orderHash": st.order_hash,
            "winner": bid.searcher,
            "executor": bid.executor,
            "bid": str(bid.amount),
            "validUntilBlock": str(self.chain.block_number() + self.cfg.valid_blocks),
        }
        digest = self.chain.hash_auction(self.cfg.router, result)
        return result, self.chain.sign_digest(self.cfg.auctioneer_pk, digest)

    def post_bid(self, body: dict) -> tuple[bool, str]:
        st = self.orders.get(body.get("order_id", ""))
        if st is None or st.status != "open" or time.time() > st.close_at:
            return False, "auction not open"
        try:
            bid = Bid(
                searcher=body["searcher"],
                executor=body["executor"],
                amount=int(body["amount"]),
                backrun_data=body["backrun_data"],
            )
        except (KeyError, ValueError):
            return False, "malformed bid"
        msg = bid_message(st.order_id, bid.executor, bid.amount, bid.backrun_data)
        if not self.chain.verify_message(bid.searcher, msg, body.get("signature", "")):
            return False, "bad bid signature"
        with self.lock:
            st.bids[bid.searcher.lower()] = bid  # sealed; a searcher may replace its own bid until close
        return True, "accepted"

    # ------------------------------------------------------------------ queries
    def public_status(self, order_id: str) -> dict | None:
        st = self.orders.get(order_id)
        if st is None:
            return None
        if st.status == "awarded" and self._settled(st):
            st.status = "settled"
        out = {"order_id": st.order_id, "status": st.status, "detail": st.detail,
               "est_profit": str(st.est_profit), "est_net": str(st.est_net), "reserve": str(st.reserve)}
        if st.status in ("awarded", "settled") and st.winner:
            out.update(winner=st.winner.searcher, price=str(st.winner.amount))
        return out

    def result_for(self, order_id: str, searcher: str) -> dict:
        st = self.orders.get(order_id)
        if st is None:
            return {"status": "unknown"}
        if st.status not in ("awarded", "settled"):
            return {"status": st.status}
        if st.winner is None or st.winner.searcher.lower() != searcher.lower():
            return {"status": "lost"}
        return {"status": "won", "order": st.order, "user_signature": st.signature,
                "result": st.result, "auctioneer_signature": st.result_sig,
                "backrun_data": st.winner.backrun_data}

    def open_hints(self, after: int) -> list[dict]:
        now = time.time()
        return [h for h in self.hints if h["seq"] > after and h["close_at"] > now]

    def _settled(self, st: OrderState) -> bool:
        try:
            used = self.chain.call(self.cfg.router, "nonceUsed(address,uint256)(bool)", st.order["user"], st.order["nonce"])
            return str(used[0]).lower() == "true"
        except ChainError:
            return False

    def _audit(self, st: OrderState) -> None:
        os.makedirs(os.path.dirname(self.cfg.audit_log) or ".", exist_ok=True)
        rec = {
            "time": time.time(), "order_id": st.order_id, "order_hash": st.order_hash, "status": st.status,
            "detail": st.detail, "est_profit": str(st.est_profit), "est_net": str(st.est_net),
            "reserve": str(st.reserve), "partner": st.partner,
            "bids": [{**asdict(b), "amount": str(b.amount)} for b in st.bids.values()],
            "winner": st.winner.searcher if st.winner else None,
            "price": str(st.winner.amount) if st.winner else None,
        }
        with self.lock, open(self.cfg.audit_log, "a") as f:
            f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------- HTTP
def make_handler(auc: Auctioneer):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, obj) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._body()
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid JSON"})
            if path == "/orders":
                if "order" not in body or "signature" not in body:
                    return self._json(400, {"error": "need order and signature"})
                st = auc.submit_order(body["order"], body["signature"])
                return self._json(202, {"order_id": st.order_id, "status": st.status})
            if path == "/bids":
                ok, msg = auc.post_bid(body)
                return self._json(200 if ok else 400, {"ok": ok, "detail": msg})
            self._json(404, {"error": "not found"})

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            parts = u.path.strip("/").split("/")
            if parts == ["hints"]:
                return self._json(200, auc.open_hints(int(q.get("after", ["0"])[0])))
            if len(parts) == 2 and parts[0] == "orders":
                s = auc.public_status(parts[1])
                return self._json(200 if s else 404, s or {"error": "unknown order"})
            if len(parts) == 2 and parts[0] == "results":
                return self._json(200, auc.result_for(parts[1], q.get("searcher", [""])[0]))
            self._json(404, {"error": "not found"})

        def log_message(self, *args):  # quiet
            pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Backrun auctioneer service")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--router", required=True)
    ap.add_argument("--weth", default="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
    ap.add_argument("--pair", action="append", required=True, help="POOL:PARTNER (repeatable; both directions are registered)")
    ap.add_argument("--port", type=int, default=8600)
    ap.add_argument("--window", type=float, default=3.0)
    ap.add_argument("--reserve-frac", type=float, default=0.1)
    ap.add_argument("--relay-fill", action="store_true")
    ap.add_argument("--audit-log", default="offchain/logs/auctions.jsonl")
    args = ap.parse_args()

    pk = os.environ.get("AUCTIONEER_PK")
    if not pk:
        raise SystemExit("set AUCTIONEER_PK")
    partners: dict[str, list[str]] = {}
    for p in args.pair:
        a, b = p.split(":")
        partners.setdefault(a.lower(), []).append(b)
        partners.setdefault(b.lower(), []).append(a)
    cfg = Config(router=args.router, weth=args.weth, auctioneer_pk=pk, partners=partners,
                 window_s=args.window, reserve_frac=args.reserve_frac, relay_fill=args.relay_fill,
                 audit_log=args.audit_log)
    auc = Auctioneer(cfg, Chain(args.rpc))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(auc))
    print(f"auctioneer {auc.address} listening on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
