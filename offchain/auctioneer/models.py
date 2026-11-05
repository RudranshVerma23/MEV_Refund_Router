"""Data types shared by the auctioneer and searcher bots."""

from dataclasses import dataclass, field
import time


@dataclass(frozen=True)
class Bid:
    searcher: str  # will call settle(), i.e. the AuctionResult.winner
    executor: str  # their IBackrunner contract
    amount: int  # bid in WETH wei
    backrun_data: str  # hex bytes passed to executor.backrun(...)
    received_at: float = field(default_factory=time.time)


def bid_message(order_id: str, executor: str, amount: int, backrun_data: str) -> str:
    """Text a searcher signs (EIP-191 personal_sign) to authenticate a bid.

    Without it, anyone could submit a bid in another searcher's name and force them to "win"
    at a price they never chose.
    """
    return f"backrun-bid:{order_id}:{executor.lower()}:{amount}:{backrun_data.lower()}"
