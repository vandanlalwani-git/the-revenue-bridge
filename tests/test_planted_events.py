"""The scenario itself: each planted event must produce its signal in the simulation's data.

These checks read truth.* and raw.* directly, so they validate the answer key, not the SQL marts.
The metrics-layer checks against the marts come with the SQL (Days 3-5).
"""
import pytest

CUSTOMER_MONTHS = """
    CREATE OR REPLACE TEMP TABLE cm AS
    SELECT month, customer, sum(arr_cents) AS arr, bool_or(past_due) AS past_due
    FROM truth.mrr_month_end GROUP BY ALL HAVING sum(arr_cents) > 0
"""
MONTH = "strftime(to_timestamp({col}) AT TIME ZONE 'UTC', '%Y-%m')"


@pytest.fixture(scope="module")
def db(con):
    con.execute(CUSTOMER_MONTHS)
    return con


@pytest.fixture(scope="module")
def key(sim):
    return {e["id"]: e for e in sim.answer_key()["events"]}


def series(db, sql) -> dict:
    return dict(db.execute(sql).fetchall())


def trailing(values: dict, month: str, n: int = 3) -> float:
    months = sorted(m for m in values if m < month)[-n:]
    return sum(values.get(m, 0) for m in months) / n


def voluntary_churns(db, plan_like="%"):
    return series(db, f"""
        SELECT {MONTH.format(col='s.ended_at')} m, count(*) FROM raw.subscriptions s
        WHERE s.cancellation_reason = 'cancellation_requested' AND s.ended_at IS NOT NULL
          AND (SELECT pr.name FROM raw.invoice_line_items l JOIN raw.prices p ON p.id = l.price
               JOIN raw.products pr ON pr.id = p.product
               WHERE l.subscription = s.id AND l.amount > 0 ORDER BY l.period_start DESC LIMIT 1) LIKE '{plan_like}'
        GROUP BY 1""")


def test_e1_price_rise(db):
    migrated = db.execute("""
        SELECT count(DISTINCT l.subscription_item) FROM raw.invoice_line_items l
        JOIN raw.prices p ON p.id = l.price
        WHERE p.nickname = 'Starter monthly 2025-09' AND l.period_start < epoch(TIMESTAMP '2025-10-01')
          AND EXISTS (SELECT 1 FROM raw.invoice_line_items o JOIN raw.prices op ON op.id = o.price
                      WHERE o.subscription_item = l.subscription_item AND op.nickname = 'Starter monthly')""").fetchone()[0]
    assert migrated >= 50
    churn = voluntary_churns(db, "Starter")
    for month in ("2025-09", "2025-10"):
        assert churn.get(month, 0) > 1.5 * trailing(churn, "2025-09")


def test_e2_one_logo_dominates_churn(db, key):
    e2 = key["E2"]["details"]["customer"]
    churned = dict(db.execute("""
        SELECT o.customer, o.arr FROM cm o LEFT JOIN cm n ON n.customer = o.customer AND n.month = '2025-11'
        WHERE o.month = '2025-10' AND n.customer IS NULL""").fetchall())
    assert churned[e2] == max(churned.values())
    assert churned[e2] > 0.5 * sum(churned.values())


def test_e3_promo_cohort_cliff_and_discount_roll_off(db):
    cohort = db.execute("""
        WITH first AS (SELECT customer, min(month) AS cohort FROM cm GROUP BY 1)
        SELECT f.customer,
               (SELECT arr FROM cm WHERE customer = f.customer AND month = '2026-03'),
               (SELECT arr FROM cm WHERE customer = f.customer AND month = '2026-04')
        FROM first f WHERE f.cohort = '2026-01'""").fetchall()
    size = len(cohort)
    m2 = sum(1 for _, mar, _ in cohort if mar) / size
    m3 = sum(1 for _, _, apr in cohort if apr) / size
    assert m2 - m3 >= 0.20, (m2, m3)
    survivors = [(mar, apr) for _, mar, apr in cohort if mar and apr]
    assert sum(apr for _, apr in survivors) > 1.5 * sum(mar for mar, _ in survivors)


def test_e4_card_expiry(db):
    # Primary evidence: counts.
    entering = series(db, f"""
        WITH first_fail AS (SELECT invoice, min(created) AS t FROM raw.charges WHERE status = 'failed' GROUP BY 1)
        SELECT {MONTH.format(col='t')}, count(*) FROM first_fail GROUP BY 1""")
    assert entering["2026-02"] > 2.5 * trailing(entering, "2026-02")
    assert entering["2026-04"] < entering["2026-02"] / 2          # the organic April cluster is not a count spike
    in_dunning = series(db, "SELECT month, count(*) FROM cm WHERE past_due GROUP BY 1")
    assert in_dunning["2026-02"] > 3 * trailing(in_dunning, "2026-02")
    assert in_dunning.get("2026-04", 0) < in_dunning["2026-02"] / 2
    invol = series(db, f"""SELECT {MONTH.format(col='ended_at')}, count(*) FROM raw.subscriptions
                          WHERE cancellation_reason = 'payment_failed' GROUP BY 1""")
    for month in ("2026-02", "2026-03"):
        assert invol.get(month, 0) > 2 * trailing(invol, "2026-02")
    # Supporting evidence: at-risk MRR rises in February and is back to baseline at March month-end.
    at_risk = series(db, "SELECT month, sum(arr) FROM cm WHERE past_due GROUP BY 1")
    assert at_risk["2026-02"] > 3 * trailing(at_risk, "2026-02")
    assert at_risk.get("2026-03", 0) < at_risk["2026-02"] / 2


def test_e5_annual_push_billings(db):
    billings = series(db, f"SELECT {MONTH.format(col='finalized_at')}, sum(amount_due) FROM raw.invoices GROUP BY 1")
    assert billings["2026-03"] > 1.5 * trailing(billings, "2026-03")


def test_e6_large_refund_customer_stays(db, key):
    e6 = key["E6"]["details"]
    assert e6["amount"] >= 100_000
    arr = series(db, f"SELECT month, arr FROM cm WHERE customer = '{e6['customer']}'")
    assert arr["2026-03"] == arr["2026-04"] == arr["2026-06"]


def reactivations(db):
    return series(db, """
        SELECT n.month, count(*) FROM cm n
        WHERE NOT EXISTS (SELECT 1 FROM cm p WHERE p.customer = n.customer
                          AND p.month = strftime(strptime(n.month, '%Y-%m') - INTERVAL 1 MONTH, '%Y-%m'))
          AND EXISTS (SELECT 1 FROM cm e WHERE e.customer = n.customer AND e.month < n.month)
        GROUP BY 1""")


def test_e7_winback_reactivation_wave(db):
    react = reactivations(db)
    assert react["2026-05"] > 3 * max(trailing(react, "2026-05"), 1)


def test_e8_control_month_is_quiet(db):
    react = reactivations(db)
    assert react.get("2026-06", 0) <= 2 * max(trailing(react, "2026-05"), 1)
    at_risk = series(db, "SELECT month, sum(arr) FROM cm WHERE past_due GROUP BY 1")
    assert at_risk.get("2026-06", 0) < at_risk["2026-02"] / 3

