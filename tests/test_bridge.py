"""Gate 2: the MRR bridge ties every month, and the movements and drivers are right.

The tie check proves every customer delta lands in exactly one movement. Its independence comes
from the other tests: closing MRR is checked against the simulation's truth, and the movement
classification is recomputed here in Python, straight from truth.
"""
import random
from collections import defaultdict
from fractions import Fraction

import pytest

from pipeline.display import (arr_cents_to_mrr_dollars, bridge, mrr_bridge_row, percent, round_half_away,
                              thousands)


def rows(db, sql, *params):
    cur = db.execute(sql, list(params))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


@pytest.fixture(scope="module")
def bridge_rows(marts):
    return rows(marts, "SELECT * FROM mrr_bridge ORDER BY month")


# ---- tie -------------------------------------------------------------------
def test_bridge_ties_every_month_to_the_cent(bridge_rows):
    assert len(bridge_rows) == 30
    assert [r["month"] for r in bridge_rows if r["unexplained_arr_cents"] != 0] == []


def test_closing_matches_truth_and_rolls_into_next_opening(marts, bridge_rows):
    truth = dict(marts.execute("SELECT month, sum(arr_cents) FROM truth.mrr_month_end GROUP BY 1").fetchall())
    for prev, cur in zip(bridge_rows, bridge_rows[1:]):
        assert cur["opening_arr_cents"] == prev["closing_arr_cents"]
    for r in bridge_rows:
        assert r["closing_arr_cents"] == truth[r["month"]]


def test_each_customer_has_exactly_one_movement_per_month(marts):
    dupes = marts.execute("SELECT month, customer FROM mrr_movements GROUP BY ALL HAVING count(*) > 1").fetchall()
    assert dupes == []


# ---- classification, recomputed independently from truth -------------------
def truth_movements(sim):
    arr = defaultdict(lambda: defaultdict(int))
    month_end = {}
    for r in sim.truth["mrr_month_end"]:
        arr[r["month"]][r["customer"]] += r["arr_cents"]
        month_end[r["month"]] = r["month_end"]
    paying = {m: {c: a for c, a in cs.items() if a > 0} for m, cs in arr.items()}
    months = sorted(paying)
    ended = defaultdict(list)
    for s in sim.rows["subscriptions"]:
        if s["ended_at"] is not None:
            ended[s["customer"]].append((s["ended_at"], s["cancellation_reason"]))

    seen, out = set(), {}
    for i, m in enumerate(months):
        prev = paying[months[i - 1]] if i else {}
        prev_end = month_end[months[i - 1]] if i else 0
        tally = defaultdict(lambda: [0, 0])
        for c in set(prev) | set(paying[m]):
            o, n = prev.get(c, 0), paying[m].get(c, 0)
            if o == 0:
                kind = "reactivation" if c in seen else "new"
            elif n == 0:
                last = max(e for e in ended[c] if prev_end < e[0] <= month_end[m])
                kind = "churn_involuntary" if last[1] == "payment_failed" else "churn_voluntary"
            elif n > o:
                kind = "expansion"
            elif n < o:
                kind = "contraction"
            else:
                continue
            tally[kind][0] += 1
            tally[kind][1] += n - o
        seen |= set(paying[m])
        out[m] = {k: tuple(v) for k, v in tally.items()}
    return out


def test_movements_match_independent_classification(sim, marts):
    expected = truth_movements(sim)
    got = defaultdict(dict)
    for r in rows(marts, """
            SELECT month, CASE WHEN movement = 'churn' THEN 'churn_' || churn_type ELSE movement END AS kind,
                   count(*) AS n, sum(arr_delta_cents) AS delta
            FROM mrr_movements WHERE movement <> 'flat' GROUP BY ALL"""):
        got[r["month"]][r["kind"]] = (r["n"], r["delta"])
    assert dict(got) == expected


def test_drivers_sum_to_each_customers_delta(marts):
    bad = marts.execute("""
        SELECT m.month, m.customer, m.arr_delta_cents, sum(d.arr_delta_cents)
        FROM mrr_movements m LEFT JOIN mrr_drivers d USING (month, customer)
        WHERE m.movement IN ('expansion', 'contraction')
        GROUP BY ALL HAVING sum(d.arr_delta_cents) IS DISTINCT FROM m.arr_delta_cents""").fetchall()
    assert bad == []
    assert marts.execute("SELECT count(*) FROM mrr_drivers WHERE driver = 'none'").fetchone()[0] == 0


# ---- planted events at the metrics layer ------------------------------------
def driver(marts, month, name, movement):
    r = marts.execute("SELECT arr_delta_cents, customers FROM mrr_bridge_drivers WHERE month = ? AND driver = ? "
                      "AND movement = ?", [month, name, movement]).fetchone()
    return r or (0, 0)


def test_e1_price_change_expansion_in_september_only(marts):
    sep, customers = driver(marts, "2025-09", "price_change", "expansion")
    assert sep > 0 and customers >= 50
    for month in ("2025-06", "2025-07", "2025-08"):
        assert driver(marts, month, "price_change", "expansion")[0] == 0
    # All of it is Starter, and it is the expansion driver with the most customers. The answer key's
    # E1 signal is about customers expanding, not dollars: after adjustment 6, 3 plan changes ($670)
    # out-earn the price change ($514, 87 customers) in dollars.
    starter_only = marts.execute("""
        SELECT bool_and(i.product = 'Starter') FROM mrr_drivers d
        JOIN mrr_item_month_end i ON i.month = d.month AND i.subscription_item = d.subscription_item
        WHERE d.month = '2025-09' AND d.driver = 'price_change'""").fetchone()[0]
    assert starter_only
    top = marts.execute("SELECT driver FROM mrr_bridge_drivers WHERE month = '2025-09' AND movement = 'expansion' "
                        "ORDER BY customers DESC LIMIT 1").fetchone()[0]
    assert top == "price_change"


def test_e2_one_logo_is_most_of_november_churn(marts, sim):
    e2 = sim.key["E2_largest_churn"]["customer"]
    churn = dict(marts.execute("SELECT customer, -arr_delta_cents FROM mrr_movements "
                               "WHERE month = '2025-11' AND movement = 'churn'").fetchall())
    assert max(churn, key=churn.get) == e2 and churn[e2] > 0.5 * sum(churn.values())


def test_e3_discount_roll_off_is_april_expansion_from_january_cohort(marts):
    amount, customers = driver(marts, "2026-04", "discount_ended", "expansion")
    assert amount > 0 and customers >= 20
    from_cohort = marts.execute("""
        SELECT sum(d.arr_delta_cents) FILTER (WHERE f.first_month = '2026-01') / sum(d.arr_delta_cents)
        FROM mrr_drivers d JOIN (SELECT customer, min(month) AS first_month FROM customer_mrr GROUP BY 1) f USING (customer)
        WHERE d.month = '2026-04' AND d.driver = 'discount_ended'""").fetchone()[0]
    assert from_cohort == 1


def test_e5_interval_switch_contraction_in_march(marts):
    amount, customers = driver(marts, "2026-03", "interval_switch", "contraction")
    assert amount < 0 and customers >= 20
    trailing = [driver(marts, m, "interval_switch", "contraction")[0] for m in ("2025-12", "2026-01", "2026-02")]
    assert amount < 5 * min(trailing + [-1])     # at least 5x the largest recent month


def test_e7_reactivation_wave_in_may(bridge_rows):
    by_month = {r["month"]: r for r in bridge_rows}
    trailing = sum(by_month[m]["reactivation_arr_cents"] for m in ("2026-02", "2026-03", "2026-04")) / 3
    assert by_month["2026-05"]["reactivation_arr_cents"] > 3 * trailing


# ---- display rounding (definitions §7) --------------------------------------
def test_round_half_away_from_zero():
    assert [round_half_away(Fraction(n, 2)) for n in (-3, -1, 1, 3, 5)] == [-2, -1, 1, 2, 3]
    assert percent(1, 3) == percent(Fraction(1, 3), 1) and str(percent(1, 3)) == "33.3"
    assert str(thousands(Fraction(774822, 10))) == "77.5"


def test_displayed_bridge_adds_up_every_month(bridge_rows):
    for r in bridge_rows:
        shown = mrr_bridge_row(r)
        parts = [v for k, v in shown.items() if k not in ("opening", "closing")]
        assert shown["opening"] + sum(parts) == shown["closing"], r["month"]
        assert shown["closing"] == round_half_away(arr_cents_to_mrr_dollars(r["closing_arr_cents"]))


def test_largest_remainder_moves_each_component_at_most_one_unit():
    rng = random.Random(0)
    for _ in range(2000):
        comps = [Fraction(rng.randint(-10**6, 10**6), rng.choice([12, 100, 1200])) for _ in range(rng.randint(2, 7))]
        opening = Fraction(rng.randint(0, 10**8), 1200)
        shown = bridge(opening, [(str(i), c) for i, c in enumerate(comps)], opening + sum(comps))
        parts = [shown[str(i)] for i in range(len(comps))]
        assert shown["opening"] + sum(parts) == shown["closing"]
        assert all(abs(p - round_half_away(c)) <= 1 for p, c in zip(parts, comps))


def test_bridge_that_does_not_tie_is_refused():
    with pytest.raises(ValueError):
        bridge(Fraction(0), [("a", Fraction(1))], Fraction(100))
