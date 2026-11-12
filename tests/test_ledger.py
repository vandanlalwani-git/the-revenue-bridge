"""Gate 3: the three reconciliations and the ledger rollforwards tie to the cent every month.

A tie between SQL tables can hide a shared mistake, so the balances, the refund split and the
IFRS 9 provision matrix are also recomputed here in Python, straight from the simulation's rows.
"""
from collections import defaultdict
from fractions import Fraction

import pytest

from generator.generate import round_half_up
from pipeline.display import bridge, cents_to_dollars

UNEXPLAINED = ["r1_unexplained", "r2_unexplained_cents", "r3_unexplained_cents", "cl_unexplained_cents",
               "ar_unexplained_cents", "ecl_unexplained_cents", "write_off_sources_differ_cents"]


@pytest.fixture(scope="module")
def recon(marts):
    cur = marts.execute("SELECT * FROM reconciliation_month ORDER BY month")
    names = [d[0] for d in cur.description]
    return {r[0]: dict(zip(names, r)) for r in cur.fetchall()}


@pytest.fixture(scope="module")
def month_ends(marts):
    return marts.execute("SELECT month, prev_month_end, month_end FROM calendar ORDER BY month").fetchall()


def test_every_unexplained_line_is_zero_every_month(recon):
    assert len(recon) == 30
    bad = {(m, col): r[col] for m, r in recon.items() for col in UNEXPLAINED if r[col] != 0}
    assert bad == {}


# ---- independent recomputation in Python ------------------------------------------
def recognised_to(net, ps, pe, service_end, d):
    return round_half_up(net * (min(max(d, ps), service_end) - ps), pe - ps)


@pytest.fixture(scope="module")
def books(sim):
    """Invoice-level facts derived only from the simulation's Stripe-shaped rows."""
    subs = {s["id"]: s for s in sim.rows["subscriptions"]}
    lines = defaultdict(list)
    for ln in sim.rows["invoice_line_items"]:
        net = ln["amount"] - ln["discount_amount"]
        if net:
            end = subs[ln["subscription"]]["ended_at"]
            ps, pe = ln["period_start"], ln["period_end"]
            lines[ln["invoice"]].append((net, ps, pe, max(ps, min(pe, end if end is not None else pe))))
    fails = defaultdict(list)
    for ch in sorted(sim.rows["charges"], key=lambda c: c["created"]):
        if ch["status"] == "failed":
            fails[ch["invoice"]].append(ch["created"])
    invoices = {}
    for inv in sim.rows["invoices"]:
        ls = lines[inv["id"]]
        earned = sum(recognised_to(n, ps, pe, se, se) for n, ps, pe, se in ls)
        invoices[inv["id"]] = dict(inv, lines=ls, earned=earned, unearned=sum(n for n, *_ in ls) - earned,
                                   fails=fails.get(inv["id"], []))
    charge_invoice = {c["id"]: c["invoice"] for c in sim.rows["charges"]}
    unearned_refunds, used = [], defaultdict(int)
    for re in sorted(sim.rows["refunds"], key=lambda r: (r["created"], r["id"])):
        inv = invoices[charge_invoice[re["charge"]]]
        ended = max((se for n, ps, pe, se in inv["lines"] if se < pe), default=None)
        available = inv["unearned"] if ended is not None and ended <= re["created"] else 0
        take = min(re["amount"], max(available - used[inv["id"]], 0))
        if ended is not None and ended <= re["created"]:
            used[inv["id"]] += re["amount"]
        unearned_refunds.append((re["created"], take))
    return invoices, unearned_refunds


def open_receivable(inv, at):
    return (inv["amount_due"] > 0 and inv["finalized_at"] <= at
            and (inv["paid_at"] is None or inv["paid_at"] > at)
            and (inv["marked_uncollectible_at"] is None or inv["marked_uncollectible_at"] > at))


def test_contract_liability_balance_matches_python(books, recon, month_ends):
    invoices, unearned_refunds = books
    for month, _, e in month_ends:
        on_lines = sum(n - recognised_to(n, ps, pe, se, e)
                       for inv in invoices.values() if inv["finalized_at"] <= e for n, ps, pe, se in inv["lines"])
        written_off = sum(inv["unearned"] for inv in invoices.values()
                          if inv["status"] == "uncollectible" and inv["marked_uncollectible_at"] <= e)
        refunded = sum(take for at, take in unearned_refunds if at <= e)
        assert recon[month]["cl_close_cents"] == on_lines - written_off - refunded, month
        assert recon[month]["cl_close_cents"] >= 0


def test_receivables_balance_matches_python(books, recon, month_ends):
    invoices, _ = books
    for month, _, e in month_ends:
        expected = sum(inv["amount_due"] for inv in invoices.values() if open_receivable(inv, e))
        assert recon[month]["ar_close_cents"] == expected, month


def test_provision_matrix_matches_python(books, marts, month_ends):
    # Stage = failed card attempts by the month-end (0 = current, capped at 3). Own trailing rate;
    # an empty failed stage borrows the pooled failed-stage rate; then the ageing floor.
    invoices, _ = books
    ends = [e for _, _, e in month_ends]
    observed = []                      # (month-end, stage, balance, resolved_at, loss)
    for e in ends:
        for inv in invoices.values():
            if open_receivable(inv, e):
                stage = min(sum(1 for t in inv["fails"] if t <= e), 3)
                resolved = min(t for t in (inv["paid_at"], inv["marked_uncollectible_at"], float("inf")) if t is not None)
                loss = inv["earned"] if inv["status"] == "uncollectible" else 0
                observed.append((e, stage, inv["amount_due"], resolved, loss))
    sql = dict(marts.execute("SELECT month, allowance_close_cents FROM ecl_month").fetchall())
    # Applied rates are compared too, not just the allowance: a fallback or floor rate on a
    # bucket that happens to be empty that month would not move the allowance.
    sql_rates = {(m, s): Fraction(lo, ob) if ob else Fraction(0) for m, s, lo, ob in marts.execute(
        "SELECT month, stage, rate_loss_cents, rate_observed_cents FROM ecl_rates").fetchall()}
    floor_used = pooled_used = False
    for i, (month, prev_end, e) in enumerate(month_ends):
        first = ends[i - 12] if i >= 12 else 0
        window = [o for o in observed if first <= o[0] <= prev_end and o[3] <= prev_end]
        own = {s: (sum(o[4] for o in window if o[1] == s), sum(o[2] for o in window if o[1] == s)) for s in range(4)}
        pooled = (sum(own[s][0] for s in (1, 2, 3)), sum(own[s][1] for s in (1, 2, 3)))
        base = {s: own[s] if own[s][1] else (pooled if s else (0, 0)) for s in range(4)}
        pooled_used |= any(s and not own[s][1] and pooled[1] for s in range(4))
        ratio = lambda pair: Fraction(pair[0], pair[1]) if pair[1] else Fraction(0)
        allowance = 0
        for s in range(4):
            # highest base rate at or before stage s; on a tie, the later stage
            pick = max(range(s + 1), key=lambda k: (ratio(base[k]), k))
            floor_used |= ratio(base[pick]) != ratio(base[s])
            assert sql_rates[(month, s)] == ratio(base[pick]), (month, s)
            loss, obs = base[pick]
            balance = sum(o[2] for o in observed if o[0] == e and o[1] == s)
            allowance += round_half_up(balance * loss, obs) if obs else 0
        assert sql[month] == allowance, month
    assert floor_used and pooled_used  # both rules are exercised by this dataset, so a broken one would show


def test_daily_mrr_agrees_with_month_end_mrr(marts):
    # 45_mrr_daily.sql repeats 10_mrr.sql's governing-line rule; this keeps the two copies identical.
    diff = marts.execute("""
        SELECT m.month, m.subscription_item FROM mrr_item_month_end m
        FULL JOIN (SELECT d.month, d.subscription_item, d.arr_cents FROM mrr_item_daily d
                   JOIN calendar c ON c.month_end = d.snapshot_at) d USING (month, subscription_item)
        WHERE m.arr_cents IS DISTINCT FROM d.arr_cents""").fetchall()
    assert diff == []
    days = dict(marts.execute("SELECT month, days FROM mrr_daily_month").fetchall())
    assert (days["2024-02"], days["2025-02"], days["2026-03"], days["2026-04"]) == (29, 28, 31, 30)


def test_allowance_never_exceeds_receivables(recon):
    for month, r in recon.items():
        assert 0 <= r["allowance_close_cents"] <= r["ar_close_cents"], month


# ---- planted events at the ledger layer ---------------------------------------------
def test_e4_bad_debt_expense_rises(recon):
    expense = {m: r["bad_debt_expense_cents"] for m, r in recon.items()}
    trailing = sum(expense[m] for m in ("2025-11", "2025-12", "2026-01")) / 3
    assert expense["2026-02"] + expense["2026-03"] > 3 * max(trailing, 1)


# ---- display (definitions §7) ---------------------------------------------------------
def r1_fraction(r, col):
    return Fraction(r[col], r["r1_denominator"]) / 100


def test_displayed_reconciliations_add_up(recon):
    for month, r in recon.items():
        r1 = bridge(r1_fraction(r, "r1_closing_mrr"),
                    [("timing", -r1_fraction(r, "r1_timing")),
                     ("daycount_proration", r1_fraction(r, "r1_daycount_proration")),
                     ("once_coupons", -r1_fraction(r, "r1_once_coupons")),
                     ("refunds_delivered", -r1_fraction(r, "r1_refunds_delivered"))],
                    r1_fraction(r, "r1_net_revenue"))
        r2 = bridge(cents_to_dollars(r["net_revenue_cents"]),
                    [(k, cents_to_dollars(r[k])) for k in ("refunds_delivered_cents", "cl_change_cents",
                                                            "unearned_written_off_cents", "refunds_unearned_cents")],
                    cents_to_dollars(r["billings_cents"]))
        r3 = bridge(cents_to_dollars(r["billings_cents"]),
                    [(k, -cents_to_dollars(r[k])) for k in ("ar_change_cents", "earned_written_off_cents",
                                                             "unearned_written_off_cents", "refunds_total_cents")],
                    cents_to_dollars(r["net_cash_cents"]))
        for shown in (r1, r2, r3):
            parts = [v for k, v in shown.items() if k not in ("opening", "closing")]
            assert shown["opening"] + sum(parts) == shown["closing"], month


def test_empty_stage_borrows_pooled_rate_on_a_made_up_book():
    # In the frozen data the pooled fallback only fires in 2024, at a 0% rate, so it never moves the
    # allowance. This runs the matrix SQL on a tiny book where it does.
    import duckdb
    from pipeline.run import SQL_DIR
    con = duckdb.connect()
    staging = (SQL_DIR / "00_staging.sql").read_text()
    for stmt in staging.split(";"):
        if stmt.strip().startswith("CREATE OR REPLACE MACRO") or "\nCREATE OR REPLACE MACRO" in stmt:
            con.execute(stmt[stmt.index("CREATE OR REPLACE MACRO"):])
    ledger = (SQL_DIR / "50_ledger.sql").read_text()
    buckets = ledger[ledger.index("CREATE OR REPLACE TABLE ecl_buckets"):ledger.index("-- Gross receivables")]
    con.execute(buckets)
    con.execute("CREATE TABLE calendar AS SELECT * FROM (VALUES ('m1', 0, 100), ('m2', 100, 200), ('m3', 200, 300)) "
                "AS t(month, prev_month_end, month_end)")
    # A: failed_2 at m1, written off at 150 with 200 earned. B: failed_3 at m1, paid at 150.
    # C: failed_1 at m3, still open. failed_1 has no history, so it borrows the pooled rate 200 / 2000.
    con.execute("""CREATE TABLE ledger_invoices AS SELECT * FROM (VALUES
        ('A', 'uncollectible', NULL, 150, 200), ('B', 'paid', 150, NULL, 1000), ('C', 'open', NULL, NULL, 0))
        AS t(invoice, status, paid_at, marked_uncollectible_at, earned_cents)""")
    con.execute("""CREATE TABLE receivables_month_end AS SELECT * FROM (VALUES
        ('m1', 100, 'A', 1000, 'failed_2'), ('m1', 100, 'B', 1000, 'failed_3'), ('m3', 300, 'C', 500, 'failed_1'))
        AS t(month, month_end, invoice, balance_cents, bucket)""")
    con.execute(ledger[ledger.index("CREATE OR REPLACE TABLE ecl_rates"):])
    rates = {b: (src, frm, lo, ob) for b, src, frm, lo, ob in con.execute(
        "SELECT bucket, base_source, rate_from, rate_loss_cents, rate_observed_cents FROM ecl_rates WHERE month = 'm3'").fetchall()}
    assert rates["failed_1"] == ("pooled", "failed_1", 200, 2000)
    assert rates["failed_2"] == ("own", "failed_2", 200, 1000)
    assert rates["failed_3"] == ("own", "failed_2", 200, 1000)      # floored up from 0% to failed_2's 20%
    allowance = con.execute("SELECT allowance_close_cents FROM ecl_month WHERE month = 'm3'").fetchone()[0]
    assert allowance == 50                                            # 500 x 10%
