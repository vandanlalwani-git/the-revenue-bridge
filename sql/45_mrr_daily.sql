-- 45_mrr_daily.sql
-- ============================================================================
-- HEADER
--   What it does: MRR at 23:59:59 UTC on every day, not just month-ends, per
--     subscription item, using exactly the same rule as 10_mrr.sql. A month's
--     average MRR (needed for R1) = the daily values added up / days in month.
--   Judgement call: a plain average. Every day counts equally, whatever
--     happened during it.
--   Edge case: the same rule now lives in two files, so a test checks that the
--     last day of each month equals 10_mrr.sql's month-end value exactly. UTC
--     has no daylight saving, so every day is exactly 86,400 seconds.
-- ============================================================================

CREATE OR REPLACE TABLE days AS
WITH bounds AS (SELECT min(prev_month_end) + 1 AS first_day, max(month_end) AS last_second FROM calendar)
SELECT bounds.first_day + r.i * 86400 + 86399 AS snapshot_at,
       month_of(bounds.first_day + r.i * 86400) AS month
FROM bounds CROSS JOIN range(0, 4000) AS r(i)
WHERE bounds.first_day + r.i * 86400 + 86399 <= bounds.last_second;

CREATE OR REPLACE TABLE mrr_item_daily AS
WITH covering AS (
    SELECT d.month, d.snapshot_at, l.*,
           row_number() OVER (PARTITION BY d.snapshot_at, l.subscription_item
                              ORDER BY l.period_start DESC, l.proration DESC, l.line) AS rank
    FROM days d
    JOIN stg_lines l
      ON l.period_start <= d.snapshot_at AND d.snapshot_at < l.period_end AND l.amount >= 0
    JOIN stg_subscriptions s
      ON s.subscription = l.subscription AND (s.ended_at IS NULL OR s.ended_at > d.snapshot_at)
)
SELECT month, snapshot_at, customer, subscription_item,
       CASE WHEN amount = 0 THEN 0
            ELSE (unit_amount * quantity
                  - CASE WHEN coupon_duration IN ('forever', 'repeating')
                              AND discount_start <= snapshot_at AND (discount_end IS NULL OR snapshot_at < discount_end)
                         THEN round_half_up(unit_amount * quantity * CAST(round(percent_off * 100) AS BIGINT), 10000)
                         ELSE 0 END)
                 * CASE WHEN interval = 'month' THEN 12 ELSE 1 END
       END AS arr_cents
FROM covering
WHERE rank = 1;

-- Average MRR for month t = sum of daily ARR / (12 x days in t), kept as an exact numerator and day count.
CREATE OR REPLACE TABLE mrr_daily_month AS
SELECT d.month, count(*) AS days,
       coalesce(sum(m.arr_cents), 0) AS sum_daily_arr_cents
FROM days d
LEFT JOIN (SELECT snapshot_at, sum(arr_cents) AS arr_cents FROM mrr_item_daily GROUP BY snapshot_at) m USING (snapshot_at)
GROUP BY d.month;
