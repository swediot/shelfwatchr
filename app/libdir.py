"""The library directory: the bundled list, and the search over it.

Two things live here, and they are the two halves of the picker.

The list itself ships with the app, in data/libraries.tsv — every library that
is live on Libby, about 2,200 of them, written by tools/seed_libraries.py.
It used to be that a fresh install knew no libraries at all and learned them one
search at a time from OverDrive, which meant the picker's answers depended on
what somebody had typed before. Worse, it could not really learn: the upstream
search ignores `query` and returns the same first page whatever is asked, so a
library nobody had already found stayed unfindable. Shipping the list makes the
picker exhaustive on the first run, offline, on every deployment.

The search is ours because it has to be. It scores every entry in memory —
2,200 rows is nothing — which buys the things a SQL LIKE cannot do: matching
words in any order, ignoring accents and punctuation, initials ("nypl"), the
towns a consortium serves under a name that mentions none of them, knowing that
"st" and "saint" are the same word, and ranking Brooklyn Public Library above
the other names that merely contain "brooklyn".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

BUNDLE = Path(__file__).parent / "data" / "libraries.tsv"

# Words that are the same word. Both the name and the query fold to the value,
# so "st marys", "st. mary's" and "saint mary's" are one query, and a library
# spelled any of those ways is found by all of them.
SYNONYMS = {
    "saint": "st", "sainte": "st", "ste": "st",
    "mount": "mt", "fort": "ft",
    "libraries": "library", "librarys": "library",
    "cty": "county", "dist": "district", "univ": "university",
}

# US state codes spelled out, so "california" finds a library whose region
# reads "CA, USA". Only the US is stored code-shaped; every other region is
# already a full country name.
STATE_NAMES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island",
    "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas",
    "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
    "dc": "district of columbia washington",
}

# The other names a country goes by, so "England" and "Britain" reach the
# libraries filed under "United Kingdom".
REGION_ALIASES = {
    "usa": "united states america",
    "united kingdom": "uk britain great britain england scotland wales",
    "new zealand": "nz aotearoa",
    "czechia": "czech republic",
    "south korea": "korea",
    "united arab emirates": "uae dubai abu dhabi",
}

_NOT_WORD = re.compile(r"[^a-z0-9]+")
_APOSTROPHE = re.compile(r"['’ʼ]")


def fold(text: str) -> str:
    """Lowercase, unaccented, punctuation-free. "Zürich" and "zurich" meet here.

    Apostrophes close up rather than splitting the word, so "St Mary's" ends as
    "st marys" — which is what somebody types when they can't be bothered with
    the apostrophe, and it still prefix-matches when they type "mary".
    """
    flat = unicodedata.normalize("NFKD", text.lower())
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    return _NOT_WORD.sub(" ", _APOSTROPHE.sub("", flat)).strip()


def words(text: str) -> list[str]:
    return [SYNONYMS.get(w, w) for w in fold(text).split()]


def initials(name_words: list[str]) -> str:
    """NYPL, from New York Public Library. Joining words are skipped for the
    same reason nobody says "nyopl": they aren't part of how a name is said."""
    return "".join(w[0] for w in name_words if w not in ("of", "the", "at", "for", "and"))


def expand_region(region: str) -> str:
    """Every name a region answers to. "NY, USA" is also "new york" and
    "united states"."""
    if not region:
        return ""
    parts = [fold(region)]
    head, _, tail = region.partition(",")
    code = head.strip().lower()
    if tail and code in STATE_NAMES:
        parts.append(STATE_NAMES[code])
    for phrase, extra in REGION_ALIASES.items():
        if phrase in parts[0]:
            parts.append(extra)
    return " ".join(parts)


# What OverDrive's `type` means, in words a person would use. Half the
# directory is not a public library at all: colleges, companies, one school.
# They belong in the picker — somebody has that card — but not in front of the
# public library somebody is far more likely to be looking for, and the label
# is what explains why "New York Power Authority" is on the list at all.
KIND_LABEL = {"DLR": "public", "CDL": "college", "Corporate": "company", "SDL": "school"}


@dataclass(slots=True)
class Entry:
    key: str
    name: str
    region: str
    kind: str                 # public | college | company | school
    words: tuple[str, ...]    # the name, folded and split
    flat: str                 # the name, folded, one string
    key_flat: str
    initials: str
    country: frozenset        # every name this library's country answers to
    region_words: str         # the region, in all its spellings
    haystack: str             # name, key and every spelling of the region
    places: str               # towns this library serves, folded
    quality: float            # the part of the score no query changes


def build(rows) -> list[Entry]:
    """One Entry per (key, name, region, kind, places) row. Everything the
    search needs is computed once here, not per keystroke."""
    entries = []
    for row in rows:
        key, name, region = row[0], row[1], row[2]
        kind = row[3] if len(row) > 3 else ""
        places = row[4] if len(row) > 4 else ""
        name_words = words(name)
        flat = " ".join(name_words)
        key_flat = fold(key)
        region_words = expand_region(region)
        entries.append(Entry(
            key=key, name=name, region=region, kind=kind,
            words=tuple(name_words), flat=flat, key_flat=key_flat,
            initials=initials(name_words),
            country=country_names(region), region_words=region_words,
            haystack=" ".join(p for p in (flat, key_flat, region_words) if p),
            places=" ".join(words(places)) if places else "",
            quality=0.30 * (kind == "public") + 0.15 / (1.0 + len(flat) / 40.0),
        ))
    return entries


def country_names(region: str) -> frozenset:
    """What this library's country answers to — empty for a region that names a
    US state, which _is_country explains."""
    if not region or "," in region:
        return frozenset()
    flat = fold(region)
    return frozenset([flat, *REGION_ALIASES.get(flat, "").split()])


# Tiers, best first. The gap between them is wider than any within-tier score,
# so a better tier always wins however good the runner-up is.
KEY_EXACT, NAME_EXACT, COUNTRY_EXACT, INITIALS = 100, 90, 87, 85
NAME_PREFIX, ALL_WORDS, PLACE = 80, 70, 60
REGION, LOOSE, KEY_PART, FUZZY = 50, 40, 30, 10


# How good a hit is within its tier, in three parts. Entry.quality holds the
# two that no query changes: whether it is a library a person can walk into,
# which is what nearly everyone here is looking for, and how long the name is,
# because between "Denver Public Library" and "Denver Public Library
# Foundation" the plain one is the answer. This is the third — how much of the
# name the query accounts for. The three together stay under 1, so a within-tier
# score can never promote an entry past a tier that matched more squarely.
ALIGNMENT_WEIGHT = 0.55


def _word_alignment(entry: Entry, terms: list[str]) -> float:
    """How much of the name the query accounts for, or 0 if any query word is
    missing from it. Every word has to start some word of the name: "brook pub"
    finds Brooklyn Public Library, in that order or the other one; "xbrook"
    finds nothing."""
    unused = list(entry.words)
    covered = 0
    for term in terms:
        hit = next((w for w in unused if w.startswith(term)), None)
        if hit is None:
            return 0.0
        unused.remove(hit)
        covered += len(term)
    # Names that are mostly the query rank first: "Denver Public Library" over
    # "Denver Theological Seminary", which matches "denver" just as squarely.
    return covered / max(len(entry.flat), 1)


def _is_country(entry: Entry, joined: str) -> bool:
    """The whole query is the name of the country this library is in.

    It outranks a name that merely starts with the same word, because a query
    that is exactly a country is a query about the country: "canada" wants the
    hundred libraries in Canada before Cañada College, and "uk" wants British
    libraries before the University of Kent. Both are still reachable — "canada
    college" and "university of kent" are no longer country queries.

    Countries only. "Washington" is as likely to be the county, the city or the
    university as the state, so state names get no such promotion.
    """
    return joined in entry.country


@dataclass(slots=True)
class Query:
    """One query, prepared once for the whole directory instead of per entry.

    The patterns are why this is a class: compiling a word-boundary regex per
    library per keystroke was most of what a search spent its time on, and
    there are only ever a handful of distinct ones.
    """

    terms: list[str]
    joined: str
    squashed: str
    town_patterns: list
    region_patterns: list

    @classmethod
    def parse(cls, text: str):
        terms = words(text)
        if not terms:
            return None
        joined = " ".join(terms)
        return cls(
            terms=terms, joined=joined, squashed=joined.replace(" ", ""),
            # A town matches from its start, so the list narrows while the name
            # is still being typed: "andov" already finds Andover. Words of two
            # letters are dropped because towns are stored without them, and
            # the "st" of "st marys" would otherwise sink the whole query.
            town_patterns=[re.compile(rf"\b{re.escape(t)}") for t in terms if len(t) > 2],
            # A region has to match whole: "usa" is the country, not the tail
            # of "azusa", and nobody half-types a country they mean.
            region_patterns=[re.compile(rf"\b{re.escape(t)}\b") for t in terms],
        )


def _rank(entry: Entry, q: Query) -> float:
    if entry.key_flat in (q.joined, q.squashed):
        return KEY_EXACT                       # the slug, pasted from a Libby URL
    if entry.flat == q.joined:
        return NAME_EXACT
    if len(q.squashed) >= 2 and entry.initials == q.squashed:
        return INITIALS
    if _is_country(entry, q.joined):
        return COUNTRY_EXACT + entry.quality
    if entry.flat.startswith(q.joined + " "):
        return NAME_PREFIX + entry.quality
    aligned = _word_alignment(entry, q.terms)
    if aligned:
        return ALL_WORDS + entry.quality + ALIGNMENT_WEIGHT * aligned
    if entry.places and q.town_patterns and all(
            p.search(entry.places) for p in q.town_patterns):
        return PLACE + entry.quality
    if entry.region_words and all(
            p.search(entry.region_words) for p in q.region_patterns):
        return REGION + entry.quality
    if all(t in entry.haystack for t in q.terms):
        return LOOSE + entry.quality
    if q.squashed and q.squashed in entry.key_flat:
        return KEY_PART + entry.quality
    return 0.0


def search(entries: list[Entry], query: str, limit: int = 25) -> list[Entry]:
    q = Query.parse(query)
    if q is None:
        return []
    scored = [(score, e) for score, e in ((_rank(e, q), e) for e in entries) if score]
    if not scored:
        scored = _fuzzy(entries, q.joined)
    scored.sort(key=lambda pair: (-pair[0], len(pair[1].name), pair[1].name))
    return [e for _, e in scored[:limit]]


def _fuzzy(entries: list[Entry], joined: str) -> list[tuple[float, Entry]]:
    """Nothing matched, so the query is probably misspelled — "vancover". Only
    reached on a dead end, which is the one time comparing against every name
    is worth what it costs."""
    matcher = SequenceMatcher(b=joined, autojunk=False)
    out = []
    for entry in entries:
        for candidate in (entry.flat, entry.flat.split(" ")[0]):
            if abs(len(candidate) - len(joined)) > 4:
                continue
            matcher.set_seq1(candidate)
            if matcher.real_quick_ratio() < 0.7 or matcher.quick_ratio() < 0.7:
                continue
            ratio = matcher.ratio()
            if ratio >= 0.75:
                out.append((FUZZY + ratio, entry))
                break
    return out


# ------------------------------------------------------------- the bundle


COLUMNS = ("key", "name", "region", "kind", "places")


def read_bundle(path: Path = BUNDLE) -> list[tuple[str, ...]]:
    """The shipped directory, one tab-separated line per library.

    A missing file is not an error: the app then behaves the way it did before
    the bundle existed, learning libraries as they are searched for."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = (line.split("\t") + [""] * len(COLUMNS))[:len(COLUMNS)]
        if parts[0] and parts[1]:
            rows.append(tuple(part.strip() for part in parts))
    return rows


def write_bundle(rows, path: Path = BUNDLE) -> int:
    """Sorted by key, so a refresh reads in the diff as the libraries that
    changed rather than as a reshuffle of all of them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Every library live on Libby. Tab separated: " + ", ".join(COLUMNS) + ".",
        "# Written by tools/seed_libraries.py, loaded into the database at startup.",
    ]
    lines += ["\t".join(str(col).replace("\t", " ").strip() for col in row).rstrip("\t")
              for row in sorted(rows, key=lambda r: r[0])]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)
