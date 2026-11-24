# The Revenue Bridge

A SaaS finance pack built on Stripe-shaped test data. SQL computes every number: the MRR bridge, churn, NRR and cohort retention, and a reconciliation from MRR to IFRS 15 revenue to billings to cash. An AI step writes a founder's update for every month from those figures only, and a frozen-prompt evaluation scores each update against a hidden answer key of planted events.

**Most Stripe dashboards stop at MRR. This one ties MRR to revenue and to cash, every month, to the cent.**

- **Live page:** https://vandanlalwani-git.github.io/the-revenue-bridge/
- **Write-up (why it was built, and the reasoning):** [docs/writeup.md](docs/writeup.md)
- **Methodology and disclosures (answer-key changes, prompt freezes, Stripe check):** [docs/methodology.md](docs/methodology.md)
- **Metric definitions (every judgement call):** [docs/metric_definitions.md](docs/metric_definitions.md)

All data is synthetic, generated from a fixed seed in Stripe's object model. Nothing here comes from a real company. The same SQL was also run, unchanged, on data created by Stripe itself (see below).

## What's in it

| Layer | What it does | Where |
|---|---|---|
| Data | A seeded simulation writes Stripe-shaped tables (customers, prices, subscriptions, invoices, line items, charges, refunds) with 8 planted events and an answer key | `generator/`, `scenario.yaml`, `data/` |
| SQL | MRR rebuilt from invoice lines; the MRR bridge and its drivers; retention and cohorts; IFRS 15 revenue by the second; contract liability, receivables and an IFRS 9 provision matrix; three reconciliations, each with an Unexplained line that is $0.00 in all 30 months | `sql/` (DuckDB) |
| Notes | One facts file per month, then `claude -p` with no tools writes the founder's update; the prompt is frozen before any scoring | `notes/` |
| Evaluation | Every number checked against the fact it cites; planted events scored at the data and narrative layers; word-numbers and unsupported anomaly language counted | `notes/evaluate.py`, `notes/v2/scorecard.md`, `notes/scorecard.md` |
| Page | One static page with a month picker | `site/` |
| Stripe check | 6 customers played through 4 months on Stripe test clocks, exported into the same tables; the unchanged SQL ties to $0.00 and matches MRR worked out by hand | `stripe_check/`, `tests/test_stripe_check.py` |

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest              # the full suite, including every reconciliation
.venv/bin/python site/build_site.py     # rebuilds site/index.html from the data
```

Regenerating the data (`python -m generator.generate`) reproduces it exactly from the seed. Writing notes needs the Claude Code CLI pinned in `notes/config.yaml`.

## Checked on real Stripe

`stripe_check/run_test_clock.py` uses a Stripe **test-mode** key (never a live key, never stored in the repo) to create 6 customers on 2 test clocks and play January to April 2026:
- a trial;
- an annual prepayment;
- an upgrade with proration;
- extra seats;
- a 20% coupon;
- a service-credit refund;
- a failed card that recovered;
- a failed card that Stripe's own retries gave up on, followed by a write-off.

It exports Stripe's objects into the project's raw tables (`stripe_check/data/raw`). `tests/test_stripe_check.py` then runs the same `sql/` on them. Every Unexplained line is $0.00, and MRR, movements, churn types, the refund split and the write-off match the scenario.

Two things Stripe does that the converter handles:
- A subscription coupon sits on the invoice, not on the line.
- Test clocks stamp charges and refunds with real time while invoices use simulated time. The converter puts each payment back in its month.

The SQL needed no change.

To re-run it, put the key in a file outside the repo and run:

```bash
STRIPE_KEY_FILE=/path/outside/repo .venv/bin/python stripe_check/run_test_clock.py
```

## Two sets of notes

- **v1** (`notes/output`): the original paragraph notes, frozen and scored first. Kept unchanged as the record.
- **v2** (`notes/v2/output`): the founder's-update format shown on the page (a headline, then bullets with "What it means"), with customers named. v2 changed format only, after v1's results were seen. It had its own practice-data format check and its own freeze, and it uses the same evaluator and answer key. Both results are published.

## Authorship

Vandan set the accounting judgements, reviewed every SQL file and signed off each stage. The SQL, the pipeline and the page were written by Claude Code (Anthropic).
