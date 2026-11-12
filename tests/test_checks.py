"""The note format checks (notes/checks.py), on made-up notes."""
from notes.checks import format_problems, numbers_with_citations, word_numbers

IDS = {f"F{i}" for i in range(1, 60)}
FILLER = " ".join(["Our team kept shipping and support stayed steady through the month."] * 12)


def note(body: str) -> str:
    return f"{body} {FILLER}"


def test_a_clean_note_passes():
    text = note("MRR closed at $72,596 [F8], with 45 new customers [F12] and logo churn of 3.3% [F27]. "
                "Billings were 2.4 times [F50] their usual level and 2.4× [F50] on cash, up 1.3 pts [F40]. "
                "Our 2026-01 cohort and customer cus_AbC123 are named without numbers needing citations.")
    assert format_problems(text, IDS) == []


def test_uncited_numbers_and_unknown_ids_are_flagged():
    problems = format_problems(note("MRR closed at $72,596 and churn was 3.3% [F99]."), IDS)
    assert "number '$72,596' has no citation after it" in problems
    assert "cites F99, which is not in the facts file" in problems


def test_citation_must_follow_closely():
    pairs = numbers_with_citations("We added 45 customers on the Scale and Growth plans across the whole month [F12].")
    assert pairs == [("45", None)]
    assert numbers_with_citations("We added 45 new paying customers [F12].") == [("45", "F12")]


def test_metric_names_are_not_numbers():
    pairs = numbers_with_citations("Our 12-month NRR was 89.1% [F32], against a trailing 3-month average of 90.0% [F40].")
    assert pairs == [("89.1%", "F32"), ("90.0%", "F40")]
    assert numbers_with_citations("We lost 12 customers this month.") == [("12", None)]


def test_word_numbers_are_flagged_with_a_small_allowlist():
    assert word_numbers("Billings doubled, churn halved, twice the usual, three customers, a dozen") == \
        ["doubled", "halved", "twice", "three", "dozen"]
    assert word_numbers("A one-off refund; no one left; one of our largest accounts") == []


def test_length_and_layout():
    short = format_problems("MRR closed at $72,596 [F8].", IDS)
    assert any(p.startswith("length") for p in short)
    assert "uses bullets or headings" in format_problems(note("- MRR closed at $72,596 [F8]."), IDS)
