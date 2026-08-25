"""A fake catalogue, so the app runs and the tests pass with no network.

Set SHELFWATCH_MOCK=1. Useful for demoing the interface and for developing the
frontend without pestering OverDrive.
"""

from __future__ import annotations

import asyncio
import os
import time
import zlib

from ..matching import Book, estimate_wait_days, score_candidate
from ..models import Availability, Scope
from .base import ProviderError
from .libby import FORMAT_LABEL

LIBRARIES = [
    Scope(key="westmount", name="Westmount Public Library", region="Québec, Canada"),
    Scope(key="queenslibrary", name="Queens Public Library", region="New York, USA"),
    Scope(key="aubora", name="Aubora (Zürich)", region="Switzerland"),
    Scope(key="brooklyn", name="Brooklyn Public Library", region="New York, USA"),
    Scope(key="nypl", name="New York Public Library", region="New York, USA"),
]

# Every title resolves, with a spread of availability — for exercising the
# interface at 1,000+ books.
# Every other setting is SHELFWATCH_*; this one shipped with the extra R and
# the mismatch is a trap — a typo here silently gives you a three-book
# catalogue instead of an error. Both spellings work.
SYNTHETIC = (os.environ.get("SHELFWATCH_MOCK_SYNTHETIC")
             or os.environ.get("SHELFWATCHR_MOCK_SYNTHETIC")
             or "").lower() in ("1", "true", "yes")

# Pretend each lookup costs something, so progress, cancellation and rejoining a
# run can be exercised without a real catalogue on the other end.
try:
    DELAY = float(os.environ.get("SHELFWATCHR_MOCK_DELAY_MS", "0")) / 1000
except ValueError:
    DELAY = 0.0

CATALOGUE = {
    "westmount": [
        dict(id="w1", title="Piranesi", author="Susanna Clarke",
             available=1, owned=2, holds=0, wait=0),
        dict(id="w2", title="The Fifth Season", subtitle="The Broken Earth, Book 1",
             author="N. K. Jemisin", available=0, owned=1, holds=6, wait=None),
    ],
    "queenslibrary": [
        dict(id="q1", title="The Fifth Season", author="N. K. Jemisin",
             available=0, owned=4, holds=12, wait=38),
        dict(id="q2", title="Babel", subtitle="Or the Necessity of Violence",
             author="R. F. Kuang", available=2, owned=6, holds=0, wait=0),
        dict(id="q3", title="Piranesi", author="Susanna Clarke",
             available=0, owned=3, holds=2, wait=11),
    ],
    "aubora": [
        dict(id="a1", title="Babel-17", author="Samuel R. Delany",
             available=1, owned=1, holds=0, wait=0),  # must not match Babel
        dict(id="a2", title="Der Schwarm", author="Frank Schätzing",
             available=0, owned=2, holds=9, wait=95),
    ],
    "brooklyn": [
        dict(id="b1", title="The Fifth Season", author="N. K. Jemisin",
             available=1, owned=5, holds=0, wait=0),
    ],
    "nypl": [],
}


FMT_TAG = {"audiobook-overdrive": "a", "ebook-overdrive": "e"}


def _synthetic(book: Book, scope: Scope, fmt: str = "audiobook-overdrive") -> dict:
    """A deterministic pseudo-catalogue: every book exists somewhere, with a
    spread of availability. Lets the interface be exercised at real list sizes
    without a real catalogue behind it."""
    seed = zlib.crc32(f"{scope.key}:{book.key}:{fmt}".encode())
    roll = seed % 100
    if roll < 12:
        return dict(available=1 + seed % 3, owned=3 + seed % 5, holds=0, wait=0)
    if roll < 22:
        return dict(available=0, owned=0, holds=0, wait=None)          # not owned here
    if roll < 55:
        return dict(available=0, owned=1 + seed % 4, holds=1 + seed % 9, wait=3 + seed % 25)
    return dict(available=0, owned=1 + seed % 3, holds=5 + seed % 40, wait=30 + seed % 200)


class MockProvider:
    name = "libby"
    # Set SHELFWATCHR_MOCK_NO_BATCH=1 to exercise the fallback path.
    batch_ok = os.environ.get("SHELFWATCHR_MOCK_NO_BATCH", "").lower() not in ("1", "true", "yes")
    batch_note = "mock"
    bulk_size = 0

    # A pretend server that rejects batches over this size, so the probing
    # logic has something to discover.
    MAX_BULK = int(os.environ.get("SHELFWATCHR_MOCK_MAX_BULK", "0") or 0)

    async def aclose(self) -> None:
        return None

    async def search_scopes(self, query: str) -> list[Scope]:
        q = query.strip().lower()
        return [s for s in LIBRARIES if q in s.name.lower() or q in s.key.lower()] or []

    async def lookup(self, book: Book, scope: Scope, fmt: str, threshold: float) -> Availability:
        if DELAY:
            await asyncio.sleep(DELAY)
        if SYNTHETIC:
            info = _synthetic(book, scope, fmt)
            av = Availability(
                provider=self.name, scope_key=scope.key, scope_name=scope.name,
                format=FORMAT_LABEL.get(fmt, fmt), fmt=fmt, checked_at=time.time(),
                matched_title=book.title, matched_author=book.author_display,
                match_score=0.95, available_copies=info["available"],
                owned_copies=info["owned"], holds=info["holds"],
                title_id=str(abs(zlib.crc32(book.key.encode())) % 9999999),
                duration_seconds=3600 * 2 + (zlib.crc32(book.key.encode()) % 54000),
                url=f"https://libbyapp.com/library/{scope.key}/everything/page-1/000",
            )
            if info["owned"] == 0:
                av.status = "not_owned"
            elif info["available"] > 0:
                av.status, av.wait_days = "available", 0
            else:
                av.status, av.wait_days = "holdable", info["wait"]
            return av

        av = Availability(
            provider=self.name,
            scope_key=scope.key,
            scope_name=scope.name,
            format=FORMAT_LABEL.get(fmt, fmt), fmt=fmt,
            checked_at=time.time(),
        )
        best, best_score = None, 0.0
        for item in CATALOGUE.get(scope.key, []):
            score = score_candidate(book, item["title"], item.get("subtitle", ""), [item["author"]])
            if score > best_score:
                best, best_score = item, score

        av.match_score = round(best_score, 2)
        if not best or best_score < threshold:
            av.status = "not_owned"
            return av

        av.matched_title = best["title"]
        av.matched_author = best["author"]
        av.title_id = str(best["id"])
        av.duration_seconds = best.get("duration") or (
            3600 * 3 + (zlib.crc32(book.key.encode()) % 46000))
        av.url = f"https://libbyapp.com/library/{scope.key}/everything/page-1/{best['id']}"
        av.available_copies = best["available"]
        av.owned_copies = best["owned"]
        av.holds = best["holds"]

        if best["available"] > 0:
            av.status = "available"
            av.wait_days = 0
        elif best["owned"] > 0:
            av.status = "holdable"
            if best["wait"] is not None:
                av.wait_days = best["wait"]
            else:
                av.wait_days = estimate_wait_days(best["holds"], best["owned"])
                av.wait_estimated = True
        else:
            av.status = "not_owned"
        return av


    async def search_across(self, book: Book, scope_keys: list, fmt: str, threshold: float):
        """One call that covers every library, mirroring the real endpoint."""
        if DELAY:
            await asyncio.sleep(DELAY)
        if SYNTHETIC:
            # The id carries the format, as a real OverDrive id does: the
            # audiobook and the ebook are separate records with separate queues.
            return {"id": f"syn-{FMT_TAG.get(fmt, 'x')}-{abs(zlib.crc32(book.key.encode()))}",
                    "title": book.title,
                    "author": book.author_display, "score": 0.95, "availability": {}}

        best, best_score, best_id = None, 0.0, ""
        for scope_key in scope_keys:
            for item in CATALOGUE.get(scope_key, []):
                score = score_candidate(book, item["title"], item.get("subtitle", ""), [item["author"]])
                if score > best_score:
                    best, best_score, best_id = item, score, item["id"]
        if not best or best_score < threshold:
            return None
        # The fixture catalogue keys entries per library, so a shared id needs
        # synthesising: use the title, which is what the real global id stands in for.
        return {"id": f"t-{FMT_TAG.get(fmt, 'x')}-{norm_key(best['title'])}", "title": best["title"],
                "author": best["author"], "score": round(best_score, 2), "availability": {}}

    async def availability_bulk(self, scope_key: str, title_ids: list) -> dict:
        if self.MAX_BULK and len(title_ids) > self.MAX_BULK:
            raise ProviderError(f"HTTP 413 ({len(title_ids)} > {self.MAX_BULK})", status=413)
        if DELAY:
            await asyncio.sleep(DELAY)
        out = {}
        for title_id in title_ids:
            if SYNTHETIC:
                info = _synthetic_by_id(title_id, scope_key)
                out[title_id] = _raw(info)
                continue
            for item in CATALOGUE.get(scope_key, []):
                if title_id.endswith(f"-{norm_key(item['title'])}") and title_id.startswith("t-"):
                    out[title_id] = _raw(item)
        return out

    def availability_from_raw(self, raw: dict, scope: Scope, fmt: str, *, title_id: str = "",
                              matched_title: str = "", matched_author: str = "", score: float = 0.0):
        av = Availability(
            provider=self.name, scope_key=scope.key, scope_name=scope.name,
            format=FORMAT_LABEL.get(fmt, fmt), fmt=fmt, checked_at=time.time(),
            matched_title=matched_title, matched_author=matched_author, match_score=score,
            available_copies=raw.get("availableCopies", 0), owned_copies=raw.get("ownedCopies", 0),
            holds=raw.get("holdsCount", 0),
            title_id=str(title_id),
            duration_seconds=3600 * 2 + (zlib.crc32(str(title_id).encode()) % 54000),
            url=f"https://libbyapp.com/library/{scope.key}/everything/page-1/{title_id}",
        )
        if av.owned_copies == 0 and not raw.get("isAvailable"):
            av.status = "not_owned"
        elif raw.get("isAvailable") or av.available_copies > 0:
            av.status, av.wait_days = "available", 0
        else:
            av.status = "holdable"
            wait = raw.get("estimatedWaitDays")
            if isinstance(wait, int):
                av.wait_days = wait
            else:
                av.wait_days = estimate_wait_days(av.holds, av.owned_copies)
                av.wait_estimated = True
        return av



def norm_key(title: str) -> str:
    return "".join(c for c in title.lower() if c.isalnum())


def _raw(item: dict) -> dict:
    return {
        "isAvailable": item["available"] > 0,
        "availableCopies": item["available"],
        "ownedCopies": item["owned"],
        "holdsCount": item["holds"],
        "estimatedWaitDays": item.get("wait"),
    }


def _synthetic_by_id(title_id: str, scope_key: str) -> dict:
    seed = zlib.crc32(f"{scope_key}:{title_id}".encode())
    roll = seed % 100
    if roll < 12:
        return dict(available=1 + seed % 3, owned=3 + seed % 5, holds=0, wait=0)
    if roll < 22:
        return dict(available=0, owned=0, holds=0, wait=None)
    if roll < 55:
        return dict(available=0, owned=1 + seed % 4, holds=1 + seed % 9, wait=3 + seed % 25)
    return dict(available=0, owned=1 + seed % 3, holds=5 + seed % 40, wait=30 + seed % 200)
