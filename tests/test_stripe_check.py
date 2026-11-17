"""The Stripe test-clock check: the unchanged sql/*.sql run on objects Stripe itself created.

stripe_check/run_test_clock.py played 4 months (Jan-Apr 2026) for 6 customers on Stripe test clocks
and exported them to stripe_check/data/raw. Expected values below come from the scenario's own steps
(prices x quantities, by hand), not from the SQL. Pinecrest's cancellation date is Stripe's: its
retry schedule gave up and cancelled the subscription, which is the point of using real Stripe.
"""
from pathlib import Path

import pytest

from pipeline.run import connect, build

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "stripe_check" / "data"

pytestmark = pytest.mark.skipif(not (DATA / "raw" / "invoices.parquet").exists(), reason="no Stripe export")

# ARR cents (MRR x 12) at each month-end, from the scenario steps.
STARTER, GROWTH, SCALE, SEAT = 2900, 9900, 39900, 3000
EXPECTED_ARR = {
    "2026-01": {"Northwind Labs": STARTER * 12, "Bluefin Analytics": 99000, "Kestrel Studio": STARTER * 12,
                "Orchid Health": (SCALE + 3 * SEAT) * 12, "Pinecrest Co": GROWTH * 12,
                "Quill & Co": STARTER * 80 // 100 * 12},
    "2026-02": {"Northwind Labs": GROWTH * 12, "Bluefin Analytics": 99000, "Kestrel Studio": STARTER * 12,
                "Orchid Health": (SCALE + 3 * SEAT) * 12, "Quill & Co": STARTER * 80 // 100 * 12},
    "2026-03": {"Northwind Labs": GROWTH * 12, "Bluefin Analytics": 99000,
                "Orchid Health": (SCALE + 5 * SEAT) * 12, "Quill & Co": STARTER * 80 // 100 * 12},
}
EXPECTED_ARR["2026-04"] = EXPECTED_ARR["2026-03"]


@pytest.fixture(scope="module")
def db():
    con = connect(DATA)
    build(con)
    yield con
    con.close()


def by_name(db, sql):
    return db.execute(sql.replace("{name}", "(SELECT name FROM raw.customers c WHERE c.id = customer)")).fetchall()


def test_export_is_stripe_test_mode_data(db):
    ids = db.execute("SELECT min(id), max(id) FROM raw.invoices").fetchone()
    assert all(i.startswith("in_") for i in ids)
    assert db.execute("SELECT count(*) FROM raw.customers").fetchone()[0] == 6
    assert db.execute("SELECT list(DISTINCT month ORDER BY month) FROM calendar").fetchone()[0] == list(EXPECTED_ARR)


def test_every_unexplained_line_is_zero(db):
    cols = [c[0] for c in db.execute("DESCRIBE reconciliation_month").fetchall() if c[0].endswith("unexplained")
            or c[0].endswith("unexplained_cents")]
    assert len(cols) == 6
    rows = db.execute(f"SELECT month, {', '.join(cols)} FROM reconciliation_month ORDER BY month").fetchall()
    assert [r[0] for r in rows] == list(EXPECTED_ARR)
    for r in rows:
        assert all(v == 0 for v in r[1:]), dict(zip(["month", *cols], r))
    assert db.execute("SELECT count(*) FROM mrr_bridge WHERE unexplained_arr_cents <> 0").fetchone()[0] == 0


def test_mrr_matches_the_scenario(db):
    got = {}
    for month, name, arr in by_name(db, "SELECT month, {name}, arr_cents FROM customer_mrr"):
        got.setdefault(month, {})[name] = arr
    assert got == EXPECTED_ARR


def test_movements_and_churn_types(db):
    moves = {(m, n): (mv, ct) for m, n, mv, ct in by_name(
        db, "SELECT month, {name}, movement, churn_type FROM mrr_movements WHERE movement <> 'flat'")}
    assert moves == {
        **{("2026-01", n): ("new", None) for n in EXPECTED_ARR["2026-01"]},
        ("2026-02", "Northwind Labs"): ("expansion", None),
        ("2026-02", "Pinecrest Co"): ("churn", "involuntary"),     # Stripe cancelled it: payment_failed
        ("2026-03", "Orchid Health"): ("expansion", None),
        ("2026-03", "Kestrel Studio"): ("churn", "voluntary"),     # cancel at period end
    }
    drivers = {(m, n): d for m, n, d in by_name(db, "SELECT month, {name}, driver FROM mrr_drivers")}
    assert drivers == {("2026-02", "Northwind Labs"): "plan_change", ("2026-03", "Orchid Health"): "quantity_change"}


def test_stripe_cancelled_pinecrest_after_its_retries(db):
    status, reason, ended, failures = db.execute("""
        SELECT s.status, s.cancellation_reason, month_of(s.ended_at),
               (SELECT count(*) FROM raw.charges ch WHERE ch.customer = s.customer AND ch.status = 'failed')
        FROM raw.subscriptions s JOIN raw.customers c ON c.id = s.customer WHERE c.name = 'Pinecrest Co'""").fetchone()
    assert (status, reason, ended) == ("canceled", "payment_failed", "2026-02")
    assert failures >= 4


def test_refund_is_a_service_credit(db):
    rows = by_name(db, "SELECT month, {name}, amount_cents, delivered_cents, unearned_cents FROM refund_split")
    assert rows == [("2026-02", "Bluefin Analytics", 15000, 15000, 0)]


def test_quill_recovered_and_pinecrest_written_off(db):
    # Quill's card failed on 10 Feb and was fixed on 13 Feb: a dunning spell, but nothing open at Feb-end.
    spells = by_name(db, "SELECT {name}, month_of(first_failed_at), month_of(resolved_at) FROM stg_dunning")
    assert sorted(spells) == [("Pinecrest Co", "2026-02", "2026-03"), ("Quill & Co", "2026-02", "2026-02")]
    feb = db.execute("SELECT bucket, balance_cents FROM receivables_month_end WHERE month = '2026-02'").fetchall()
    assert feb == [("failed_3", GROWTH)]                       # Pinecrest's open invoice, 3+ failures
    earned, unearned = db.execute("""
        SELECT earned_written_off_cents, unearned_written_off_cents FROM revenue_month WHERE month = '2026-03'
    """).fetchone()
    assert earned + unearned == GROWTH and 0 < earned < GROWTH  # served to 15 Feb: loss; after: never revenue
    assert db.execute("SELECT count(*) FROM receivables_month_end WHERE month >= '2026-03'").fetchone()[0] == 0
