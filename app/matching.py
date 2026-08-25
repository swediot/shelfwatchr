"""Title/author normalisation and match scoring.

Kept deliberately free of I/O so it can be unit-tested on its own — this is the
part most likely to need tuning against a real catalogue.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

ARTICLES = ("the ", "a ", "an ")
NOISE = re.compile(
    r"\b(unabridged|abridged|audiobook|audio book|a novel|library edition|"
    r"book \d+|vol(?:ume)? \d+|the graphic novel)\b",
    re.I,
)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def norm_title(title: str, *, drop_subtitle: bool = False) -> str:
    t = strip_accents(title or "").lower()
    if drop_subtitle:
        t = re.split(r"[:(\[]", t, maxsplit=1)[0]
    t = NOISE.sub(" ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for art in ARTICLES:
        if t.startswith(art):
            t = t[len(art):]
            break
    return t


def surname(author: str) -> str:
    a = strip_accents(author or "").lower()
    a = re.sub(r"[^a-z ,]+", " ", a)
    if "," in a:  # "Le Guin, Ursula K."
        return a.split(",")[0].strip().split(" ")[-1]
    parts = [p for p in a.split() if p]
    return parts[-1] if parts else ""


def numbers_in(text: str) -> set:
    return set(re.findall(r"\d+", text))


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    # Word-level containment, scaled by how much of the longer title is covered,
    # so "the fifth season" in "the fifth season the broken earth" scores well
    # while "babel" in "babel 17" does not.
    ta, tb = set(a.split()), set(b.split())
    if ta and tb and (ta <= tb or tb <= ta):
        coverage = min(len(ta), len(tb)) / max(len(ta), len(tb))
        ratio = max(ratio, 0.78 + 0.20 * coverage)
    return ratio


@dataclass(frozen=True)
class Book:
    title: str
    authors: tuple = ()
    isbn: str = ""

    @property
    def key(self) -> str:
        return f"{norm_title(self.title, drop_subtitle=True)}|{surname(self.authors[0]) if self.authors else ''}"

    @property
    def author_display(self) -> str:
        return ", ".join(self.authors) if self.authors else ""

    @property
    def search_query(self) -> str:
        base = re.split(r"[:(\[]", self.title)[0].strip() or self.title
        first = self.authors[0] if self.authors else ""
        return f"{base} {first}".strip()


def score_candidate(book: Book, cand_title: str, cand_subtitle: str, cand_authors) -> float:
    """0-1 confidence that a catalogue item is the book we're looking for."""
    t_book = norm_title(book.title)
    t_book_short = norm_title(book.title, drop_subtitle=True)
    t_cand = norm_title(f"{cand_title} {cand_subtitle}".strip())
    t_cand_short = norm_title(cand_title, drop_subtitle=True)

    score = max(
        similarity(t_book, t_cand),
        similarity(t_book_short, t_cand_short),
        similarity(t_book_short, t_cand),
        similarity(t_book, t_cand_short),
    )

    # A series number on one side but not the other: Babel vs Babel-17.
    if numbers_in(t_book_short) != numbers_in(t_cand_short):
        score -= 0.18

    ours = {surname(a) for a in book.authors if surname(a)}
    theirs = {surname(a) for a in cand_authors if surname(a)}
    if not ours or not theirs:
        return score  # nothing to compare; don't penalise
    if ours & theirs:
        return min(1.0, score + 0.12)
    return score * 0.55  # same title, different author is almost always a different book


def estimate_wait_days(holds: int, owned: int) -> int:
    """Fallback when the catalogue doesn't report an estimate.

    Assumes ~18-day loans and that every copy turns over at that pace.
    """
    if owned <= 0:
        return 0
    turns = (holds + owned - 1) // owned
    return max(7, turns * 18)
