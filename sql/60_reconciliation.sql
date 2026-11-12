-- 60_reconciliation.sql
-- ============================================================================
-- HEADER
--   What it does: the three reconciliations, each ending in an "unexplained"
--     line that must be $0.00. R1: MRR -> net revenue. R2: net revenue ->
--     billings. R3: billings -> net cash. Also rolls contract liability,
--     receivables and the allowance from opening to closing (opening + additions
--     - reductions = closing), each with its own unexplained line.
--   Judgement call: balances come from 50_ledger.sql and movements from
--     40_revenue.sql, built separately, so a zero means they genuinely agree.
--     R1's "day count and proration" line is worked out for each subscription
--     item, not backed into as whatever is left over.
--   Edge case: average MRR is a fraction of a cent, so R1 is kept as exact
--     whole numbers over a shared bottom number (12 x days in the month) and is
--     only rounded on the page.
-- ============================================================================

-- R1 item level: revenue on the MRR basis (before one-off coupons) against the item's daily ARR.
CREATE OR REPLACE TABLE r1_items AS
WITH revenue_items AS (
    SELECT b.month, l.subscription_item,
           sum(b.recognised_cents
               + CASE WHEN r.coupon_duration = 'once'
                      THEN round_half_up(b.recognised_cents * r.discount_amount, r.net_amount) ELSE 0 END) AS mrr_basis_cents
    FROM revenue_by_line b
    JOIN revenue_lines r USING (line)
    JOIN stg_lines l USING (line)
    GROUP BY b.month, l.subscription_item
),
mrr_items AS (
    SELECT month, subscription_item, sum(arr_cents) AS sum_daily_arr_cents
    FROM mrr_item_daily GROUP BY month, subscription_item
)
SELECT coalesce(r.month, m.month) AS month,
       coalesce(r.subscription_item, m.subscription_item) AS subscription_item,
       coalesce(r.mrr_basis_cents, 0)     AS mrr_basis_cents,
       coalesce(m.sum_daily_arr_cents, 0) AS sum_daily_arr_cents
FROM revenue_items r
FULL JOIN mrr_items m ON m.month = r.month AND m.subscription_item = r.subscription_item;

CREATE OR REPLACE TABLE reconciliation_month AS
WITH cl AS (
    SELECT month, balance_cents AS close_cents,
           coalesce(lag(balance_cents) OVER (ORDER BY month), 0) AS open_cents
    FROM contract_liability_month_end
),
ar_close AS (
    SELECT c.month, coalesce(sum(r.balance_cents), 0) AS close_cents
    FROM calendar c LEFT JOIN receivables_month_end r USING (month)
    GROUP BY c.month
),
ar AS (
    SELECT month, close_cents, coalesce(lag(close_cents) OVER (ORDER BY month), 0) AS open_cents FROM ar_close
),
items AS (
    SELECT month, sum(mrr_basis_cents) AS mrr_basis_cents, sum(sum_daily_arr_cents) AS sum_daily_arr_cents
    FROM r1_items GROUP BY month
),
base AS (
    SELECT rv.*, d.days, d.sum_daily_arr_cents, b.closing_arr_cents,
           coalesce(i.mrr_basis_cents, 0) AS item_mrr_basis_cents,
           coalesce(i.sum_daily_arr_cents, 0) AS item_sum_daily_arr_cents,
           cl.open_cents AS cl_open_cents, cl.close_cents AS cl_close_cents,
           ar.open_cents AS ar_open_cents, ar.close_cents AS ar_close_cents,
           e.allowance_open_cents, e.allowance_close_cents, e.bad_debt_expense_cents,
           e.earned_written_off_cents AS ecl_written_off_cents
    FROM revenue_month rv
    JOIN mrr_daily_month d USING (month)
    JOIN mrr_bridge b USING (month)
    LEFT JOIN items i USING (month)
    JOIN cl USING (month)
    JOIN ar USING (month)
    JOIN ecl_month e USING (month)
),
r1 AS (                                                  -- numerators over r1_denominator, in cents
    SELECT *,
           12 * days                                                       AS r1_denominator,
           closing_arr_cents * days                                        AS r1_closing_mrr,
           closing_arr_cents * days - sum_daily_arr_cents                  AS r1_timing,
           sum_daily_arr_cents                                             AS r1_average_mrr,
           item_mrr_basis_cents * 12 * days - item_sum_daily_arr_cents     AS r1_daycount_proration,
           once_coupon_recognised_cents * 12 * days                        AS r1_once_coupons,
           refunds_delivered_cents * 12 * days                             AS r1_refunds_delivered,
           net_revenue_cents * 12 * days                                   AS r1_net_revenue
    FROM base
)
SELECT month, days, r1_denominator,
       -- R1  MRR -> net revenue
       r1_closing_mrr, r1_timing, r1_average_mrr, r1_daycount_proration, r1_once_coupons, r1_refunds_delivered,
       r1_net_revenue,
       r1_net_revenue - (r1_closing_mrr - r1_timing + r1_daycount_proration - r1_once_coupons - r1_refunds_delivered)
           AS r1_unexplained,
       -- R2  net revenue -> billings (cents)
       net_revenue_cents, refunds_delivered_cents, cl_close_cents - cl_open_cents AS cl_change_cents,
       unearned_written_off_cents, refunds_unearned_cents, billings_cents,
       billings_cents - (net_revenue_cents + refunds_delivered_cents + (cl_close_cents - cl_open_cents)
                         + unearned_written_off_cents + refunds_unearned_cents) AS r2_unexplained_cents,
       -- R3  billings -> net cash (cents)
       ar_close_cents - ar_open_cents AS ar_change_cents, earned_written_off_cents, refunds_total_cents,
       net_cash_cents,
       net_cash_cents - (billings_cents - (ar_close_cents - ar_open_cents) - earned_written_off_cents
                         - unearned_written_off_cents - refunds_total_cents) AS r3_unexplained_cents,
       -- Rollforwards (§5.3)
       cl_open_cents, gross_recognised_cents, cl_close_cents,
       cl_close_cents - (cl_open_cents + billings_cents - gross_recognised_cents
                         - unearned_written_off_cents - refunds_unearned_cents) AS cl_unexplained_cents,
       ar_open_cents, collections_cents, ar_close_cents,
       ar_close_cents - (ar_open_cents + billings_cents - collections_cents
                         - earned_written_off_cents - unearned_written_off_cents) AS ar_unexplained_cents,
       allowance_open_cents, bad_debt_expense_cents, allowance_close_cents,
       allowance_close_cents - (allowance_open_cents + bad_debt_expense_cents - ecl_written_off_cents)
           AS ecl_unexplained_cents,
       ecl_written_off_cents - earned_written_off_cents AS write_off_sources_differ_cents
FROM r1;
