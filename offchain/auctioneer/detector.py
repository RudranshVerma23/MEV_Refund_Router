"""Detection: does an order create an arbitrage worth auctioning? Pure functions (no chain access).

The pre-screen matters more than anything else economically: the 2024 backtest found that
~96% of user swaps create less arbitrage than one settle() costs in gas, and relaying all of
them would cost far more than the refunds. So the auctioneer only opens an auction when the
estimated net profit is positive.
"""

from __future__ import annotations

from .v2math import cycle_profit, get_amount_out, optimal_arb_input

# pool -> (token0, token1, reserve0, reserve1)
Reserves = dict[str, tuple[str, str, int, int]]


def _weth_side(res: tuple[str, str, int, int], weth: str) -> tuple[int, int]:
    """(WETH reserve, other reserve) for a WETH pair."""
    t0, t1, r0, r1 = res
    if t0.lower() == weth.lower():
        return r0, r1
    if t1.lower() == weth.lower():
        return r1, r0
    raise ValueError("pool is not a WETH pair")


def reserves_after_swap(res: tuple[str, str, int, int], token_in: str, amount_in: int) -> tuple[str, str, int, int]:
    t0, t1, r0, r1 = res
    if token_in.lower() == t0.lower():
        out = get_amount_out(amount_in, r0, r1)
        return t0, t1, r0 + amount_in, r1 - out
    out = get_amount_out(amount_in, r1, r0)
    return t0, t1, r0 - out, r1 + amount_in


def best_cycle(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int, bool]:
    """Best WETH cycle between two pools given (wethReserve, otherReserve) for each.

    Returns (profit, input, a_first): a_first=True means WETH goes into pool a first.
    """
    best = (0, 0, True)
    for first, second, a_first in ((a, b, True), (b, a, False)):
        x1, y1 = first
        x2, y2 = second
        dx = optimal_arb_input(x1, y1, y2, x2)
        if dx:
            p = cycle_profit(dx, x1, y1, y2, x2)
            if p > best[0]:
                best = (p, dx, a_first)
    return best


def estimate_created_arb(order: dict, reserves: Reserves, partners: list[str], weth: str) -> tuple[int, str | None]:
    """WETH profit of the best backrun after `order`, net of arbitrage that already existed.

    Returns (profit_wei, partner_pool). The profit is gross: gas is not subtracted here.
    """
    pool = order["pool"]
    before = reserves[pool]
    after = reserves_after_swap(before, order["tokenIn"], int(order["amountIn"]))
    best_profit, best_partner = 0, None
    for p in partners:
        other = _weth_side(reserves[p], weth)
        existing = best_cycle(_weth_side(before, weth), other)[0]
        created = best_cycle(_weth_side(after, weth), other)[0] - existing
        if created > best_profit:
            best_profit, best_partner = created, p
    return best_profit, best_partner
