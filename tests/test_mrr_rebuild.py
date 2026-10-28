"""D1: MRR rebuilt from invoice line items must equal the simulation's true subscription-state MRR.

The coverage tests run now and prove the data contains every case the rebuild must handle.
The equality test runs once sql/10_mrr.sql exists (Day 3). That file must create
mrr_month_end(month VARCHAR, customer VARCHAR, arr_cents BIGINT), where arr_cents = MRR x 12.
"""
from pathlib import Path

import pytest

from tests.conftest import ROOT

MRR_SQL = ROOT / "sql" / "10_mrr.sql"

# Month-ends as epoch seconds, from the truth table.
MONTH_ENDS = "(SELECT DISTINCT month_end AS e FROM truth.mrr_month_end)"


def count(con, sql):
    return con.execute(sql).fetchone()[0]


def test_covers_mid_period_upgrade_across_month_end(con):
    # An upgrade's proration charge line covers a month-end with a different price from its credit line:
    # the rebuild must take the new price from that line, not its prorated amount.
    n = count(con, f"""
        SELECT count(*) FROM raw.invoice_line_items charge
        JOIN raw.invoice_line_items credit
          ON credit.invoice = charge.invoice AND credit.subscription_item = charge.subscription_item
         AND credit.amount < 0 AND credit.price <> charge.price
        JOIN {MONTH_ENDS} me ON me.e >= charge.period_start AND me.e < charge.period_end
        WHERE charge.proration AND charge.amount > 0""")
    assert n >= 3


def test_covers_seat_increase_across_month_end(con):
    n = count(con, f"""
        SELECT count(*) FROM raw.invoice_line_items l
        JOIN raw.prices p ON p.id = l.price JOIN raw.products pr ON pr.id = p.product
        JOIN {MONTH_ENDS} me ON me.e >= l.period_start AND me.e < l.period_end
        WHERE l.proration AND l.amount > 0 AND pr.name LIKE '%seat%'""")
    assert n >= 3


def test_covers_cancel_at_period_end_with_month_end_in_final_period(con):
    # Cancellation requested, then a month-end passes while the customer is still paying.
    n = count(con, f"""
        SELECT count(*) FROM raw.subscriptions s
        JOIN {MONTH_ENDS} me ON me.e >= s.canceled_at AND me.e < s.ended_at
        WHERE s.cancel_at_period_end AND s.cancellation_reason = 'cancellation_requested'""")
    assert n >= 10


def test_covers_annual_plans(con):
    n = count(con, f"""
        SELECT count(DISTINCT l.subscription) FROM raw.invoice_line_items l
        JOIN raw.prices p ON p.id = l.price
        JOIN {MONTH_ENDS} me ON me.e >= l.period_start AND me.e < l.period_end
        WHERE p.recurring_interval = 'year' AND l.amount > 0""")
    assert n >= 30


def test_covers_early_annual_termination(con):
    n = count(con, """
        SELECT count(*) FROM raw.subscriptions s
        JOIN raw.invoice_line_items l ON l.subscription = s.id
        WHERE s.ended_at < l.period_end AND s.cancellation_reason = 'cancellation_requested'""")
    assert n >= 1


def test_covers_repeating_discount_ending(con):
    n = count(con, """
        SELECT count(*) FROM raw.discounts d JOIN truth.mrr_month_end t
          ON t.subscription = d.subscription AND t.month_end >= d."end"
        WHERE d.coupon = 'PROMO50'""")
    assert n >= 10


def test_covers_dunning_and_trials_at_month_end(con):
    assert count(con, "SELECT count(*) FROM truth.mrr_month_end WHERE past_due") >= 10
    n = count(con, f"""
        SELECT count(*) FROM raw.subscriptions s
        JOIN {MONTH_ENDS} me ON me.e >= s.trial_start AND me.e < s.trial_end""")
    assert n >= 10


@pytest.mark.skipif(not MRR_SQL.exists(), reason="sql/10_mrr.sql lands on Day 3")
def test_rebuilt_mrr_equals_truth_for_every_customer_and_month(con):
    con.execute(MRR_SQL.read_text())
    diff = con.execute("""
        WITH truth AS (SELECT month, customer, sum(arr_cents) AS arr FROM truth.mrr_month_end
                       GROUP BY ALL HAVING sum(arr_cents) > 0),
             rebuilt AS (SELECT month, customer, sum(arr_cents) AS arr FROM mrr_month_end
                         GROUP BY ALL HAVING sum(arr_cents) > 0)
        SELECT coalesce(t.month, r.month), coalesce(t.customer, r.customer), t.arr, r.arr
        FROM truth t FULL JOIN rebuilt r USING (month, customer)
        WHERE t.arr IS DISTINCT FROM r.arr ORDER BY 1, 2 LIMIT 20""").fetchall()
    assert diff == []
