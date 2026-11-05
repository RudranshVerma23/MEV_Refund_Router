"""Sealed-bid FIRST-PRICE auction for the right to backrun one order.

Why first-price: the router can only check what the auctioneer signs (winner, executor, bid),
so the winner must pay its own signed bid. In a second-price auction the contract would have
to trust the auctioneer's claim about the runner-up bid, which allows shill bidding.

Rules:
  * sealed: bids are never shown to other searchers while the auction is open;
  * reserve: bids below `reserve` are ignored (set from the estimated net profit);
  * ranking: highest amount first, earliest bid on ties;
  * validity: a bid only wins if simulating swap + its backrun at the bid succeeds,
    so a searcher can't win with a bid it can't pay;
  * after closing, every bid (winning or not) is written to the audit log.
"""

from typing import Callable, Iterable, Optional

from .models import Bid

# simulate(bid) -> True if swap + this backrun executes on the latest state and pays >= bid.amount
Simulator = Callable[[Bid], bool]


def run_first_price_auction(bids: Iterable[Bid], reserve: int, simulate: Simulator) -> Optional[Bid]:
    """Return the winning bid, or None if no bid meets the reserve and simulates successfully.

    Bids are tried from highest amount down. The first one whose simulation succeeds wins;
    lower bids are only simulated if every higher bid failed.
    """
    ranked = sorted((b for b in bids if b.amount >= reserve), key=lambda b: (-b.amount, b.received_at))
    for bid in ranked:
        if simulate(bid):
            return bid
    return None
