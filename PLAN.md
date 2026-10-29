# The Revenue Bridge: Build Plan (v2, approved with changes 2026-10-29)

A SaaS finance pack built on Stripe-shaped test data. SQL computes every number: the MRR bridge, churn, NRR, cohort retention, and a reconciliation from MRR to recognised revenue to billings to cash. An AI step writes a founder note for every month using only those figures. A frozen-prompt evaluation then checks each note against a hidden answer key of planted events.

**The pitch in one line:** "Most Stripe dashboards stop at MRR. This one ties MRR to IFRS 15 revenue and to cash, and its AI summary is tested against a hidden answer key."

The judgement calls are in [docs/metric_definitions.md](docs/metric_definitions.md). That document is what gets signed off at Gate 1.

---

## 1. What the final product looks like

One static page on GitHub Pages, deployed from `site/` by a GitHub Actions workflow. A month picker drives the whole page across the 18 reporting months (2025-01 to 2026-06).

| # | Section | What the viewer sees |
|---|---------|----------------------|
| 1 | **Header** | Name, a one-line pitch, a "Synthetic test data, Stripe schema, USD" badge, and links to the repo, write-up and video. |
| 2 | **KPI strip** | Closing MRR, MoM growth, logo churn %, gross MRR churn %, 12-month NRR / GRR, and at-risk MRR in dunning. |
| 3 | **MRR bridge** | Waterfall chart: Opening → +New → +Expansion → +Reactivation → −Contraction → −Churn (voluntary / involuntary) → Closing. Expansion and contraction are split by driver. |
| 4 | **Founder notes** | 3 featured notes (2025-11, 2026-02, 2026-06 control). Each figure cites a fact ID that links to its source row. The other 15 notes are linked in the repo. |
| 5 | **Cohort retention** | Heatmap of cohort month × months since first paid month, with a toggle between logo retention and revenue retention. |
| 6 | **MRR → Revenue → Billings → Cash** | Three bridges per month, each with an "Unexplained" line that must show $0.00. Below them, rollforwards for deferred revenue and receivables, plus bad-debt expense. |
| 7 | **Evaluation scorecard** | All 18 notes: planted events caught or missed at the metrics layer and the narrative layer, ungrounded numbers (target 0), word-number violations (target 0), and unsupported anomaly claims in quiet months. It also shows the frozen prompt's hash. |
| 8 | **Metric definitions** | The judgement-call document. |
| 9 | **How it's built** | A pipeline diagram, the stack, and the frozen-prompt protocol. |

**Deliverables:** the page, a 300–500 word write-up (`docs/writeup.md`), a 2-minute walkthrough, and a public repo.

---

## 2. Architecture

```
scenario.yaml ──► generator/generate.py ──► data/raw/*.parquet (Stripe-shaped, USD, cents, epoch secs)
                        │                              │
               data/answer_key.json                    ▼
               (planted events, hidden)        DuckDB + sql/*.sql ──► marts
                        │                              │
                        │                              ▼
                        │                  notes/build_facts.py ──► notes/facts/YYYY-MM.json
                        │                              │
                        │                              ▼
                        │                  notes/write_notes.py ──► `claude -p` ×18 (isolated, no tools)
                        │                              │
                        │                              ▼
                        └──────────────► notes/evaluate.py  (refuses to run until prompt is frozen)
                                                       │
                                                       ▼
                                          site/build_site.py ──► site/index.html ──► GitHub Pages
```

**Stack:** Python (local venv), DuckDB, PyYAML, pytest. Notes come from `claude -p` headless. Charts are plain SVG or Chart.js. Hosting is GitHub Pages through Actions. Total cost is $0.

**Data source.** The primary source is a seeded, deterministic generator that writes Stripe's object model: `customers`, `products`, `prices`, `coupons`, `discounts`, `subscriptions`, `subscription_items`, `invoices`, `invoice_line_items`, `charges`, `refunds`. Amounts are integer cents and timestamps are Unix epoch seconds, as Stripe returns them. A small Stripe test-clock run (Day 8) checks the loader against a real export. It is the first thing cut if time runs short.

**Simulation window.** The simulation runs 2024-01 to 2026-06. The first 12 months are burn-in so that 12-month NRR exists from the first reporting month and opening balances are realistic. Reporting covers 2025-01 to 2026-06.

---

## 3. Data scenario (USD only)

- **Company:** a fictional B2B SaaS tool launched January 2024, with roughly 500–650 paying customers by mid-2026.
- **Plans:**

  | Plan | Monthly | Annual (10× monthly) |
  |------|---------|----------------------|
  | Starter | $29 flat | $290 |
  | Growth | $99 flat | $990 |
  | Scale | $399 base, includes 10 seats, plus $30 per extra seat | $3,990 base, $300 per extra seat |

  Extra seats are a second subscription item, as Stripe models add-ons.
- **Messiness, on purpose:**
  - 14-day free trials
  - coupons that run forever, repeat or apply once
  - immediate upgrades with proration
  - downgrades at period end
  - seat-quantity changes
  - switches from monthly to annual
  - refunds: service credits for delivered service, and pro-rata refunds when an annual plan ends early
  - failed payments with a 14-day dunning window
  - reactivations, which open a new subscription for an existing customer

**Dunning window:** the first payment attempt is on the invoice date (day 0), with retries on days 3, 7 and 14. If the day-14 retry fails, the invoice is marked uncollectible and the subscription is cancelled on day 14 (`cancellation_reason = payment_failed`). While in dunning the subscription is `past_due`, keeps access, and its MRR counts as **at-risk MRR**.

### Planted events (`data/answer_key.json`, never shown to the note writer)

| ID | Event | Expected signal (month: signal) |
|----|-------|--------------------------------|
| E1 | Monthly Starter price rises from $29 to $35, effective at each subscription's first renewal from 2025-09-01. Annual Starter plans move at their renewal. | **2025-09:** expansion with driver *price change*, concentrated in Starter. **2025-09 and 2025-10:** Starter voluntary churn above its trailing 3-month average. |
| E2 | Largest Scale customer (monthly, about 60 seats) cancels, ending in 2025-11 | **2025-11:** one logo makes up more than 50% of churned MRR, and gross MRR churn % far exceeds logo churn %. |
| E3 | Most of the 2026-01 cohort signs up on a promo: no trial, 50% off for 3 months (repeating coupon) | **2026-04:** expansion with driver *discount ended*, attributable to the 2026-01 cohort. Voluntary churn concentrated in that cohort. Logo-retention cliff at M3 for the 2026-01 cohort. |
| E4 | A batch of cards expires at the end of 2026-01, so those customers' 2026-02 renewals fail | **Primary (counts):** 2026-02: customers entering dunning spike, involuntary churn count up (failures on renewal days 1–14), and customers still in dunning at month-end elevated (days 15–28). 2026-03: involuntary churn count up again as the spill-over clears. **Supporting:** at-risk MRR at 2026-02 month-end elevated and back to baseline at 2026-03 month-end; bad-debt expense up. At-risk MRR is not primary evidence: before adjustment 5, an organic cluster of 3 Scale customers in dunning moved it as much at 2026-04 month-end ($2,074 against $2,025), and a dollar measure is that fragile in any random path. |
| E5 | Annual-prepay push: monthly customers switch to annual at their 2026-03 renewal | **2026-03:** billings and cash far exceed revenue, and deferred revenue jumps. MRR shows contraction with driver *interval switch*, because annual = 10/12 of monthly. Logo churn is unaffected. |
| E6 | Large refund to a Scale customer for service already delivered (dispute settled). The customer stays. | **2026-04:** a $1,899 delivered-portion refund reduces net revenue and net cash. This is measured against the month's gross figures, not as an absolute fall, because the business is growing. MRR is unaffected. (E3 also falls in this month, which tests whether the note handles several events.) |
| E7 | Win-back campaign: a batch of churned customers return | **2026-05:** reactivation MRR well above its trailing 3-month average. |
| E8 | **Control:** no planted event | **2026-06:** no expected signal. The note must not make anomaly claims that no threshold-crossing fact supports. |

---

## 4. Metric definitions

The full set is in [docs/metric_definitions.md](docs/metric_definitions.md) (**Gate 1**). It includes exact formulas and denominators for MRR, movements and drivers, logo churn %, gross and net MRR churn %, NRR, GRR, cohorts, revenue recognition, the ECL treatment, the ledger rollforwards and all three reconciliations.

Revenue recognition, changed from v1:
- Revenue is recognised over the service period delivered, **including days in dunning**.
- Uncollectible invoices **do not reverse revenue**. The earned portion of the receivable goes to bad-debt expense under IFRS 9 ECL.
- The unearned portion, the days after cancellation, is derecognised against the contract liability and has no P&L effect.
- Write-offs appear in the billings → cash bridge.
- Bad-debt expense uses an IFRS 9 provision matrix with two buckets (current, and 0–14 days in dunning). Each bucket's trailing loss rate starts from the burn-in history.
- Refunds are split. A refund of delivered service reduces revenue. A refund of the unearned portion reduces the contract liability.
- A monthly → annual switch counts as contraction.

---

## 5. AI step: notes for all 18 months

1. **Facts.** `build_facts.py` writes `notes/facts/YYYY-MM.json` for every reporting month. Each figure gets an ID ([F1], [F2] …): bridge components, drivers, churn by plan, type and cohort, NRR and GRR, at-risk MRR, the reconciliation items, the **top 5 movers** and the **delta against the trailing 3-month average** for each headline metric. Refunds are **split into delivered (earned, reduces revenue) and unearned (reduces the contract liability)**, and the **largest single refund** is listed with its amount, portion and customer ID, so the note can attribute refunds correctly. Facts carry no event labels, only metrics.
2. **Prompt** (`notes/prompt.md`):
   - 120–200 words in a founder's voice.
   - Every number cites a fact ID.
   - **Every number is written as digits.** No word-numbers: no "doubled", "half", "three", "twice" or "a quarter of".
   - No causes beyond what the facts show.
   - Say plainly when a month is quiet.
3. **Freeze protocol.** Before any evaluation result is viewed:
   - The only check allowed before the freeze is a format lint (length, citation syntax, digits-only) on a **dev seed**, a different random seed from the test scenario.
   - The prompt is then frozen. `notes/FREEZE.json` records the prompt's SHA-256, the model ID and the CLI version, and that file is committed.
   - Each note's metadata records the prompt hash it was generated with.
   - `evaluate.py` refuses to run if `FREEZE.json` is missing or any hash doesn't match.
4. **Generation.** `write_notes.py` calls `claude -p` once per month (18 calls), using the CLI path, expected version and model pinned in `notes/config.yaml`. The actual CLI version and model are recorded next to each note in `notes/output/YYYY-MM.meta.json`:
   - run from a temp directory outside the repo
   - flags: `--tools ""`, `--strict-mcp-config`, `--no-session-persistence`, a pinned `--model`
   - facts passed on stdin
   - with no tools, the model can't read the answer key or any other file
5. **Evaluation**, every note against the answer key:
   - **Grounding:** each number in the note must equal a cited fact, after display rounding. Any that doesn't counts as ungrounded.
   - **What counts as a hallucination (only these two):** (a) a number that matches no fact after display rounding, and (b) a cause or attribution that the cited facts don't show. An accurate mention of a movement that wasn't planted, such as the 2026-04 dunning cluster, is **never** penalised. The answer key scores what was caught, not what else was reported.
   - **Word-numbers:** a regex flags cardinal words (zero–twenty, tens, hundred, thousand, million, dozen) and multiplicatives (half, halved, double/doubled, triple/tripled, twice, thrice, quarter). Fixed phrases such as "one-off" are allowlisted. The target is 0 violations.
   - **Detection, metrics layer:** each expected signal, checked by threshold in SQL.
   - **Detection, narrative layer:** does the note for that month mention the signal? This is checked with keyword and fact-ID rules, and a manual review is recorded.
   - **Quiet months, including E8 and all unplanted months:** anomaly language ("spike", "surge", "sharp", "concentrated", "unusual"…) has to cite a fact whose trailing-average delta crosses the threshold. Otherwise it counts as an unsupported claim.
6. **Write-up disclosures (required, drafted in `docs/writeup.md`):** the answer key was finalised after data generation and before any note was written, with each adjustment listed; the prompt was frozen before evaluation. Also: the facts file **pre-computes the top movers and the trailing-average deltas**. The AI's job is to select, rank and narrate figures already computed. It doesn't discover anomalies from raw data.

---

## 6. Repo layout

```
the-revenue-bridge/
├─ PLAN.md
├─ scenario.yaml
├─ requirements.txt
├─ generator/
│  ├─ schema.py            # Stripe-shaped table schemas
│  └─ generate.py          # seeded simulation → data/raw/*.parquet, data/answer_key.json
├─ data/raw/               # generated (reproducible from seed)
├─ stripe_check/           # Day 8, first to cut
├─ sql/
│  ├─ 00_staging.sql       # typed lines, calendar, dunning spells, round_half_up macro
│  ├─ 10_mrr.sql           # month-end MRR (as ARR cents) rebuilt from invoice lines, at-risk flag
│  ├─ 20_mrr_movements.sql # movements, drivers, the bridge (+ blind-spot count)
│  ├─ 30_retention.sql     # logo churn, MRR churn, NRR, GRR (numerators/denominators), cohorts
│  ├─ 40_revenue.sql       # by-second recognition over delivered service, refund split, monthly revenue/cash
│  ├─ 50_ledger.sql        # deferred revenue + AR rollforwards, ECL, write-offs
│  └─ 60_reconciliation.sql
├─ pipeline/run.py        # builds sql/*.sql, exports data/marts/*.parquet
├─ pipeline/display.py    # the only display-rounding code (definitions §7)
├─ notes/  prompt.md, FREEZE.json, build_facts.py, write_notes.py, evaluate.py, facts/, output/
├─ site/   build_site.py, template.html, index.html
├─ .github/workflows/pages.yml   # deploys site/ only
├─ tests/
└─ docs/   metric_definitions.md, writeup.md
```

**SQL ownership:** Claude drafts each SQL file, and Vandan annotates it. Every file opens with a header in Vandan's words covering what the file computes, the judgement call it makes and one edge case it handles. A file isn't done until it can be defended without notes.

---

## 7. Timeline and sign-off gates

| Day | Work | Done when |
|-----|------|-----------|
| 1 | Metric definitions, scenario, repo scaffold, generator skeleton (monthly plans, renewals, voluntary churn) | **✅ Gate 1 signed off 2026-10-29** |
| 2 | Generator messiness: annual, trials, coupons, proration, seats, refunds, dunning. Plant E1–E8 and write the answer key. Export true subscription-state ARR for `test_mrr_rebuild` | ✅ Done 2026-10-29: 25 tests pass, 2 wait for SQL |
| 3 | Staging, month-end MRR, movements and drivers, display rounding | **⛔ Gate 2 (awaiting sign-off): bridge ties all 30 months; rebuild = truth for all 7,257 customer-months** |
| 4 | Retention (logo churn, MRR churn, NRR, GRR, cohorts), revenue recognition by the second, refund split | ✅ Done 2026-10-29: revenue matches an independent Python recomputation every month; D4 and E5 checks pass |
| 5 | Daily MRR snapshots for average MRR (R1), ledger (deferred, AR, ECL, write-offs), cash, three reconciliations | **⛔ Gate 3: all reconciliations show $0.00 unexplained (pytest), Vandan signs off** |
| 6 | Facts builder. Prompt written, format-linted on the dev seed, frozen. 18 notes via `claude -p`. Then the evaluator | Scorecard produced |
| 7 | Static site | Page renders locally |
| 8 | Stripe test-clock check, GitHub Actions Pages workflow, polish | Same SQL runs on the Stripe export |
| 9 | Write-up, 2-min video, go live | Live URL |

Days 10–11 are slack.

**Cut order if behind:**
1. The Stripe check.
2. Seat-quantity changes (Scale becomes flat $399).

**The reconciliation is never cut.**

---

## 8. Hard v1 boundary

**In:**
- the generator and answer key
- SQL marts
- the MRR bridge with drivers
- retention metrics and cohorts
- revenue recognition and the ledger
- the three reconciliations
- 18 notes with evaluation, 3 of them featured on the page
- the static page

**Out (future work):**
- **Multi-currency.** If added later: constant-currency MRR so FX doesn't show as expansion or contraction. Deferred revenue and its release measured at the historical invoice-date rate, because a contract liability is non-monetary under IAS 21. Receivables and cash retranslated, with FX differences shown as a bridge item.
- Live Stripe connections, logins and multiple users.
- Usage-based pricing, forecasting, multi-entity, tax/VAT.
- Mid-period cancellation of monthly plans. Annual plans can end early with a pro-rata refund, and that is in scope.

---

## 9. Decisions made

| Topic | Decision |
|-------|----------|
| SQL | Claude drafts, Vandan annotates |
| Notes | `claude -p` headless (bundled Claude Code 2.1.308), isolated with no tools |
| Hosting | GitHub Pages from `site/` through GitHub Actions (`docs/` holds the definitions and write-up) |
| Python | local `.venv` (system Python 3.9.6, DuckDB 1.4.5) |
