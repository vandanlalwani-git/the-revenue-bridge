# Metric Definitions

**Status:** Gate 1 signed off 2026-10-29, with the changes in §0 · **Currency:** USD only · **Time zone:** UTC

Each definition below is a judgement call. Each one states the rule, the formula where there is one, and the reason.

**Notation**
- *t* is a reporting month and *E(t)* is its month-end, `YYYY-MM-last 23:59:59 UTC`.
- *MRRc(d)* is customer *c*'s MRR at instant *d*.
- "Paying customer at *d*" means *MRRc(d)* > 0.
- All money is held as integer cents in raw data and shown in dollars to the cent.

---

## 0. Decisions (Gate 1)

| # | Decision | Outcome |
|---|----------|---------|
| D1 | **Source of MRR history** | **Approved.** MRR is rebuilt from invoice line items (period, price, quantity, coupon). It is tested against the generator's true subscription-state MRR for every customer and month-end (§1.13). |
| D2 | **ECL** | **Approved as an allowance**, using the IFRS 9 simplified approach with a provision matrix (§5.2). Changed at Gate 3 (2026-11-12, Vandan's decision) from two buckets to four, aged by failed card attempts, with an ageing floor. |
| D3 | **Gross presentation** | **Approved.** Receivables and contract liabilities are shown separately and not netted. A receivable is an unconditional right to consideration, and IFRS 15 presents it apart from contract assets and liabilities. |
| D4 | **Refunds** | **Changed: split by what is refunded** (§4.5). A refund of delivered service reduces revenue in the month issued (change in transaction price). A refund of the unearned portion reduces the contract liability and has no revenue effect. |
| D5 | **Collectability at renewal** | **Approved, as a stated simplification** (§4.3). |

---

## 1. MRR

**1.1 Definition.** MRR is the monthly-normalised recurring list price of a customer's active subscription items, net of recurring discounts that apply at that moment.

```
MRRc(d) = Σ over subscription items i of c that are live at d:
            ( unit_amount(i,d) × quantity(i,d) × (1 − percent_off(i,d)) − amount_off(i,d) ) × k(i,d)

k = 1     for interval = month
k = 1/12  for interval = year
```

**1.2 Live at d.**
- A recurring line item for item *i* has `period_start ≤ d < period_end`.
- The subscription has not ended: `ended_at IS NULL OR ended_at > d`.
- Where several lines cover *d*, for example after an upgrade proration, the **latest-starting non-credit line** wins.

**1.3 Prices, not line amounts.** Proration lines carry partial-period amounts. MRR uses `unit_amount × quantity` from the governing line's price. After a mid-period upgrade, the new price comes from the proration charge line's **price**, never its prorated **amount**. Proration affects revenue and cash, not MRR.

**1.4 Snapshot.** Reported MRR for month *t* is *MRR(E(t))*. **Average MRR** for *t* is the mean of the daily snapshots at 23:59:59 on each day of *t*. It is used only in the MRR → revenue bridge.

**1.5 Annual plans.** Price ÷ 12, regardless of when cash is received.

**1.6 Discounts.**
- `forever` and `repeating` coupons reduce MRR while the discount is active (`discount.start ≤ d < discount.end`).
- `once` coupons never touch MRR. They are a one-off revenue and cash effect.
- A 100%-off coupon gives MRR = 0, so that customer is **not** a paying customer.

**1.7 Trials.** MRR = 0 during a trial. The first paid period counts as **New**. Stripe bills a trial with a $0 invoice whose lines carry the plan's price but `amount = 0`, so a governing line with `amount = 0` contributes 0 to MRR, whatever its price.

**1.8 Cancellation.**
- A voluntary cancel on a monthly plan is always `cancel_at_period_end`. MRR continues until `ended_at`, the period end.
- An annual plan can also be ended early with a pro-rata refund of the unused term. MRR stops at `ended_at`.
- An involuntary cancel happens on dunning day 14. MRR stops at `ended_at`.

**1.9 Past due.**
- A subscription with an open invoice in dunning stays in MRR.
- **At-risk MRR(t)** = Σ *MRRc(E(t))* for subscriptions with status `past_due` at *E(t)*.

**1.10 Upgrades and downgrades.**
- Upgrades take effect immediately. MRR changes on the upgrade date, and the proration invoice goes to revenue and cash.
- Downgrades take effect at period end. MRR changes at renewal.

**1.11 Monthly → annual switch = contraction.**
- Annual is priced at 10× monthly, so the switch lowers MRR to 10/12 of its previous level.
- It is reported as **contraction** with driver *interval switch*, even though cash rises.
- The reverse switch, annual → monthly, is expansion.
- The customer has committed to more cash and lower run-rate revenue, and the bridge shows both effects rather than hiding either one.

**1.12 ARR** = 12 × MRR, used as a label only.

**1.13 Exact arithmetic and the rebuild test.**
- MRR is held internally as **ARR in integer cents**: monthly items × 12, annual items × 1.
- Annual ÷ 12 therefore never creates fractional cents, and the rebuild test can compare exactly.
- Percent discounts are rounded to the cent per item, as Stripe rounds per line.
- `test_mrr_rebuild` requires the invoice-rebuilt ARR to equal the generator's subscription-state ARR **for every customer at every month-end**. It also checks that the data contains cases of each of these:
  - mid-period upgrades
  - seat increases
  - cancel-at-period-end in the final paid period
  - annual plans
  - early annual terminations
  - repeating discounts ending
  - dunning
  - trials

---

## 2. MRR movements (the bridge)

**2.1 Level.** Movements are classified **per customer**, comparing *MRRc(E(t−1))* with *MRRc(E(t))*. A plan switch on a new subscription is therefore not counted as churn plus new.

| Movement | Condition | Amount |
|----------|-----------|--------|
| New | *MRRc(E(t−1))* = 0, *MRRc(E(t))* > 0, and *c* never paid before *E(t−1)* | + *MRRc(E(t))* |
| Reactivation | *MRRc(E(t−1))* = 0, *MRRc(E(t))* > 0, and *c* was a paying customer at some earlier month-end | + *MRRc(E(t))* |
| Expansion | both > 0 and *MRRc(E(t))* > *MRRc(E(t−1))* | + delta |
| Contraction | both > 0 and *MRRc(E(t))* < *MRRc(E(t−1))* | − delta |
| Churn | *MRRc(E(t−1))* > 0, *MRRc(E(t))* = 0 | − *MRRc(E(t−1))* |

**2.2 Bridge identity.** Opening MRR + New + Reactivation + Expansion − Contraction − Churn = Closing MRR. It must tie every month **to the cent**. Opening and closing are computed independently of the movements, from the daily MRR table.

**2.3 Churn type.** Churn is **involuntary** if the last subscription of *c* to end in *t* has `cancellation_reason = payment_failed`. Otherwise it is **voluntary**.

**2.4 Drivers.** Expansion and contraction are split by driver, per subscription item. Each item's delta gets one driver, using the first rule that applies:
1. **Plan change** – the product differs.
2. **Interval switch** – same product, monthly ↔ annual. Monthly → annual is always contraction (§1.11).
3. **Price change** – same product and interval, different `unit_amount`.
4. **Quantity change** – seats.
5. **Discount started / ended.**

Driver amounts are signed and sum to the customer's net delta.

**2.5 Blind spot.** A customer who starts and churns within the same month never appears in month-end snapshots. The count of such customers is shown on the page.

---

## 3. Churn and retention rates

**3.1 Paying customers** = |{ c : *MRRc(E(t))* > 0 }|.

**3.2 Logo churn %**

```
             |{ c : MRRc(E(t−1)) > 0  and  MRRc(E(t)) = 0 }|
logo_churn = ─────────────────────────────────────────────────
                     |{ c : MRRc(E(t−1)) > 0 }|
```

- **Denominator:** paying customers at the prior month-end. It excludes trials, 100%-off customers, and anyone new during *t*.
- **Numerator:** only customers from that opening set. Customers who are new or reactivated in *t* and churn within *t* are excluded (see 2.5).

**3.3 Gross MRR churn %** = (Churn MRR + Contraction MRR) ÷ Opening MRR.

**3.4 Net MRR churn %** = (Churn + Contraction − Expansion) ÷ Opening MRR. It excludes New and Reactivation. It can be negative.

**3.5 NRR (12-month)**

```
A(t)   = { c : MRRc(E(t−12)) > 0 }

NRR(t) = Σ_{c ∈ A(t)} MRRc(E(t))  ÷  Σ_{c ∈ A(t)} MRRc(E(t−12))
```

- It includes all expansion, contraction and churn of the base set *A(t)*, and their reactivation within the window.
- It excludes customers who were not paying at *E(t−12)*.
- The 12-month burn-in (2024) means NRR exists for every reporting month.

**3.6 GRR (12-month)** = Σ_{c ∈ A(t)} min( *MRRc(E(t))*, *MRRc(E(t−12))* ) ÷ Σ_{c ∈ A(t)} *MRRc(E(t−12))*. It is capped at 100% and excludes expansion.

**3.7 Cohorts**
- **Cohort** of *c* = the month containing *c*'s first instant with *MRRc* > 0, which is the first paid day, not the signup date.
- **Mk** = the month *k* months after the cohort month. M0 is the cohort month itself.

```
Logo retention(cohort, k)    = |{ c ∈ cohort : MRRc(E(M_k)) > 0 }| ÷ |cohort|
Revenue retention(cohort, k) = Σ_{c ∈ cohort} MRRc(E(M_k)) ÷ Σ_{c ∈ cohort} MRRc(E(M_0))
```

- Reactivated customers count again from the month they return.
- When a discount ends, revenue retention can rise above logo retention (E3). That reflects a roll-off from a discounted M0 base, not underlying growth, and the page labels it that way.

---

## 4. Revenue recognition (IFRS 15 / ASC 606)

**4.1 Performance obligation.** Access to the software over the service period. The obligation is satisfied over time and recognised **ratably by the second**.
- Stripe prorates by the second: upgrade prorations, seat changes and the pro-rata refund when an annual plan ends early are all computed on seconds remaining.
- Recognising on the same clock means the revenue and the billing for any part of a period are the same number. A refund of the unused term equals the contract liability left at that moment, to the cent, with no day-rounding residue.

**4.2 Per invoice line.**
- Each line amount (net of all discounts on that line, including `once` coupons) is spread over `[period_start, period_end)`. Let N be its length in seconds.
- **Cumulative cent rounding** is used. Revenue recognised up to instant *d* is `R(d) = round_half_up(amount × (d − period_start) / N)`, computed in integer arithmetic.
- Revenue for month *t* = R(min(*E(t)*, end)) − R(max(*E(t−1)*, `period_start`)), where end = min(`period_end`, `ended_at`).
- Each line therefore recognises exactly its own amount in integer cents over its life, and the generator computes prorations and refunds with the same function.
- Proration credit lines are negative and are spread the same way, rounding half away from zero.

**4.3 Delivered days only.**
- Revenue accrues from `period_start` to min(`period_end`, `ended_at`).
- **Days in dunning count as delivered**, because the customer keeps access.
- **Simplification (D5):** collectability is not reassessed at each renewal. Exposure is capped, because dunning stops after 14 days and the subscription is cancelled, so at most 14 days of unpaid service is recognised per invoice. That exposure is covered by the ECL allowance (§5.2).
- A stricter reading would stop recognising from the first failure. That would understate revenue for the majority of dunning invoices, which recover. The recovery rate is shown on the page.

**4.4 Uncollectible invoices.** Revenue for delivered days is **not reversed**. On write-off (dunning day 14):
- **Earned portion** (delivered days): Dr ECL allowance (or bad-debt expense) / Cr Receivable.
- **Unearned portion** (days after `ended_at`): Dr Contract liability / Cr Receivable. Nothing was earned, so this has no P&L effect.

**4.5 Refunds (D4), split by what is refunded.** Every refund is first measured against the refunded invoice's **unearned portion**: the amount of its lines falling after the subscription's `ended_at`. That portion is zero for a subscription that is still live.

- **Unearned portion** = min(refund, unearned amount still in the contract liability). Dr Contract liability / Cr Cash. No revenue effect. Example: the pro-rata refund on an annual plan ended early.
- **Delivered portion** = the rest of the refund. Dr Refunds (contra-revenue) / Cr Cash, in the month issued, as a change in transaction price. Prior months are not restated. Example: a service credit to a customer who stays.
- **Net revenue** = gross recognised − delivered-portion refunds.
- `test_refund_split` covers a pro-rata annual refund. It checks that the whole refund reduces the contract liability, that revenue is unchanged, and that revenue for the delivered months stays as it was.

**4.6 Billings** = Σ `amount_due` of invoices finalised in *t*. This includes proration invoices and excludes $0 trial invoices.

---

## 5. Ledger and expected credit loss (IFRS 9)

**5.1 Journal entries**

| Event | Dr | Cr |
|-------|----|----|
| Invoice finalised | Receivable | Contract liability |
| Revenue earned (daily) | Contract liability | Revenue |
| Charge succeeds | Cash | Receivable |
| Charge fails | — (dunning starts) | — |
| Write-off, earned portion | ECL allowance | Receivable |
| Write-off, unearned portion | Contract liability | Receivable |
| Month-end ECL true-up (D2) | Bad-debt expense | ECL allowance |
| Refund, delivered portion | Refunds (contra-revenue) | Cash |
| Refund, unearned portion | Contract liability | Cash |

**5.2 ECL allowance: IFRS 9 simplified approach, provision matrix (D2)**

Receivables are aged by the number of failed card attempts by *E(t)*. Stripe attempts the card 1 hour after finalisation and retries on days 3, 7 and 14. The 4th failure writes the invoice off, so an open invoice has at most 3 failures at a month-end.

| Bucket | Receivables in it at *E(t)* |
|--------|-----------------------------|
| **current** | Finalised and unpaid, no failed attempt yet. Card billing makes this bucket almost always empty: it holds only invoices finalised in the month's last hour. It matters for invoice-billed customers. |
| **failed_1** | 1 failed attempt (days 0–3 of dunning). |
| **failed_2** | 2 failed attempts (days 3–7). |
| **failed_3** | 3 failed attempts (days 7–14). The next failure is a write-off. |

**Loss rates**
- A bucket's own loss rate *r_b(t)* = Σ earned write-offs that eventually hit receivables which sat in bucket *b* at a month-end ÷ Σ the balances of bucket *b* at those month-ends.
- The rate is measured over the **trailing 12 month-ends to E(t−1)**, for invoices already resolved (paid or written off).
- The numerator is **earned** write-offs only. The unearned portion is derecognised against the contract liability and is not a credit loss.
- **No history:** a failed_* bucket with no balances in the window borrows the pooled rate of all failed_* buckets. The current bucket has no fallback, because it holds no failed payment.
- **Ageing floor:** the applied rate of a bucket is the highest rate among it and every earlier bucket, so an invoice slipping to a later retry stage never lowers the allowance. This is how current conditions reach the allowance. When a wave of failures moves balances into later stages, the allowance follows them at the higher rates, with no separate overlay parameter. It is not a forecast of future economic conditions. IFRS 9 also asks for forecasts, and none is modelled (§8).
- `ecl_rates` also stores the count of observed and lost invoices per bucket, so the chance that an invoice in a bucket is never paid (lost ÷ observed) can be shown beside the money-weighted rate.

**Allowance and expense**
- Allowance(*E(t)*) = Σ_b round_half_up(balance_b(*E(t)*) × applied rate_b(*t*)).
- Bad-debt expense(*t*) = earned write-offs(*t*) + Allowance(*E(t)*) − Allowance(*E(t−1)*).
- **Not applied: post-month-end evidence (IAS 10 style).** Every dunning invoice resolves within 14 days, so outcomes known by the close could replace the estimate. This was considered and not applied. It needs a close date that this pipeline doesn't model, and it would turn the allowance into near-hindsight. The amounts are immaterial: the allowance never exceeds $1,000.
- **Small sample.** There are about 40 month-end balances in 30 months, so the per-bucket rates move a lot from month to month. Money-weighted rates do not rise steadily with stage in this data. Annual invoices sit in the later stages, and only a small earned slice of each is ever lost. The ageing floor corrects for that.

**5.3 Rollforwards** (each must tie to the cent)

```
Contract liability:  open + billings − gross revenue recognised − unearned derecognised − unearned refunded = close
Receivable (gross):  open + billings − collections − write-offs (earned) − unearned derecognised = close
ECL allowance:       open + bad-debt expense − write-offs (earned) = close
```

**5.4 Cash.** Net cash collected = collections (succeeded charges) − all refunds (both portions), by charge or refund date.

---

## 6. The three reconciliations

Each reconciling item is computed **independently**. The "Unexplained" line is the target minus the sum of items, and it must show **$0.00** every month (pytest).

**R1 – MRR → Net revenue**

```
  Closing MRR
− Timing: closing MRR − average MRR        (mid-month new, churn, upgrades)
= Average MRR
+ Day-count & proration allocation          (ratable-by-day vs monthly-normalised MRR, e.g. 28/365 × annual in Feb,
                                             proration lines, delivered stubs before involuntary cancellation)
− One-off (once) coupons recognised
− Refunds, delivered portion
= Net revenue                               Unexplained: $0.00
```

**R2 – Net revenue → Billings**

```
  Net revenue
+ Refunds, delivered portion
+ Δ Contract liability (close − open)
+ Unearned portion derecognised on write-off
+ Refunds, unearned portion
= Billings                                  Unexplained: $0.00
```

**R3 – Billings → Net cash**

```
  Billings
− Δ Receivable, gross (close − open)
− Write-offs, earned portion
− Unearned portion derecognised
− Refunds, both portions
= Net cash collected                        Unexplained: $0.00
```

Bad-debt expense (P&L) appears beside R3. It is not part of the cash bridge.

---

## 7. Display rounding

**7.1 Compute in cents, round only for display.**
- Every stored figure is integer cents. MRR is held as ARR cents (§1.13), so MRR in cents = ARR ÷ 12, which is exact and may carry a fraction.
- Nothing is rounded before display, and no rounded figure is ever fed back into a calculation.

**7.2 Display units.**
- Money on the page and in notes is shown in whole dollars, or as $k to 1 decimal where a fact provides that form.
- Percentages are shown to 1 decimal place. Counts are exact.
- Rounding is half away from zero.

**7.3 Displayed bridges add up.**
- In any bridge (the MRR bridge and R1–R3), the opening and closing figures are rounded on their own, because they also appear elsewhere on the page.
- The components are then rounded with the **largest-remainder method**, so that displayed opening + Σ displayed components = displayed closing:
  1. Round each component.
  2. Compute the shortfall *k* in display units, which is at most about half the number of components.
  3. Move *k* components by one unit each, starting with the components whose rounding moved them furthest the other way. Ties go to the larger absolute value, then to bridge order.
- The adjustment is never more than 1 display unit per component. The "Unexplained" line is computed in cents, and it must be exactly 0 before any display rounding happens.

**7.4 One implementation.** `pipeline/display.py` is the only rounding code. The page, the facts file (which carries each fact's displayed form) and `notes/evaluate.py` all use it. A number in a note is grounded only if it equals a cited fact's displayed form under these rules.

## 8. Out of scope in v1

- **Multi-currency.** The future approach is constant-currency MRR, and deferred revenue plus its release at the historical invoice-date rate (IAS 21, non-monetary).
- **Usage-based pricing.**
- **Mid-period cancellation of monthly plans.** Monthly cancels are always at period end. Early termination with a pro-rata refund is modelled for annual plans only.
- **Tax and VAT.** All amounts are tax-exclusive.
- **Forecast overlay on ECL.** IFRS 9 asks for forecasts of future conditions as well as current ones. The matrix reflects current conditions through its ageing floor (§5.2) but models no forecast.

---

**Sign-off:** ☑ D1 ☑ D2 (provision matrix) ☑ D3 ☑ D4 (split) ☑ D5 (as a simplification) ☑ §1–6 · Gate 1 approved 2026-10-29 · D2 matrix changed to four aged buckets, Gate 3, 2026-11-12
