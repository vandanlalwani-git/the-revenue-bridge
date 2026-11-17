"""Format checks shared by the pre-freeze lint and notes/evaluate.py (PLAN §5.3, §5.5).

These look only at the note's form: length, citation syntax, digits-only numbers. They never
look at the answer key, so running them before the freeze can't tune the prompt to the test.
"""
from __future__ import annotations

import re

MIN_WORDS, MAX_WORDS = 120, 200
CITATION = re.compile(r"\[(F\d+)\]")

# A number as a note may write it: $72,596  $72.6k  3.3%  1.3 pts  2.4×  2.4 times  45  -$549
NUMBER = re.compile(r"(?<![\w.\-])-?\$?\d[\d,]*(?:\.\d+)?(?:k\b|%|×|\s?pts\b|\s?times\b)?")
# Not numbers for these purposes: dates and cohort labels (2026-03, 2026), customer IDs, and the
# metric windows the facts themselves use ("12-month NRR", "trailing 3-month average", "12 months ago").
# v1 was scored with the narrower exemption ("12-month" only); v2 also exempts "12 months" (added before v2's freeze).
EXEMPT_V1 = re.compile(r"\b20\d\d(?:-\d\d)?\b|\bcus_\w+|\[F\d+\]|\b(?:3|12)-month\b")
EXEMPT = re.compile(r"\b20\d\d(?:-\d\d)?\b|\bcus_\w+|\[F\d+\]|\b(?:3|12)[- ]months?\b")

WORD_NUMBERS = re.compile(
    r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundreds?|thousands?|millions?|dozens?|half|halved|halving|double[ds]?|doubling|triple[ds]?|"
    r"tripling|twice|thrice|quarters?)\b", re.I)
WORD_NUMBER_ALLOWLIST = re.compile(r"\bone-off\b|\bno one\b|\bone of our\b", re.I)
MAX_WORDS_TO_CITATION = 6


def words(note: str) -> int:
    return len(re.findall(r"\b[\w'’$%.,×-]+\b", CITATION.sub("", note)))


def numbers_with_citations(note: str, exempt: re.Pattern = EXEMPT_V1) -> list[tuple[str, str | None]]:
    """Each number in the note with the fact ID cited after it (within a few words), or None."""
    masked = exempt.sub(lambda m: m.group(0) if m.group(0).startswith("[F") else " " * len(m.group(0)), note)
    found = [(m.start(), m.end(), m.group(0).strip()) for m in NUMBER.finditer(masked)]
    out = []
    for i, (start, end, token) in enumerate(found):
        stop = found[i + 1][0] if i + 1 < len(found) else len(note)
        window = note[end:stop]
        cite = CITATION.search(window)
        if cite and len(re.findall(r"\b\w+\b", window[:cite.start()])) <= MAX_WORDS_TO_CITATION:
            out.append((token, cite.group(1)))
        else:
            out.append((token, None))
    return out


def word_numbers(note: str) -> list[str]:
    cleaned = WORD_NUMBER_ALLOWLIST.sub(" ", note)
    return [m.group(0) for m in WORD_NUMBERS.finditer(cleaned)]


def format_problems(note: str, fact_ids: set[str], min_words: int = MIN_WORDS, max_words: int = MAX_WORDS,
                    bullets: bool = False, exempt: re.Pattern = EXEMPT_V1) -> list[str]:
    """v1 notes are paragraphs. v2 notes are a 'Headline:' line, then 4-6 '- ' bullets, each with 'What it means:'."""
    problems = []
    n = words(note)
    if not min_words <= n <= max_words:
        problems.append(f"length {n} words, outside {min_words}-{max_words}")
    for fid in CITATION.findall(note):
        if fid not in fact_ids:
            problems.append(f"cites {fid}, which is not in the facts file")
    for token, fid in numbers_with_citations(note, exempt):
        if fid is None:
            problems.append(f"number {token!r} has no citation after it")
    for w in word_numbers(note):
        problems.append(f"word-number {w!r}")
    if not bullets:
        if re.search(r"^\s*([-*•]|#)", note, re.M):
            problems.append("uses bullets or headings")
        return problems
    lines = [l.strip() for l in note.strip().splitlines() if l.strip()]
    if not lines or not lines[0].startswith("Headline: "):
        problems.append("does not start with 'Headline: '")
    items = [l for l in lines[1:] if l.startswith("- ")]
    if len(items) != len(lines) - 1:
        problems.append("has lines that are neither the headline nor a bullet")
    if not 4 <= len(items) <= 6:
        problems.append(f"{len(items)} bullets, not 4-6")
    for item in items:
        if "What it means:" not in item:
            problems.append(f"bullet without 'What it means:': {item[:40]!r}")
    if re.search(r"^\s*#", note, re.M):
        problems.append("uses headings")
    return problems
