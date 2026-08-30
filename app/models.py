"""Shared shapes. Providers all speak this language, whatever they wrap."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, computed_field

Status = Literal["available", "holdable", "not_owned", "unknown", "error"]

# How the report ranks a book: the best status across all its scopes wins.
RANK = {"available": 0, "holdable": 1, "not_owned": 2, "unknown": 3, "error": 4}


class Scope(BaseModel):
    """A place a provider can look — for Libby, one library card."""

    provider: str = "libby"
    key: str
    name: str
    region: str = ""
    # public | college | company | school. Only the picker uses it, to say
    # which of the two libraries with the same name needs a student card.
    kind: str = ""


class BookIn(BaseModel):
    title: str
    author: str = ""
    isbn: str = ""
    added: str = ""        # ISO date from the export, for "added first/last"
    pages: Optional[int] = None


# The formats this app understands, and how they're written for a person.
FORMAT_LABEL = {
    "audiobook-overdrive": "Audiobook",
    "ebook-overdrive": "Ebook",
}
FORMATS = tuple(FORMAT_LABEL)


class Availability(BaseModel):
    provider: str = "libby"
    scope_key: str
    scope_name: str
    status: Status = "not_owned"
    format: str = ""      # display label: "Audiobook" / "Ebook"
    # The raw format key the lookup used. `format` is for reading; this is for
    # deciding, and the two used to be conflated — some code paths put the key
    # in `format` and some put the label, which nothing could filter on.
    fmt: str = ""

    available_copies: int = 0
    owned_copies: int = 0
    holds: int = 0
    wait_days: Optional[int] = None
    wait_estimated: bool = False
    lucky_day: int = 0

    url: str = ""
    title_id: str = ""     # OverDrive id, for building a share/app link
    duration_seconds: Optional[int] = None   # audiobook length, when reported
    matched_title: str = ""
    matched_author: str = ""
    match_score: float = 0.0

    checked_at: float = 0.0
    from_cache: bool = False
    note: str = ""

    @property
    def rank(self) -> int:
        return RANK.get(self.status, 9)


class BookResult(BaseModel):
    title: str
    author: str = ""
    key: str = ""
    added: str = ""
    pages: Optional[int] = None
    results: list[Availability] = Field(default_factory=list)

    @computed_field
    @property
    def duration_seconds(self) -> Optional[int]:
        """Length as reported by any library that had it."""
        for r in self.results:
            if r.duration_seconds:
                return r.duration_seconds
        return None

    @property
    def best_rank(self) -> int:
        return min((r.rank for r in self.results), default=9)

    @property
    def best_wait(self) -> Optional[int]:
        waits = [
            r.wait_days for r in self.results
            if r.status == "holdable" and isinstance(r.wait_days, int)
        ]
        return min(waits) if waits else None


class LookupRequest(BaseModel):
    books: list[BookIn]
    scopes: list[str]  # library keys
    formats: list[str] = Field(default_factory=lambda: ["audiobook-overdrive"])
    match_threshold: float = 0.78
    refresh: bool = False  # bypass the cache
    slug: str = ""         # optional: the saved list this run belongs to


class ProfileIn(BaseModel):
    name: str = ""
    scopes: list[Scope] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=lambda: ["audiobook-overdrive"])
    books: list[BookIn] = Field(default_factory=list)


class WatchIn(BaseModel):
    enabled: bool = True
    frequency: Literal["daily", "weekly"] = "daily"
    # What the automatic check looks at. Empty means whatever the list itself
    # was checked with — the behaviour before this field existed.
    formats: list[Literal["audiobook-overdrive", "ebook-overdrive"]] = []
    notify_type: Literal["none", "ntfy", "webhook", "email"] = "none"
    notify_target: str = ""
