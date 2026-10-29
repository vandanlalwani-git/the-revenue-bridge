# The Revenue Bridge: Write-up

*Draft skeleton. The final 300–500 word write-up is written on Day 9. The methodology statements below are required and must appear in the final version, condensed if needed.*

## Methodology: required disclosures

**Test data.** All data is synthetic. A seeded generator writes it in Stripe's object model (seed 20260129). Nothing comes from a real company.

**Answer key timing.** The expected signals in the answer key were finalised **after the data was generated and before any note was written**. Each adjustment was made on 2026-10-29, after the first generation of the test seed, and is listed below so the reader can judge them:

1. **E3 cliff rate raised from 0.35 to 0.45.** On first generation, the January 2026 promo cohort's logo-retention drop from M2 to M3 was 19.4 points, against a 20-point check. Non-promo customers in the same cohort diluted it. The planted event was made stronger and the threshold was left unchanged.
2. **E4 signal changed to counts.** The first answer key used at-risk MRR as the main evidence. An unplanted cluster of 3 Scale customers in dunning moved at-risk MRR as much at 2026-04 month-end ($2,074) as E4 did at 2026-02 ($2,025). The primary evidence is now counts: customers entering dunning, involuntary churn, and customers in dunning at month-end. At-risk MRR is only supporting evidence.
3. **E1 wording.** The expected signal is stated at driver level: price-change expansion concentrated in Starter, and the number of customers expanding. Total expansion dollars in 2025-09 ($747) were not above 2025-06 ($880), so "expansion spike" alone would have been wrong.
4. **E6 wording.** The refund's effect is measured against the month's gross figures, not as an absolute fall in revenue or cash, because the business is growing.
5. **Baseline churn and expansion tuned to published benchmarks.** At this point 12-month NRR was 65–77%, too low for the segment. Targets were set from published benchmarks before tuning (`benchmark_targets` in `scenario.yaml`):
   - **NRR median 90–106%:** SaaS Capital, *2025 B2B SaaS Retention Benchmarks* (Research Brief 32), Figure 2, ACV under $12k, 25th–75th percentile (median 98%). The survey covers more than 1,000 private B2B SaaS companies and excludes those under $1M ARR.
   - **GRR median 80–90%:** same report, Figure 1, ACV under $12k, median 90%. The floor is set at 80% because this company's ACV (about $1.9k) sits at the bottom of that bucket and its ARR (about $1M) is at the survey's cut-off.
   - **Monthly logo churn 3–5%:** Tomasz Tunguz, *The Innovator's Dilemma for SaaS Startups* (2015), SMB 3–7% monthly. He presents these as his "general observations", not a survey, so this is the weakest of the three sources. The lower half was used because the GRR benchmark points lower.

   Over the 18 reporting months:

   | Metric | Before | After | Target |
   |---|---|---|---|
   | Monthly logo churn (mean) | 4.12% | 3.33% | 3–5% |
   | NRR (median) | 75.3% | 102.2% | 90–106% |
   | GRR (median) | 71.5% | 84.3% | 80–90% |

   Inputs changed:
   - Churn: Growth 2.5% → 0.8% and Scale 1.2% → 0.4% a month. Starter rose from 4.5% to 5.0%, which concentrates churn in the smallest accounts, as is typical.
   - Upgrades 1.2% → 1.6%, seat adds 6% → 8%, downgrades 0.8% → 0.3%, seat removals 3% → 1%.
   - Card failure rate 4% → 3%, recovery per retry 35% → 60%.

   On the separate dev seed, the same inputs give 3.01% / 101.8% / 90.3%, so GRR moves by about ±5 points between random paths. Re-running the planted-event checks showed which signals weakened; they are listed in the Gate 3 notes.

**Prompt freeze.** The note prompt was frozen before any evaluation result was viewed. `notes/FREEZE.json` records the prompt's SHA-256, the model and the CLI version. Each note records the hash it was generated with, and the evaluator refuses to run on a mismatch. Before the freeze, the only check run was a format lint (length, citation syntax, digits only) on a separate development seed.

**What the AI does and does not do.** The facts file **pre-computes the top movers and the deltas against the trailing 3-month average** for every headline metric. The AI's job is to select, rank and narrate figures already computed. It does not discover anomalies in raw data.

**What counts as a hallucination.** Only two things: a number that matches no fact after display rounding, and a cause the cited facts don't show. An accurate mention of a movement that wasn't planted is not penalised.

## Sections to write on Day 9
- What it is and who it's for
- The accounting: MRR vs IFRS 15 revenue vs cash, and the three bridges
- Evaluation results: caught or missed, ungrounded numbers, word-numbers
- Limits: synthetic data, USD only, the simplifications in the metric definitions (D5 and others)
