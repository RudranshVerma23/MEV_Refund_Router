"""Python mirror of src/libraries/UniV2Math.sol, used by the detector. Keep the two in sync."""

from math import isqrt

FEE_NUM = 997
FEE_DEN = 1000


def get_amount_out(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    in_with_fee = amount_in * FEE_NUM
    return in_with_fee * reserve_out // (reserve_in * FEE_DEN + in_with_fee)


def optimal_arb_input(x1: int, y1: int, y2: int, x2: int) -> int:
    """Best input of X for the cycle X -> Y (pool 1: x1, y1) -> X (pool 2: y2, x2). 0 if unprofitable."""
    if min(x1, y1, y2, x2) <= 0:
        return 0
    s = isqrt(x1 * y1) * isqrt(x2 * y2)  # same rounding as the Solidity version
    lhs = FEE_NUM * FEE_DEN * s
    rhs = FEE_DEN * FEE_DEN * x1 * y2
    if lhs <= rhs:
        return 0
    return (lhs - rhs) // (FEE_NUM * (FEE_DEN * y2 + FEE_NUM * y1))


def cycle_profit(dx: int, x1: int, y1: int, y2: int, x2: int) -> int:
    dy = get_amount_out(dx, x1, y1)
    return get_amount_out(dy, y2, x2) - dx
