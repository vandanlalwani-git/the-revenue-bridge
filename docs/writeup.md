# The Revenue Bridge: Write-up

*Draft skeleton. The final 300–500 word write-up is written on Day 9. The methodology statements below are required and must appear in the final version, condensed if needed.*

## Methodology: required disclosures

**Test data.** All data is synthetic. A seeded generator writes it in Stripe's object model (seed 20260129). Nothing comes from a real company.

**Answer key timing.** The expected signals in the answer key were finalised **after the data was generated and before any note was written**. Each adjustment was made on 2026-10-29, after the first generation of the test seed, and is listed below so the reader can judge them:

1. **E3 cliff rate raised from 0.35 to 0.45.** On first generation, the January 2026 promo cohort's logo-retention drop from M2 to M3 was 19.4 points, against a 20-point check. Non-promo customers in the same cohort diluted it. The planted event was made stronger and the threshold was left unchanged.
2. **E4 signal changed to counts.** The first answer key used at-risk MRR as the main evidence. An unplanted cluster of 3 Scale customers in dunning moved at-risk MRR as much at 2026-04 month-end ($2,074) as E4 did at 2026-02 ($2,025). The primary evidence is now counts: customers entering dunning, involuntary churn, and customers in dunning at month-end. At-risk MRR is only supporting evidence.
3. **E1 wording.** The expected signal is stated at driver level: price-change expansion concentrated in Starter, and the number of customers expanding. Total expansion dollars in 2025-09 ($747) were not above 2025-06 ($880), so "expansion spike" alone would have been wrong.
4. **E6 wording.** The refund's effect is measured against the month's gross figures, not as an absolute fall in revenue or cash, because the business is growing. The key also notes an unplanted $4,036 pro-rata annual refund in the same month, which is unearned and does not reduce revenue.

**Prompt freeze.** The note prompt was frozen before any evaluation result was viewed. `notes/FREEZE.json` records the prompt's SHA-256, the model and the CLI version. Each note records the hash it was generated with, and the evaluator refuses to run on a mismatch. Before the freeze, the only check run was a format lint (length, citation syntax, digits only) on a separate development seed.

**What the AI does and does not do.** The facts file **pre-computes the top movers and the deltas against the trailing 3-month average** for every headline metric. The AI's job is to select, rank and narrate figures already computed. It does not discover anomalies in raw data.

**What counts as a hallucination.** Only two things: a number that matches no fact after display rounding, and a cause the cited facts don't show. An accurate mention of a movement that wasn't planted is not penalised.

## Sections to write on Day 9
- What it is and who it's for
- The accounting: MRR vs IFRS 15 revenue vs cash, and the three bridges
- Evaluation results: caught or missed, ungrounded numbers, word-numbers
- Limits: synthetic data, USD only, the simplifications in the metric definitions (D5 and others)
