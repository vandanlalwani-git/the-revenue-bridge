"""Day 4: churn and retention rates (definitions §3) and cohorts, checked against truth recomputed in Python."""
from collections import defaultdict

import pytest


@pytest.fixture(scope="module")
def paying(sim):
    arr = defaultdict(lambda: defaultdict(int))
    for r in sim.truth["mrr_month_end"]:
        arr[r["month"]][r["customer"]] += r["arr_cents"]
    return {m: {c: a for c, a in cs.items() if a > 0} for m, cs in sorted(arr.items())}


@pytest.fixture(scope="module")
def retention(marts):
    cur = marts.execute("SELECT * FROM retention_month ORDER BY month")
    names = [d[0] for d in cur.description]
    return {r[0]: dict(zip(names, r)) for r in cur.fetchall()}


def test_logo_churn_numerator_and_denominator(paying, retention):
    months = list(paying)
    for prev, month in zip(months, months[1:]):
        opening = paying[prev]
        churned = sum(1 for c in opening if c not in paying[month])
        assert (retention[month]["churned_customers"], retention[month]["opening_customers"]) == (churned, len(opening))


def test_nrr_and_grr_against_truth(paying, retention):
    months = list(paying)
    for i, month in enumerate(months):
        r = retention[month]
        if i < 12:
            assert r["nrr_base_arr_cents"] is None
            continue
        base = paying[months[i - 12]]
        now = paying[month]
        assert r["nrr_base_customers"] == len(base)
        assert r["nrr_base_arr_cents"] == sum(base.values())
        assert r["nrr_arr_cents"] == sum(now.get(c, 0) for c in base)
        assert r["grr_arr_cents"] == sum(min(now.get(c, 0), a) for c, a in base.items())
        assert r["grr_arr_cents"] <= r["nrr_arr_cents"] and r["grr_arr_cents"] <= r["nrr_base_arr_cents"]
    assert sum(1 for m in months if retention[m]["nrr_base_arr_cents"]) == 18   # every reporting month


def test_gross_mrr_churn_inputs_come_from_the_bridge(marts):
    bad = marts.execute("""
        SELECT r.month FROM retention_month r JOIN mrr_bridge b USING (month)
        WHERE r.opening_arr_cents <> b.opening_arr_cents
           OR r.churn_arr_cents <> b.churn_voluntary_arr_cents + b.churn_involuntary_arr_cents
           OR r.contraction_arr_cents <> b.contraction_arr_cents""").fetchall()
    assert bad == []


# ---- cohorts -----------------------------------------------------------------
def test_every_paying_customer_has_one_cohort(marts):
    ever_paid, in_cohorts, distinct = marts.execute("""
        SELECT (SELECT count(DISTINCT customer) FROM stg_lines WHERE amount > 0 AND NOT proration),
               (SELECT count(*) FROM customer_cohort), (SELECT count(DISTINCT customer) FROM customer_cohort)""").fetchone()
    assert ever_paid == in_cohorts == distinct


def test_cohort_counts_are_bounded_and_m0_is_nearly_full(marts):
    assert marts.execute("SELECT count(*) FROM cohort_retention WHERE retained_customers > cohort_customers").fetchone()[0] == 0
    worst_m0 = marts.execute("SELECT min(retained_customers / cohort_customers) FROM cohort_retention "
                             "WHERE k = 0").fetchone()[0]
    assert 0.9 <= worst_m0 <= 1          # below 1 only for customers who paid and left inside one month


def cohort(marts, name):
    return {k: (n, size, arr, base) for k, n, size, arr, base in marts.execute(
        "SELECT k, retained_customers, cohort_customers, arr_cents, m0_arr_cents FROM cohort_retention "
        "WHERE cohort = ? ORDER BY k", [name]).fetchall()}


def test_e3_cliff_at_m3_and_revenue_retention_above_logo(marts):
    c = cohort(marts, "2026-01")
    logo = {k: n / size for k, (n, size, _, _) in c.items()}
    revenue = {k: arr / base for k, (_, _, arr, base) in c.items()}
    assert logo[2] - logo[3] >= 0.20, logo
    # Discount roll-off: survivors pay full price from M3, so revenue retention sits above logo retention.
    assert revenue[3] > logo[3]


def test_e2_gross_mrr_churn_far_above_logo_churn(retention):
    r = retention["2025-11"]
    gross = (r["churn_arr_cents"] + r["contraction_arr_cents"]) / r["opening_arr_cents"]
    logo = r["churned_customers"] / r["opening_customers"]
    assert gross > 2 * logo


# ---- planted counts at the metrics layer -------------------------------------
def by_month(marts, sql):
    return dict(marts.execute(sql).fetchall())


def trailing(values, month, n=3):
    prior = sorted(m for m in values if m < month)[-n:]
    return sum(values.get(m, 0) for m in prior) / n


def test_e1_starter_voluntary_churn(marts):
    churn = by_month(marts, """
        SELECT m.month, count(*) FROM mrr_movements m
        JOIN mrr_item_month_end i ON i.customer = m.customer AND NOT i.is_seat
         AND i.month = strftime(strptime(m.month, '%Y-%m') - INTERVAL 1 MONTH, '%Y-%m')
        WHERE m.movement = 'churn' AND m.churn_type = 'voluntary' AND i.product = 'Starter'
        GROUP BY 1""")
    for month in ("2025-09", "2025-10"):
        assert churn[month] > 1.5 * trailing(churn, "2025-09")


def test_e4_counts_at_the_metrics_layer(marts):
    invol = by_month(marts, "SELECT month, churned_involuntary_customers FROM mrr_bridge")
    for month in ("2026-02", "2026-03"):
        assert invol[month] > 2 * trailing(invol, "2026-02")
    in_dunning = by_month(marts, "SELECT month, count(*) FROM customer_mrr WHERE past_due GROUP BY 1")
    assert in_dunning["2026-02"] > 3 * trailing(in_dunning, "2026-02")
    assert in_dunning.get("2026-04", 0) < in_dunning["2026-02"] / 2
