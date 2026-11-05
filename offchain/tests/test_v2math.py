import unittest

from offchain.auctioneer.v2math import cycle_profit, get_amount_out, optimal_arb_input

E18 = 10**18
E6 = 10**6


class V2MathTest(unittest.TestCase):
    def test_amount_out_matches_uniswap_formula(self):
        # 10 WETH into 100 WETH / 300k USDC: about 27,198 USDC
        out = get_amount_out(10 * E18, 100 * E18, 300_000 * E6)
        self.assertAlmostEqual(out / E6, 27_198.3, delta=1)

    def test_no_arb_when_prices_equal(self):
        self.assertEqual(optimal_arb_input(100 * E18, 300_000 * E6, 3_000_000 * E6, 1_000 * E18), 0)

    def test_worked_example_arb(self):
        # After the user's 10 WETH sale, pool A is cheap: WETH -> USDC on B, USDC -> WETH on A.
        out = get_amount_out(10 * E18, 100 * E18, 300_000 * E6)
        a_weth, a_usdc = 110 * E18, 300_000 * E6 - out
        b_weth, b_usdc = 1_000 * E18, 3_000_000 * E6
        dx = optimal_arb_input(b_weth, b_usdc, a_usdc, a_weth)
        profit = cycle_profit(dx, b_weth, b_usdc, a_usdc, a_weth)
        self.assertAlmostEqual(dx / E18, 8.09, delta=0.05)  # brute-force optimum: 8.09
        self.assertAlmostEqual(profit / E18, 0.78, delta=0.02)

    def test_closed_form_is_optimal(self):
        # Moving 1% away from dx* in either direction must not increase profit.
        args = (1_000 * E18, 3_000_000 * E6, 272_801 * E6, 110 * E18)
        dx = optimal_arb_input(*args)
        best = cycle_profit(dx, *args)
        self.assertGreaterEqual(best, cycle_profit(dx * 99 // 100, *args))
        self.assertGreaterEqual(best, cycle_profit(dx * 101 // 100, *args))


if __name__ == "__main__":
    unittest.main()
