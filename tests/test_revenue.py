"""Day 4: revenue recognised by the second (definitions §4), the refund split (D4) and E5.

Monthly revenue is recomputed here in Python from the simulation's own rows, using the generator's
round_half_up, so the SQL is checked against an independent implementation of §4.2.
"""
from collections import defaultdict

import pytest

from generator.generate import round_half_up


@pytest.fixture(scope="module")
def months(marts):
    return marts.execute("SELECT month, prev_month_end, month_end FROM calendar ORDER BY month").fetchall()


@pytest.fixture(scope="module")
def revenue(marts):
    cur = marts.execute("SELECT * FROM revenue_month ORDER BY month")
    names = [d[0] for d in cur.description]
    return {r[0]: dict(zip(names, r)) for r in cur.fetchall()}


def recognised_to(net, ps, pe, service_end, d):
    return round_half_up(net * (min(max(d, ps), service_end) - ps), pe - ps)


def test_gross_revenue_matches_independent_python(sim, months, revenue):
    ended = {s["id"]: s["ended_at"] for s in sim.rows["subscriptions"]}
    expected = defaultdict(int)
    for ln in sim.rows["invoice_line_items"]:
        net = ln["amount"] - ln["discount_amount"]
        if net == 0:
            continue
        ps, pe = ln["period_start"], ln["period_end"]
        end = ended[ln["subscription"]]
        service_end = max(ps, min(pe, end if end is not None else pe))
        for month, prev_end, month_end in months:
            expected[month] += (recognised_to(net, ps, pe, service_end, month_end)
                                - recognised_to(net, ps, pe, service_end, prev_end))
    assert {m: r["gross_recognised_cents"] for m, r in revenue.items()} == dict(expected)


def test_each_line_recognises_exactly_its_earned_amount(marts):
    last = marts.execute("SELECT max(month_end) FROM calendar").fetchone()[0]
    bad = marts.execute("""
        SELECT t.line FROM revenue_line_totals t
        LEFT JOIN (SELECT line, sum(recognised_cents) AS cents FROM revenue_by_line GROUP BY 1) r USING (line)
        WHERE t.service_end <= ? AND coalesce(r.cents, 0) <> t.earned_cents
           OR t.earned_cents + t.unearned_cents <> t.net_amount""", [last]).fetchall()
    assert bad == []


def test_uncollectible_invoices_keep_delivered_revenue(marts):
    # D5 and §4.4: revenue for dunning days is earned and never reversed; only the days after ended_at are unearned.
    rows = marts.execute("""
        SELECT t.invoice, sum(t.earned_cents), sum(r.cents), sum(t.unearned_cents)
        FROM revenue_line_totals t
        JOIN (SELECT line, sum(recognised_cents) AS cents FROM revenue_by_line GROUP BY 1) r USING (line)
        WHERE t.invoice_status = 'uncollectible' GROUP BY 1""").fetchall()
    assert len(rows) >= 20
    for invoice, earned, recognised, unearned in rows:
        assert recognised == earned > 0 and unearned > 0, invoice
    assert marts.execute("SELECT count(*) FROM revenue_by_line WHERE recognised_cents < 0 "
                         "AND line IN (SELECT line FROM revenue_lines WHERE net_amount > 0)").fetchone()[0] == 0


# ---- refunds (D4) --------------------------------------------------------------
def split(marts, refund):
    return marts.execute("SELECT unearned_cents, delivered_cents FROM refund_split WHERE refund = ?",
                         [refund]).fetchone()


def test_e6_service_credit_is_all_delivered(marts, sim):
    e6 = sim.key["E6_large_refund"]
    assert split(marts, e6["refund"]) == (0, e6["amount"])


def test_refunds_on_live_subscriptions_are_delivered(marts):
    bad = marts.execute("""
        SELECT r.refund FROM refund_split r
        JOIN raw.invoices i ON i.id = r.invoice JOIN stg_subscriptions s ON s.subscription = i.subscription
        WHERE (s.ended_at IS NULL OR s.ended_at > r.created) AND r.unearned_cents <> 0""").fetchall()
    assert bad == []


def test_refund_split_adds_up_and_only_delivered_reduces_revenue(revenue, sim):
    for month, r in revenue.items():
        assert r["refunds_delivered_cents"] + r["refunds_unearned_cents"] == r["refunds_total_cents"], month
        assert r["net_revenue_cents"] == r["gross_recognised_cents"] - r["refunds_delivered_cents"], month
    assert revenue["2026-04"]["refunds_delivered_cents"] >= sim.key["E6_large_refund"]["amount"]
    assert sum(r["refunds_unearned_cents"] for r in revenue.values()) > 0     # D4 is exercised somewhere


# ---- E5 at the metrics layer -------------------------------------------------------
def trailing(revenue, field, month, n=3):
    prior = sorted(m for m in revenue if m < month)[-n:]
    return sum(revenue[m][field] for m in prior) / n


def test_e5_cash_far_above_revenue_deferred_up_mrr_down(marts, revenue, sim):
    march = revenue["2026-03"]
    assert march["net_cash_cents"] > 2 * march["net_revenue_cents"]
    assert march["billings_cents"] > 2 * march["net_revenue_cents"]
    cl = {m: r["contract_liability_close_cents"] for m, r in revenue.items()}
    jump = cl["2026-03"] - cl["2026-02"]
    usual = max(abs(cl[a] - cl[b]) for a, b in (("2025-12", "2025-11"), ("2026-01", "2025-12"), ("2026-02", "2026-01")))
    assert jump > 0.5 * march["net_revenue_cents"] and jump > 3 * usual
    # MRR down for the switchers: each one's MRR falls to 10/12, the monthly -> annual contraction (§1.11).
    switched = sim.key["E5_annual_push"]["subscriptions"]
    rows = marts.execute("""
        SELECT f.subscription, f.arr_cents, t.arr_cents FROM mrr_month_end f
        JOIN mrr_month_end t ON t.subscription = f.subscription AND t.month = '2026-03'
        WHERE f.month = '2026-02' AND f.subscription IN (SELECT unnest(?))""", [switched]).fetchall()
    assert len(rows) >= 0.9 * len(switched)
    assert sum(t for _, _, t in rows) < sum(f for _, f, _ in rows)
    assert sum(1 for _, f, t in rows if t * 12 == f * 10) >= 0.9 * len(rows)
    contraction = marts.execute("SELECT arr_delta_cents FROM mrr_bridge_drivers WHERE month = '2026-03' "
                                "AND driver = 'interval_switch' AND movement = 'contraction'").fetchone()[0]
    assert contraction < 0
