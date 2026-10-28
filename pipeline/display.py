"""Display rounding (definitions §7): the only rounding code used by the page, the facts file
and notes/evaluate.py.

Inputs are exact: integer cents, or ARR cents for MRR, which become Fractions of dollars.
Nothing here is ever fed back into a calculation.
"""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Sequence


def round_half_away(x: Fraction | int) -> int:
    """Nearest integer, halves away from zero."""
    x = Fraction(x)
    n = (abs(x.numerator) * 2 + x.denominator) // (2 * x.denominator)
    return n if x >= 0 else -n


def cents_to_dollars(cents: int) -> Fraction:
    return Fraction(cents, 100)


def arr_cents_to_mrr_dollars(arr_cents: int) -> Fraction:
    return Fraction(arr_cents, 1200)


def whole_dollars(dollars: Fraction) -> int:
    return round_half_away(dollars)


def thousands(dollars: Fraction) -> Decimal:
    """$k to 1 decimal place, e.g. 77482.2 -> Decimal('77.5')."""
    return Decimal(round_half_away(dollars / 100)).scaleb(-1)


def percent(numer: Fraction | int, denom: Fraction | int, places: int = 1) -> Decimal:
    value = Fraction(numer) * 100 / Fraction(denom)
    return Decimal(round_half_away(value * 10 ** places)).scaleb(-places)


def bridge(opening: Fraction, components: Sequence[tuple[str, Fraction]], closing: Fraction) -> dict[str, int]:
    """Whole-dollar bridge whose displayed components sum to the displayed closing figure.

    Opening and closing are rounded on their own. Components are rounded, then the
    shortfall is spread one dollar at a time (largest-remainder method): to the components
    rounding moved furthest the other way, ties to the larger absolute value, then bridge order.
    Components are signed (contraction and churn negative).
    """
    shown_open, shown_close = round_half_away(opening), round_half_away(closing)
    values = [Fraction(v) for _, v in components]
    shown = [round_half_away(v) for v in values]
    gap = (shown_close - shown_open) - sum(shown)
    if abs(gap) > len(shown):
        raise ValueError(f"bridge does not tie before rounding: gap of {gap} display units")
    step = 1 if gap > 0 else -1
    order = sorted(range(len(values)),
                   key=lambda i: (-step * (values[i] - shown[i]), -abs(values[i]), i))
    for i in order[:abs(gap)]:
        shown[i] += step
    out = {"opening": shown_open}
    out.update({name: s for (name, _), s in zip(components, shown)})
    out["closing"] = shown_close
    return out


MRR_BRIDGE_COMPONENTS = [
    ("new", "new_arr_cents", 1), ("reactivation", "reactivation_arr_cents", 1),
    ("expansion", "expansion_arr_cents", 1), ("contraction", "contraction_arr_cents", -1),
    ("churn_voluntary", "churn_voluntary_arr_cents", -1), ("churn_involuntary", "churn_involuntary_arr_cents", -1),
]


def mrr_bridge_row(row: dict) -> dict[str, int]:
    """Displayed MRR bridge (whole dollars) for one row of the mrr_bridge mart."""
    return bridge(arr_cents_to_mrr_dollars(row["opening_arr_cents"]),
                  [(name, sign * arr_cents_to_mrr_dollars(row[col])) for name, col, sign in MRR_BRIDGE_COMPONENTS],
                  arr_cents_to_mrr_dollars(row["closing_arr_cents"]))
