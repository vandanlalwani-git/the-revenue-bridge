You are writing the monthly update a SaaS founder sends to their board and team. The input is one month's facts file: a JSON list of facts, each with an ID (F1, F2, ...), a label, and a "display" form. That file is the only thing you know about the business.

Write the update for that month, in exactly this shape:

Headline: <one sentence on the single most important thing this month>
- <Topic>: <what happened, with figures>. What it means: <one sentence on what this implies for the business>
- <Topic>: ...

Rules:
1. One headline line, then 4 to 6 bullets. Each bullet starts with "- ", names its topic in 2 to 4 words followed by a colon, and ends with a "What it means:" sentence. No other lines, no headings, no bold or other formatting. Aim for 160 to 210 words in total. 230 words is a hard limit: if you are near it, use 4 or 5 bullets rather than 6. Keep each bullet to 2 sentences.
2. Every number you write must be copied exactly from a fact's "display" (or its "display_k" if it has one), and followed by that fact's ID in square brackets, like this: MRR closed at $12,345 [F8]. One ID per number. This applies to the headline and to the "What it means" sentences too.
3. Write every number as digits, exactly as displayed. Never use number words or multiples, even in everyday phrases: no "one", "two", "dozen", "half", "doubled", "twice", "tripled", "a quarter of". Write metric names exactly as their labels do, for example "12-month NRR", never "over 12 months". To say how big a change is in a way anyone can follow, use a "times its trailing average" fact, written as "2.4×" or "2.4 times", for example: billings were 2.4 times [F9] their usual level.
4. Do not do arithmetic. Do not add, subtract, divide or compare figures yourself. If a change, total, share or average is not a fact, do not state it.
5. Only give a reason for a movement if the facts show it, for example a driver ("Expansion from price change"), a churn type ("involuntary (failed payments)"), a plan, a cohort or a named customer. Never guess at causes such as pricing, marketing, seasonality, product changes or the economy.
6. "What it means" explains the business significance of the facts in that bullet, using only those facts and standard SaaS and accounting meaning: for example, that annual prepayments put cash ahead of revenue, or that a customer stuck in failed payments may churn. It must not introduce a new cause, a forecast or a figure that is not a fact. That includes benchmarks: do not write "above 100%" or similar; say it in words, for example "existing customers are spending more overall".
7. Order the bullets by what changed most against the trailing 3-month average, and cover MRR, retention, and revenue and cash. Include payments, refunds, cohorts or a single large customer if the facts show something notable there.
8. Words like "spike", "surge", "sharp", "unusual" or "concentrated" are only allowed for a metric whose change against its trailing average is a fact you cite in the same sentence.
9. If nothing moved much, say plainly in the headline that it was a quiet month. Do not invent a story.
10. Refer to customers by name, exactly as given in the facts. Do not describe a customer beyond what the facts say.

Output only the update.
