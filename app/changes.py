"""What changed since last time.

The thresholds matter more than they look. Hold queues wobble by a day or two
constantly — OverDrive's estimate is itself a guess — so a naive "the number is
different" comparison would cry wolf every single night and the alerts would be
worth nothing within a week. These only fire on movement big enough to act on.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from .config import settings

# A drop has to beat both of these to count.
WAIT_DROP_MIN_DAYS = 7
WAIT_DROP_MIN_FRACTION = 0.15
# Growth is noisier and less actionable, so the bar is higher.
WAIT_GROW_MIN_DAYS = 14
WAIT_GROW_MIN_FRACTION = 0.30

KIND_ORDER = [
    "now_available",
    "newly_holdable",
    "wait_dropped",
    "wait_grew",
    "no_longer_available",
    "left_catalogue",
]
GOOD_NEWS = {"now_available", "newly_holdable", "wait_dropped"}

# A book being borrowed by someone else, or dropped from a catalogue, is not
# something you can do anything about. Suppressed unless explicitly asked for.
REMOVAL_KINDS = {"no_longer_available", "left_catalogue"}


@dataclass
class Change:
    kind: str
    title: str
    author: str
    library: str
    scope_key: str
    before: str = ""
    after: str = ""
    wait_before: Optional[int] = None
    wait_after: Optional[int] = None

    @property
    def is_good(self) -> bool:
        return self.kind in GOOD_NEWS

    def sentence(self) -> str:
        who = f"{self.title}"
        if self.kind == "now_available":
            return f"{who} — available now at {self.library}"
        if self.kind == "newly_holdable":
            wait = f", ~{self.wait_after} day wait" if isinstance(self.wait_after, int) else ""
            return f"{who} — {self.library} now has it{wait}"
        if self.kind == "wait_dropped":
            return f"{who} — wait at {self.library} down from {_days(self.wait_before)} to {_days(self.wait_after)}"
        if self.kind == "wait_grew":
            return f"{who} — wait at {self.library} up from {_days(self.wait_before)} to {_days(self.wait_after)}"
        if self.kind == "no_longer_available":
            return f"{who} — no longer on the shelf at {self.library}"
        if self.kind == "left_catalogue":
            return f"{who} — {self.library} no longer lists it"
        return f"{who} — changed at {self.library}"

    def dict(self) -> dict:
        d = asdict(self)
        d["sentence"] = self.sentence()
        d["good"] = self.is_good
        return d


def _maybe(change: "Change") -> Optional["Change"]:
    """Drop removals unless this instance wants them."""
    if change.kind in REMOVAL_KINDS and not settings.report_removals:
        return None
    return change


def _days(n) -> str:
    if not isinstance(n, int):
        return "unknown"
    if n < 14:
        return f"{n} days"
    weeks = round(n / 7)
    return f"{weeks} weeks" if weeks < 9 else f"{round(n / 30)} months"


def compare(previous: dict, book_title: str, book_author: str, av) -> Optional[Change]:
    """previous is the stored row for this (book, scope), or None on a first run."""
    if not previous:
        return None  # nothing to compare against; a first sighting is not a change
    if av.status in ("unknown", "error"):
        return None  # a failed lookup is not news about the book

    was, now = previous.get("status"), av.status
    old_wait, new_wait = previous.get("wait_days"), av.wait_days

    base = dict(
        title=book_title, author=book_author, library=av.scope_name,
        scope_key=av.scope_key, before=was, after=now,
        wait_before=old_wait, wait_after=new_wait,
    )

    if now == "available" and was != "available":
        return Change(kind="now_available", **base)
    if now == "holdable" and was == "not_owned":
        return Change(kind="newly_holdable", **base)
    if was == "available" and now == "holdable":
        return _maybe(Change(kind="no_longer_available", **base))
    if was in ("available", "holdable") and now == "not_owned":
        return _maybe(Change(kind="left_catalogue", **base))

    if was == "holdable" and now == "holdable" and isinstance(old_wait, int) and isinstance(new_wait, int):
        delta = old_wait - new_wait
        if delta >= WAIT_DROP_MIN_DAYS and delta >= old_wait * WAIT_DROP_MIN_FRACTION:
            return Change(kind="wait_dropped", **base)
        grew = -delta
        if grew >= WAIT_GROW_MIN_DAYS and grew >= max(old_wait, 1) * WAIT_GROW_MIN_FRACTION:
            return Change(kind="wait_grew", **base)
    return None


def sort_changes(changes: list[Change]) -> list[Change]:
    """Good news first, in the order someone would want to act on it."""
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    return sorted(changes, key=lambda c: (rank.get(c.kind, 99), c.wait_after or 0, c.title))


def summarise(changes: list[Change]) -> str:
    """One line for a notification title."""
    if not changes:
        return "No changes"
    counts: dict[str, int] = {}
    for c in changes:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    bits = []
    if counts.get("now_available"):
        n = counts["now_available"]
        bits.append(f"{n} available now")
    if counts.get("newly_holdable"):
        bits.append(f"{counts['newly_holdable']} newly holdable")
    if counts.get("wait_dropped"):
        bits.append(f"{counts['wait_dropped']} shorter wait")
    if counts.get("wait_grew"):
        bits.append(f"{counts['wait_grew']} longer wait")
    if counts.get("no_longer_available") or counts.get("left_catalogue"):
        bits.append(f"{counts.get('no_longer_available', 0) + counts.get('left_catalogue', 0)} gone")
    return ", ".join(bits) or f"{len(changes)} changes"
