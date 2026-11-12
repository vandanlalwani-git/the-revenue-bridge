-- 50_ledger.sql
-- ============================================================================
-- HEADER
--   What it does: the balance-sheet items at each month-end. Contract liability
--     (billed but service not yet delivered), receivables (invoiced but not yet
--     paid), and the IFRS 9 allowance for expected credit loss (ECL: the part
--     of receivables we expect never to collect), plus the monthly bad-debt
--     expense = earned write-offs + change in the allowance. Each balance is
--     counted fresh from invoices and refunds, not by adding up movements, so
--     the rollforwards in 60_reconciliation.sql are a real test.
--   Judgement call: receivables are aged by failed card attempts at the
--     month-end: current (none yet), failed_1, failed_2, failed_3 (Stripe
--     retries on days 3, 7 and 14). A bucket's loss rate = earned amounts
--     written off / what sat in it over the past 12 month-ends, counting only
--     invoices already settled. A bucket with no history borrows the rate of
--     all failed buckets together, and a later bucket never gets a lower rate
--     than an earlier one, so slipping a retry stage never lowers the allowance.
--   Edge case: a written-off invoice splits in two. The part for days already
--     served is a credit loss. The part for days after cancellation was never
--     revenue, so it just comes off the contract liability and is not a loss.
--     Together the two parts clear the whole receivable.
-- ============================================================================

CREATE OR REPLACE TABLE ledger_invoices AS
SELECT i.id AS invoice, i.customer, i.subscription, i.status, i.finalized_at, i.amount_due,
       i.paid_at, i.marked_uncollectible_at, d.first_failed_at,
       coalesce(t.earned_cents, 0)   AS earned_cents,
       coalesce(t.unearned_cents, 0) AS unearned_cents
FROM raw.invoices i
LEFT JOIN (SELECT invoice, sum(earned_cents) AS earned_cents, sum(unearned_cents) AS unearned_cents
           FROM revenue_line_totals GROUP BY invoice) t ON t.invoice = i.id
LEFT JOIN stg_dunning d ON d.invoice = i.id
WHERE i.amount_due <> 0;

-- The provision matrix buckets, in ageing order: failed card attempts by the month-end.
-- Stripe retries on days 3, 7 and 14, and the 4th failure writes the invoice off,
-- so an open invoice has at most 3 failed attempts at a month-end.
CREATE OR REPLACE TABLE ecl_buckets AS
SELECT * FROM (VALUES ('current', 0), ('failed_1', 1), ('failed_2', 2), ('failed_3', 3)) AS b(bucket, stage);

-- Gross receivables at each month-end, by bucket: finalised, not yet paid, not yet written off.
CREATE OR REPLACE TABLE receivables_month_end AS
WITH open_items AS (
    SELECT c.month, c.month_end, li.invoice, li.amount_due AS balance_cents,
           (SELECT count(*) FROM raw.charges ch
            WHERE ch.invoice = li.invoice AND ch.status = 'failed' AND ch.created <= c.month_end) AS failed_attempts
    FROM calendar c
    JOIN ledger_invoices li
      ON li.finalized_at <= c.month_end
     AND (li.paid_at IS NULL OR li.paid_at > c.month_end)
     AND (li.marked_uncollectible_at IS NULL OR li.marked_uncollectible_at > c.month_end)
)
SELECT o.*, b.bucket
FROM open_items o
JOIN ecl_buckets b ON b.stage = least(o.failed_attempts, 3);

-- Contract liability at each month-end: billed but not yet earned, less what has left on write-off or refund.
CREATE OR REPLACE TABLE contract_liability_month_end AS
WITH unearned_on_lines AS (
    SELECT c.month, sum(r.net_amount
                        - recognised_to(r.net_amount, r.period_start, r.period_end, r.service_end, c.month_end)) AS cents
    FROM calendar c
    JOIN revenue_lines r ON r.period_start <= c.month_end
    JOIN ledger_invoices li ON li.invoice = r.invoice AND li.finalized_at <= c.month_end
    GROUP BY c.month
),
released AS (
    SELECT c.month,
           coalesce((SELECT sum(unearned_cents) FROM ledger_invoices
                     WHERE status = 'uncollectible' AND marked_uncollectible_at <= c.month_end), 0)
         + coalesce((SELECT sum(unearned_cents) FROM refund_split WHERE created <= c.month_end), 0) AS cents
    FROM calendar c
)
SELECT c.month, coalesce(u.cents, 0) - r.cents AS balance_cents
FROM calendar c
LEFT JOIN unearned_on_lines u USING (month)
JOIN released r USING (month);

-- IFRS 9 provision matrix (D2): one loss rate per bucket, from the trailing 12 month-ends.
-- 1. Own rate: earned write-offs / balances that sat in the bucket, counting only invoices
--    already resolved (paid or written off) by E(t-1).
-- 2. A failed_* bucket with no balances in the window borrows the pooled rate of all failed_* buckets.
-- 3. Ageing floor: a bucket's rate is never below the rate of an earlier bucket, so an invoice
--    moving to a later retry stage never lowers the allowance.
CREATE OR REPLACE TABLE ecl_rates AS
WITH observations AS (
    SELECT r.month_end AS observed_at, r.bucket, r.balance_cents,
           coalesce(least(li.paid_at, li.marked_uncollectible_at), li.paid_at, li.marked_uncollectible_at) AS resolved_at,
           li.status = 'uncollectible'                                            AS lost,
           CASE WHEN li.status = 'uncollectible' THEN li.earned_cents ELSE 0 END AS loss_cents
    FROM receivables_month_end r JOIN ledger_invoices li USING (invoice)
),
months AS (
    SELECT month, prev_month_end, lag(month_end, 12) OVER (ORDER BY month) AS window_first
    FROM calendar
),
own AS (
    SELECT m.month, b.bucket, b.stage,
           coalesce(sum(o.loss_cents), 0)      AS loss_cents,          -- own rate numerator
           coalesce(sum(o.balance_cents), 0)   AS observed_cents,      -- own rate denominator
           count(o.observed_at)                AS observed_invoices,
           count(o.observed_at) FILTER (WHERE o.lost) AS lost_invoices -- chance never paid = lost / observed
    FROM months m
    CROSS JOIN ecl_buckets b
    LEFT JOIN observations o
      ON o.bucket = b.bucket
     AND o.observed_at <= m.prev_month_end
     AND o.observed_at >= coalesce(m.window_first, 0)
     AND o.resolved_at <= m.prev_month_end
    GROUP BY m.month, b.bucket, b.stage
),
pooled AS (
    SELECT month, sum(loss_cents) AS loss_cents, sum(observed_cents) AS observed_cents
    FROM own WHERE stage > 0 GROUP BY month
),
base AS (                                                -- steps 1 and 2
    SELECT o.*,
           CASE WHEN o.observed_cents > 0                  THEN 'own'
                WHEN o.stage > 0 AND p.observed_cents > 0  THEN 'pooled'
                ELSE 'none' END                                                           AS base_source,
           CASE WHEN o.observed_cents > 0 THEN o.loss_cents
                WHEN o.stage > 0 THEN coalesce(p.loss_cents, 0) ELSE 0 END                AS base_loss_cents,
           CASE WHEN o.observed_cents > 0 THEN o.observed_cents
                WHEN o.stage > 0 THEN coalesce(p.observed_cents, 0) ELSE 0 END            AS base_observed_cents
    FROM own o LEFT JOIN pooled p USING (month)
),
floored AS (                                             -- step 3: highest base rate at or before this stage
    SELECT b.month, b.bucket, e.bucket AS rate_from, e.base_loss_cents, e.base_observed_cents,
           row_number() OVER (PARTITION BY b.month, b.bucket
                              ORDER BY CASE WHEN e.base_observed_cents = 0 THEN 0
                                            ELSE e.base_loss_cents / e.base_observed_cents END DESC,
                                       e.stage DESC) AS rank
    FROM base b JOIN base e ON e.month = b.month AND e.stage <= b.stage
)
SELECT b.month, b.bucket, b.stage,
       b.loss_cents, b.observed_cents, b.observed_invoices, b.lost_invoices,
       b.base_source,
       f.rate_from,                                      -- the bucket whose base rate is applied
       f.base_loss_cents     AS rate_loss_cents,         -- applied rate numerator
       f.base_observed_cents AS rate_observed_cents      -- applied rate denominator
FROM base b
JOIN floored f ON f.month = b.month AND f.bucket = b.bucket AND f.rank = 1;

CREATE OR REPLACE TABLE ecl_month AS
WITH balances AS (
    SELECT c.month, b.bucket, b.stage, coalesce(sum(r.balance_cents), 0) AS balance_cents
    FROM calendar c
    CROSS JOIN ecl_buckets b
    LEFT JOIN receivables_month_end r ON r.month = c.month AND r.bucket = b.bucket
    GROUP BY c.month, b.bucket, b.stage
),
allowance AS (
    SELECT b.month,
           sum(b.balance_cents) FILTER (WHERE b.bucket = 'current')  AS current_cents,
           sum(b.balance_cents) FILTER (WHERE b.bucket = 'failed_1') AS failed_1_cents,
           sum(b.balance_cents) FILTER (WHERE b.bucket = 'failed_2') AS failed_2_cents,
           sum(b.balance_cents) FILTER (WHERE b.bucket = 'failed_3') AS failed_3_cents,
           sum(b.balance_cents) FILTER (WHERE b.stage > 0)           AS dunning_cents,
           sum(CASE WHEN e.rate_observed_cents = 0 THEN 0
                    ELSE round_half_up(b.balance_cents * e.rate_loss_cents, e.rate_observed_cents) END) AS allowance_cents
    FROM balances b JOIN ecl_rates e USING (month, bucket)
    GROUP BY b.month
),
write_offs AS (
    SELECT month_of(marked_uncollectible_at) AS month, sum(earned_cents) AS cents
    FROM ledger_invoices WHERE status = 'uncollectible' GROUP BY 1
)
SELECT a.month, a.current_cents, a.failed_1_cents, a.failed_2_cents, a.failed_3_cents, a.dunning_cents,
       coalesce(lag(a.allowance_cents) OVER (ORDER BY a.month), 0) AS allowance_open_cents,
       a.allowance_cents                                           AS allowance_close_cents,
       coalesce(w.cents, 0)                                        AS earned_written_off_cents,
       coalesce(w.cents, 0) + a.allowance_cents
           - coalesce(lag(a.allowance_cents) OVER (ORDER BY a.month), 0) AS bad_debt_expense_cents
FROM allowance a
LEFT JOIN write_offs w USING (month);
