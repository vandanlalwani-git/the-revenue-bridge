-- 10_mrr.sql
-- ============================================================================
-- HEADER (Vandan, in your own words): what this computes, the judgement call,
-- one edge case.
--
--
-- ----------------------------------------------------------------------------
-- Draft notes (Claude), for you to replace:
--   Computes: MRR at every month-end, per subscription item, rebuilt from invoice
--     lines (D1). Held as ARR in integer cents so annual / 12 never leaves a
--     fraction (definitions §1.13).
--   Judgement call: when several lines cover a month-end, the latest-starting
--     non-credit line governs, and MRR uses that line's price x quantity, never
--     its prorated amount (§1.2-1.3). After a mid-month upgrade, the proration
--     charge line governs, so the new price counts from the upgrade date.
--   Edge case: trial lines carry the plan's price but amount = 0, so they count
--     as 0 MRR (§1.7). A customer who has asked to cancel still counts until
--     ended_at (§1.8).
-- ============================================================================

CREATE OR REPLACE TABLE mrr_item_month_end AS
WITH covering AS (
    SELECT c.month, c.month_end, l.*,
           row_number() OVER (PARTITION BY c.month, l.subscription_item
                              ORDER BY l.period_start DESC, l.proration DESC, l.line) AS rank
    FROM calendar c
    JOIN stg_lines l
      ON l.period_start <= c.month_end AND c.month_end < l.period_end
     AND l.amount >= 0                                   -- credit lines never govern
    JOIN stg_subscriptions s
      ON s.subscription = l.subscription
     AND (s.ended_at IS NULL OR s.ended_at > c.month_end)  -- subscription still live
),
governing AS (
    SELECT *,
           CASE WHEN amount = 0 THEN 0 ELSE unit_amount * quantity END AS list_amount,
           CASE WHEN amount = 0                    THEN 0
                WHEN coupon_duration IS NULL       THEN 0
                WHEN coupon_duration = 'once'      THEN 0   -- one-off coupons never touch MRR
                WHEN discount_start <= month_end AND (discount_end IS NULL OR month_end < discount_end)
                     THEN round_half_up(unit_amount * quantity * CAST(round(percent_off * 100) AS BIGINT), 10000)
                ELSE 0 END                                              AS recurring_discount,
           CASE WHEN interval = 'month' THEN 12 ELSE 1 END              AS periods_per_year
    FROM covering
    WHERE rank = 1
)
SELECT month, month_end, customer, subscription, subscription_item, line, product, is_seat, interval,
       unit_amount, quantity, list_amount, recurring_discount, periods_per_year,
       (list_amount - recurring_discount) * periods_per_year AS arr_cents
FROM governing;

-- Per subscription, with the at-risk flag: an open dunning spell at the month-end (§1.9).
CREATE OR REPLACE TABLE mrr_month_end AS
SELECT m.month, m.month_end, m.customer, m.subscription,
       sum(m.arr_cents) AS arr_cents,
       EXISTS (SELECT 1 FROM stg_dunning d
               WHERE d.subscription = m.subscription
                 AND d.first_failed_at <= m.month_end
                 AND (d.resolved_at IS NULL OR d.resolved_at > m.month_end)) AS past_due
FROM mrr_item_month_end m
GROUP BY m.month, m.month_end, m.customer, m.subscription;
