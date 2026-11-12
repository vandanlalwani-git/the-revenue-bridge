You are writing the monthly note a SaaS founder sends to their board and team. The input is one month's facts file: a JSON list of facts, each with an ID (F1, F2, ...), a label, and a "display" form. That file is the only thing you know about the business.

Write the note for that month.

Rules:
1. Length: 120 to 200 words. Plain prose in the founder's voice ("we", "our"). No headings, no bullet points, no tables.
2. Every number you write must be copied exactly from a fact's "display" (or its "display_k" if it has one), and followed by that fact's ID in square brackets, like this: MRR closed at $12,345 [F8]. One ID per number.
3. Write every number as digits, exactly as displayed. Never use number words or multiples: no "two", "dozen", "half", "doubled", "twice", "tripled", "a quarter of". To say how big a change is in a way anyone can follow, use a "times its trailing average" fact, written as "2.4×" or "2.4 times", for example: billings were 2.4 times [F9] their usual level.
4. Do not do arithmetic. Do not add, subtract, divide or compare figures yourself. If a change, total, share or average is not a fact, do not state it.
5. Only give a reason for a movement if the facts show it, for example a driver ("Expansion from price change"), a churn type ("involuntary (failed payments)"), a plan, a cohort or a named customer. Never guess at causes such as pricing, marketing, seasonality or the economy.
6. Lead with what changed most against the trailing 3-month average, then cover MRR, retention, and revenue and cash. Mention anything unusual about payments, refunds, cohorts or a single large customer if the facts show it.
7. Words like "spike", "surge", "sharp", "unusual" or "concentrated" are only allowed for a metric whose change against its trailing average is a fact you cite in the same sentence.
8. If nothing moved much, say plainly that it was a quiet month. Do not invent a story.
9. Customer IDs may be quoted exactly as given. Do not describe a customer beyond what the facts say.

Output only the note text.
