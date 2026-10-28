"""D4: refunds split into an unearned portion (contract liability) and a delivered portion (revenue).

The generator-side test runs now and checks the pro-rata annual refunds equal the unearned amount
under cumulative cent rounding, so the SQL split should put all of it against the contract liability.
The SQL-side test runs once sql/40_revenue.sql exists (Day 4). That file must create
refund_split(refund VARCHAR, unearned_cents BIGINT, delivered_cents BIGINT) and
revenue_by_line(line VARCHAR, month VARCHAR, recognised_cents BIGINT).
"""
import pytest

from generator.generate import round_half_up
from tests.conftest import ROOT

REVENUE_SQL = ROOT / "sql" / "40_revenue.sql"

EARLY_ANNUAL_REFUNDS = """
    SELECT r.id AS refund, r.amount, r.created AS refunded_at, s.ended_at, i.id AS invoice
    FROM raw.refunds r
    JOIN raw.charges c ON c.id = r.charge
    JOIN raw.invoices i ON i.id = c.invoice
    JOIN raw.subscriptions s ON s.id = i.subscription
    WHERE s.ended_at = r.created
      AND EXISTS (SELECT 1 FROM raw.invoice_line_items l JOIN raw.prices p ON p.id = l.price
                  WHERE l.invoice = i.id AND p.recurring_interval = 'year' AND l.period_end > s.ended_at)
"""


def test_pro_rata_annual_refund_equals_unearned_portion(con):
    refunds = con.execute(EARLY_ANNUAL_REFUNDS).fetchall()
    assert refunds, "scenario must contain at least one pro-rata annual refund"
    for refund, amount, _, ended_at, invoice in refunds:
        lines = con.execute("SELECT amount - discount_amount, period_start, period_end "
                            "FROM raw.invoice_line_items WHERE invoice = ?", [invoice]).fetchall()
        unearned = sum(net - round_half_up(net * (ended_at - ps), pe - ps) for net, ps, pe in lines)
        assert amount == unearned, refund


def test_service_credits_are_for_delivered_periods_only(con):
    # Every other refund is on an invoice whose service period ended before the refund.
    late = con.execute(f"""
        SELECT r.id FROM raw.refunds r JOIN raw.charges c ON c.id = r.charge
        JOIN raw.invoice_line_items l ON l.invoice = c.invoice
        WHERE r.id NOT IN (SELECT refund FROM ({EARLY_ANNUAL_REFUNDS}))
          AND l.period_end > r.created""").fetchall()
    assert late == []


@pytest.mark.skipif(not REVENUE_SQL.exists(), reason="sql/40_revenue.sql lands on Day 4")
def test_pro_rata_annual_refund_hits_contract_liability_not_revenue(con):
    con.execute(REVENUE_SQL.read_text())
    for refund, amount, _, ended_at, invoice in con.execute(EARLY_ANNUAL_REFUNDS).fetchall():
        unearned, delivered = con.execute(
            "SELECT unearned_cents, delivered_cents FROM refund_split WHERE refund = ?", [refund]).fetchone()
        assert (unearned, delivered) == (amount, 0)
        # Revenue for the delivered months is untouched: the line recognises exactly net - refund in total.
        recognised = con.execute("""
            SELECT sum(r.recognised_cents) FROM revenue_by_line r
            JOIN raw.invoice_line_items l ON l.id = r.line WHERE l.invoice = ?""", [invoice]).fetchone()[0]
        net = con.execute("SELECT amount_due FROM raw.invoices WHERE id = ?", [invoice]).fetchone()[0]
        assert recognised == net - amount
