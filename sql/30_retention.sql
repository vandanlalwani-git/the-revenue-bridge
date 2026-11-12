-- 30_retention.sql
-- ============================================================================
-- HEADER
--   What it does: the retention numbers. Logo churn (share of customers lost),
--     gross and net MRR churn, 12-month NRR and GRR, and cohort tables (groups
--     of customers by the month they first paid, followed over time). Each rate
--     is stored as its top and bottom number; percentages are made on screen.
--   Judgement call: logo churn = customers lost this month / paying customers at
--     last month-end, so this month's new customers are not in the rate. NRR and
--     GRR take the customers paying exactly 12 months ago and compare their MRR
--     now with their MRR then. GRR caps each customer at their old amount, so
--     upgrades can't hide losses.
--   Edge case: a cohort is the month of a customer's first paid day, not first
--     month-end. Someone who paid and left in that first month is in the cohort
--     but already gone at month-end, so month-0 retention can be under 100%.
-- ============================================================================

CREATE OR REPLACE TABLE retention_month AS
WITH months AS (
    SELECT month,
           lag(month, 1)  OVER (ORDER BY month) AS prev_month,
           lag(month, 12) OVER (ORDER BY month) AS month_12_ago
    FROM calendar
),
opening AS (
    SELECT m.month, count(c.customer) AS customers
    FROM months m LEFT JOIN customer_mrr c ON c.month = m.prev_month
    GROUP BY m.month
),
closing AS (
    SELECT m.month, count(c.customer) AS customers
    FROM months m LEFT JOIN customer_mrr c ON c.month = m.month
    GROUP BY m.month
),
churned AS (
    SELECT month, count(*) AS customers FROM mrr_movements WHERE movement = 'churn' GROUP BY month
),
twelve AS (                                              -- base set A(t): paying at E(t-12)
    SELECT m.month,
           count(*)                                              AS base_customers,
           sum(b.arr_cents)                                      AS base_arr_cents,
           sum(coalesce(n.arr_cents, 0))                         AS nrr_arr_cents,
           sum(least(coalesce(n.arr_cents, 0), b.arr_cents))     AS grr_arr_cents
    FROM months m
    JOIN customer_mrr b ON b.month = m.month_12_ago
    LEFT JOIN customer_mrr n ON n.month = m.month AND n.customer = b.customer
    GROUP BY m.month
)
SELECT m.month,
       o.customers                                   AS opening_customers,
       c.customers                                   AS closing_customers,
       coalesce(ch.customers, 0)                     AS churned_customers,       -- logo churn numerator
       br.opening_arr_cents,
       br.churn_voluntary_arr_cents + br.churn_involuntary_arr_cents AS churn_arr_cents,
       br.contraction_arr_cents,
       br.expansion_arr_cents,
       t.base_customers                              AS nrr_base_customers,
       t.base_arr_cents                              AS nrr_base_arr_cents,     -- NRR and GRR denominator
       t.nrr_arr_cents,                                                          -- NRR numerator
       t.grr_arr_cents                                                           -- GRR numerator
FROM months m
JOIN mrr_bridge br USING (month)
JOIN opening o USING (month)
JOIN closing c USING (month)
LEFT JOIN churned ch USING (month)
LEFT JOIN twelve t USING (month);

-- For reading only: the rates as unrounded fractions.
CREATE OR REPLACE VIEW retention_month_rates AS
SELECT month,
       churned_customers / nullif(opening_customers, 0)                                  AS logo_churn,
       (churn_arr_cents + contraction_arr_cents) / nullif(opening_arr_cents, 0)          AS gross_mrr_churn,
       (churn_arr_cents + contraction_arr_cents - expansion_arr_cents)
           / nullif(opening_arr_cents, 0)                                                AS net_mrr_churn,
       nrr_arr_cents / nullif(nrr_base_arr_cents, 0)                                     AS nrr,
       grr_arr_cents / nullif(nrr_base_arr_cents, 0)                                     AS grr
FROM retention_month;

-- Cohort = month of the customer's first paid day (definitions §3.7).
CREATE OR REPLACE TABLE customer_cohort AS
SELECT customer, month_of(min(period_start)) AS cohort, min(period_start) AS first_paid_at
FROM stg_lines
WHERE amount > 0 AND NOT proration
GROUP BY customer;

CREATE OR REPLACE TABLE cohort_retention AS
WITH cal AS (
    SELECT month, row_number() OVER (ORDER BY month) - 1 AS idx FROM calendar
),
sizes AS (
    SELECT cohort, count(*) AS customers FROM customer_cohort GROUP BY cohort
),
base AS (                                                -- revenue retention denominator: cohort MRR at E(M0)
    SELECT cc.cohort, coalesce(sum(cm.arr_cents), 0) AS m0_arr_cents
    FROM customer_cohort cc
    LEFT JOIN customer_mrr cm ON cm.customer = cc.customer AND cm.month = cc.cohort
    GROUP BY cc.cohort
),
grid AS (
    SELECT s.cohort, c2.month, c2.idx - c1.idx AS k
    FROM sizes s
    JOIN cal c1 ON c1.month = s.cohort
    JOIN cal c2 ON c2.idx >= c1.idx
)
SELECT g.cohort, g.k, g.month,
       s.customers                    AS cohort_customers,
       count(cm.customer)             AS retained_customers,     -- logo retention numerator
       b.m0_arr_cents,                                            -- revenue retention denominator
       coalesce(sum(cm.arr_cents), 0) AS arr_cents                -- revenue retention numerator
FROM grid g
JOIN sizes s USING (cohort)
JOIN base b USING (cohort)
JOIN customer_cohort cc ON cc.cohort = g.cohort
LEFT JOIN customer_mrr cm ON cm.customer = cc.customer AND cm.month = g.month
GROUP BY g.cohort, g.k, g.month, s.customers, b.m0_arr_cents;
