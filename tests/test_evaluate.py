"""The evaluator's own rules (notes/evaluate.py), on a made-up facts list."""
from notes.evaluate import anomaly_claims, causal_claims, grounding

FACTS = [
    {"id": "F1", "group": "mrr_bridge", "label": "Closing MRR", "value": 72596, "display": "$72,596", "display_k": "$72.6k"},
    {"id": "F2", "group": "mrr_bridge", "label": "New MRR", "value": 4829, "display": "$4,829"},
    {"id": "F3", "group": "r1_mrr_to_revenue", "label": "R1 Timing", "value": -549, "display": "-$549"},
    {"id": "F4", "group": "vs_trailing_3m", "label": "Billings: times its trailing average", "value": 2.4, "display": "2.4×"},
    {"id": "F5", "group": "vs_trailing_3m", "label": "New MRR: times its trailing average", "value": 1.1, "display": "1.1×"},
    {"id": "F6", "group": "vs_trailing_3m", "label": "Logo churn: change vs trailing average", "value": 1.3, "display": "1.3 pts"},
    {"id": "F7", "group": "top_movers", "label": "Mover 1: customer", "value": "cus_AbC", "display": "cus_AbC"},
]


def statuses(text):
    return [(r["number"], r["status"]) for r in grounding(text, FACTS)]


def test_grounding_accepts_displayed_forms_and_flags_the_rest():
    assert statuses("MRR was $72,596 [F1], or $72.6k [F1], and 2.4 times [F4] on billings, "
                    "timing of $549 [F3] and churn up 1.3 pts [F6].") == [
        ("$72,596", "grounded"), ("$72.6k", "grounded"), ("2.4 times", "grounded"),
        ("$549", "grounded"), ("1.3 pts", "grounded")]
    assert statuses("New MRR was $4,829 [F1].") == [("$4,829", "miscited")]
    assert statuses("New MRR was $4,830 [F2].") == [("$4,830", "ungrounded")]
    assert statuses("New MRR was $4,829 this month.") == [("$4,829", "uncited")]


def test_anomaly_language_needs_a_fact_that_crosses_the_threshold():
    claims = anomaly_claims("Billings spiked to 2.4× [F4] their usual level. New MRR surged 1.1× [F5].", FACTS)
    assert [(c["words"], c["supported"]) for c in claims] == [(["spiked"], True), (["surged"], False)]
    assert anomaly_claims("Logo churn jumped 1.3 pts [F6].", FACTS)[0]["supported"]


def test_causal_claims_are_listed_for_review():
    assert causal_claims("MRR fell because of churn. Billings rose 2.4× [F4].") == ["MRR fell because of churn."]
