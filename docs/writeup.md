# The Revenue Bridge

**What it is.** A month-end finance pack for a SaaS company billed through Stripe. SQL ties MRR to IFRS 15 revenue, to billings, to net cash, every month, to the cent. An AI step writes each month's founder's update from those figures, and an evaluation scores it against a hidden answer key. The data is synthetic, in Stripe's format. It is for founders and finance teams who want to see a rigorous month-end, and for people hiring for startup finance roles who want to see how I work.

## Why I built it

What better way to understand startup finance than to build something that tracks it? I trained as an accountant (ACCA), and I wanted to see how a SaaS business reads month to month: what MRR is, how it becomes revenue, and how revenue becomes cash. Most dashboards stop at MRR. But MRR is a run rate. It is not revenue, which IFRS 15 recognises as service is delivered, and it is not cash: annual plans are paid up front, cards fail and refunds happen. So I wanted a pack that bridges them and matches each to the next, to the cent. I also wanted to test, honestly, where AI belongs in a finance pack.

## Design rules, and why I set them

- **SQL computes; the AI only narrates.** In finance a number has to be traceable, so the AI never calculates. It reads a facts file SQL has already computed and cites a fact after every number.
- **A test it cannot game.** I planted 8 events, including a price rise, a wave of failed cards, a large refund and a control month where nothing happens. I wrote down what a good update should notice and froze the prompt before any update was written, so I could not tune it after seeing the result. The evaluator refuses to run if the prompt changes.
- **Publish the misses.** The updates caught 19 of 20 primary signals. In November the update names the larger customers who left, but never says that gross MRR churn ran far above logo churn. Tuning the prompt until it passed would be teaching to the test, so the miss stays. For the same reason, all 6 changes I made to the answer key are listed, with dates.
- **Synthetic data, then real Stripe.** I did not want to use a real company's data, but synthetic data risks SQL fitted to its own generator. So I also ran the SQL, unchanged, on billing data created in a Stripe test account. It reconciled, and only the converter needed fixes.
- **Judgement calls on paper.** Where accounting leaves a choice, such as splitting refunds between delivered and unearned service, the choice and the standard behind it are in [metric_definitions.md](metric_definitions.md). I approved each before anything was built on it, so every number can be challenged back to a rule.

## What it shows

- All three bridges tie to $0.00 in all 30 months, and MRR matches the simulation's true state in all 7,257 customer-months.
- Across 18 updates, 427 of 427 numbers are grounded in a cited fact, with no invented numbers or causes.

## Limits

- Everything is synthetic and in USD, with one scenario and one run per month. 18 updates and 20 primary signals show a method, not a benchmark.
- **Provision matrix.** IFRS 9 asks for an allowance for invoices that may never be paid. I estimate it from history: of the invoices that had failed card payments, what share was eventually written off? Only 21 of 6,025 invoices were ever written off, so each rate rests on at most 10 invoices, and one more or fewer write-off swings it between 0% and about 40%. It also looks only backwards and makes no forecast. Collectability is not reassessed at each renewal, a stated simplification.
- **Real exports.** It reads Stripe-shaped tables and has run only on synthetic data and a small Stripe test account. It assumes USD, monthly or annual plans and no tax, so a real export needs a loader and a compatibility check first.
- The AI narrates figures SQL has already computed, including the trailing averages that make a movement notable. It does not find anomalies in raw data.

## Who did what

I set the accounting judgements, reviewed every SQL file and signed off each stage. The SQL, the pipeline and the page were written by Claude Code (Anthropic).

**Read more:** [methodology and disclosures](methodology.md) · [metric definitions](metric_definitions.md) · [README](../README.md)
