import unittest

from offchain.auctioneer.auction import run_first_price_auction
from offchain.auctioneer.models import Bid


def bid(who, amount, t):
    return Bid(searcher=who, executor=who, amount=amount, backrun_data=b"", received_at=t)


class AuctionTest(unittest.TestCase):
    def test_highest_valid_bid_wins(self):
        bids = [bid("a", 5, 1), bid("b", 9, 2), bid("c", 7, 3)]
        self.assertEqual(run_first_price_auction(bids, 0, lambda b: True).searcher, "b")

    def test_failed_simulation_falls_to_next(self):
        bids = [bid("a", 5, 1), bid("b", 9, 2), bid("c", 7, 3)]
        w = run_first_price_auction(bids, 0, lambda b: b.searcher != "b")
        self.assertEqual(w.searcher, "c")

    def test_reserve_and_ties(self):
        bids = [bid("late", 9, 5), bid("early", 9, 1), bid("low", 2, 0)]
        self.assertEqual(run_first_price_auction(bids, 3, lambda b: True).searcher, "early")
        self.assertIsNone(run_first_price_auction(bids, 10, lambda b: True))


if __name__ == "__main__":
    unittest.main()
