"""Orchestration: cache, concurrency, and the fallbacks that keep a bad day
from looking like a book vanishing from the catalogue.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Optional

import logging

from . import store
from .config import settings
from .matching import Book, surname
from .models import FORMAT_LABEL, Availability, BookIn, BookResult, Scope
from .providers.base import ProviderError, RateLimiter

log = logging.getLogger("shelfwatchr.service")

_provider = None
_semaphore: Optional[asyncio.Semaphore] = None


def get_provider():
    global _provider
    if _provider is None:
        if settings.mock:
            from .providers.mock import MockProvider

            _provider = MockProvider()
        else:
            from .providers.libby import LibbyProvider

            _provider = LibbyProvider(limiter=RateLimiter(
                settings.requests_per_minute,
                floor=settings.rpm_floor,
                ceiling=settings.rpm_ceiling,
                adaptive=settings.adaptive_rate,
            ))
    return _provider


def set_provider(provider) -> None:
    """Tests inject a fake here."""
    global _provider
    _provider = provider


def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_concurrency)
    return _semaphore


def to_book(b: BookIn) -> Book:
    from .csvimport import split_authors

    return Book(title=b.title, authors=tuple(split_authors(b.author)), isbn=b.isbn or "")


async def lookup_one(
    book: Book, scope: Scope, fmt: str, threshold: float, refresh: bool
) -> Availability:
    provider = get_provider()

    if not refresh:
        hit = store.cache_get(provider.name, scope.key, book.key, fmt)
        if hit:
            return hit

    async with get_semaphore():
        try:
            av = await provider.lookup(book, scope, fmt, threshold)
        except Exception as exc:  # noqa: BLE001 — one bad lookup must not sink the batch
            av = Availability(
                provider=provider.name,
                scope_key=scope.key,
                scope_name=scope.name,
                status="error",
                note=str(exc)[:120],
                checked_at=time.time(),
            )

    if av.status == "error":
        # Better to show what we last knew, clearly marked, than to imply the
        # library doesn't have the book.
        stale = store.cache_get_stale(provider.name, scope.key, book.key, fmt)
        if stale:
            stale.note = "lookup failed — showing last known"
            return stale
        av.status = "unknown"
        av.note = av.note or "lookup failed"
        return av

    store.cache_put(book.key, fmt, av)
    return av


async def lookup_book(
    b: BookIn, scopes: list[Scope], formats: list[str], threshold: float, refresh: bool
) -> BookResult:
    book = to_book(b)
    jobs = [
        lookup_one(book, scope, fmt, threshold, refresh)
        for scope in scopes
        for fmt in formats
    ]
    results = await asyncio.gather(*jobs)

    # One row per (library, format). They used to be collapsed to the best row
    # per library, which threw away the only thing the format toggle needs to
    # know: whether the audiobook or the ebook is the one you can borrow. The
    # collapsing still happens — in the browser, where it can be undone.
    order = {(s.key, f): (i, j) for i, s in enumerate(scopes) for j, f in enumerate(formats)}
    ordered = sorted(results, key=lambda av: order.get((av.scope_key, av.fmt), (99, 99)))
    return BookResult(title=b.title, author=b.author, key=book.key,
                      added=b.added, pages=b.pages, results=ordered)


# --------------------------------------------------------------- batch


async def lookup_chunk(
    books: list[BookIn], scopes: list[Scope], formats: list[str],
    threshold: float, refresh: bool, stats: Optional[dict] = None,
) -> list[BookResult]:
    """Every requested format, merged into one result per book.

    The batch pipeline below is single-format by nature — the title-id map, the
    search and the bulk availability call are all per format — so it runs once
    per format and the rows are concatenated. Sequentially, not concurrently:
    both runs draw on the same rate limiter, and interleaving them only changes
    which one waits.
    """
    formats = list(formats) or ["audiobook-overdrive"]
    merged: dict[str, BookResult] = {}
    for fmt in formats:
        for result in await _lookup_chunk_one(books, scopes, [fmt], threshold, refresh, stats):
            existing = merged.get(result.key)
            if existing is None:
                merged[result.key] = result
            else:
                existing.results.extend(result.results)
    # Input order, not dict order: the caller pages results by position.
    seen = []
    for b in books:
        key = to_book(b).key
        if key in merged and merged[key] not in seen:
            seen.append(merged[key])
    return seen


async def _lookup_chunk_one(
    books: list[BookIn], scopes: list[Scope], formats: list[str],
    threshold: float, refresh: bool, stats: Optional[dict] = None,
) -> list[BookResult]:
    """Look up a chunk of books using as few requests as possible.

    The naive shape is one search per (book, library, format) — for 24 books
    across 3 libraries that's 72 requests. This does it in roughly:

      * 0 requests for anything still fresh in the cache
      * 1 multi-library search per book whose OverDrive id we don't know yet
        (and that id is then remembered for a month, so most runs skip this)
      * 1 bulk availability request per library per chunk

    which for a warm chunk is 3 requests instead of 72.
    """
    def tally(field, n=1):
        if stats is not None:
            stats[field] = stats.get(field, 0) + n

    provider = get_provider()
    if not getattr(provider, "batch_ok", False) or not settings.use_batch:
        tally("searches", len(books) * max(len(scopes), 1))
        return await asyncio.gather(*[
            lookup_book(b, scopes, formats, threshold, refresh) for b in books
        ])

    fmt = formats[0] if formats else "audiobook-overdrive"
    pairs = [(b, to_book(b)) for b in books]
    results: dict[str, dict[str, Availability]] = {bk.key: {} for _, bk in pairs}

    # 1. Anything already cached and fresh needs no request at all.
    outstanding: dict[str, set] = {}
    for _, book in pairs:
        for scope in scopes:
            hit = None if refresh else store.cache_get(provider.name, scope.key, book.key, fmt)
            if hit:
                results[book.key][scope.key] = hit
                tally("cached")
            else:
                outstanding.setdefault(book.key, set()).add(scope.key)

    todo = [(b, bk) for b, bk in pairs if outstanding.get(bk.key)]
    if not todo:
        return _assemble(pairs, scopes, results)

    # 2. Resolve titles to OverDrive ids — cheap, because ids are remembered.
    known = store.title_map_get_many([bk.key for _, bk in todo], fmt, settings.ttl_title_map)
    unresolved = [(b, bk) for b, bk in todo if bk.key not in known]

    async def resolve(book_in, book):
        async with get_semaphore():
            try:
                found = await provider.search_across(book, [s.key for s in scopes], fmt, threshold)
            except Exception as exc:  # noqa: BLE001
                log.debug("multi-library search failed for %r: %s", book.title, exc)
                return book, None
        return book, found

    tally("searches", len(unresolved))
    resolved_now = await asyncio.gather(*[resolve(b, bk) for b, bk in unresolved])
    inline: dict[str, dict] = {}
    for book, found in resolved_now:
        if found:
            store.title_map_put(book.key, fmt, found["id"], found["title"], found["author"], found["score"])
            known[book.key] = {
                "title_id": found["id"], "matched_title": found["title"],
                "matched_author": found["author"], "score": found["score"],
            }
            if found["availability"]:
                inline[book.key] = found["availability"]
        else:
            # Remembering a miss matters as much as remembering a hit: it stops
            # us searching for the same absent book every single run.
            store.title_map_put(book.key, fmt, None)
            known[book.key] = {"title_id": None}

    # 3. One bulk availability request per library, per chunk.
    ids_by_scope: dict[str, list] = {}
    id_to_books: dict[str, list] = {}
    for _, book in todo:
        entry = known.get(book.key) or {}
        title_id = entry.get("title_id")
        if not title_id:
            continue
        id_to_books.setdefault(str(title_id), []).append(book)
        for scope_key in outstanding.get(book.key, ()):
            if book.key in inline and scope_key in inline[book.key]:
                continue  # the search already told us
            ids_by_scope.setdefault(scope_key, []).append(str(title_id))

    async def bulk(scope: Scope, ids: list):
        """Fetch availability in the largest batches the server will accept.

        The documented-by-nobody batch limit is discovered rather than assumed:
        start at the configured size, and if the server rejects the request as
        too large, halve it and try again. The working size sticks for the rest
        of the process, so the probing happens once.
        """
        out = {}
        start = 0
        while start < len(ids):
            size = max(8, provider.bulk_size or settings.bulk_availability_size)
            batch = ids[start:start + size]
            async with get_semaphore():
                try:
                    tally("bulk")
                    out.update(await provider.availability_bulk(scope.key, batch))
                except ProviderError as exc:
                    too_big = getattr(exc, "status", 0) in (400, 413, 414, 422, 431)
                    if too_big and size > 8:
                        provider.bulk_size = max(8, size // 2)
                        log.info("bulk of %d rejected (HTTP %s); using %d",
                                 size, exc.status, provider.bulk_size)
                        continue  # same offset, smaller bite
                    log.warning("bulk availability failed at %s: %s", scope.key, exc)
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("bulk availability failed at %s: %s", scope.key, exc)
                    raise
            if not provider.bulk_size:
                provider.bulk_size = size  # this size works; keep it
            start += size
        return scope, out

    bulk_jobs = [bulk(s, ids_by_scope[s.key]) for s in scopes if ids_by_scope.get(s.key)]
    bulk_out = await asyncio.gather(*bulk_jobs, return_exceptions=True)

    failed_scopes = set()
    availability_by_scope: dict[str, dict] = {}
    for outcome in bulk_out:
        if isinstance(outcome, Exception):
            continue
        scope, mapping = outcome
        availability_by_scope[scope.key] = mapping
    for scope in scopes:
        if ids_by_scope.get(scope.key) and scope.key not in availability_by_scope:
            failed_scopes.add(scope.key)

    # 4. Assemble, and fall back to single lookups for anything the bulk path
    #    couldn't answer.
    stragglers = []
    for book_in, book in todo:
        entry = known.get(book.key) or {}
        title_id = str(entry.get("title_id") or "")
        for scope in scopes:
            if scope.key not in outstanding.get(book.key, ()):
                continue
            if not title_id:
                av = Availability(
                    provider=provider.name, scope_key=scope.key, scope_name=scope.name,
                    status="not_owned", format=FORMAT_LABEL.get(fmt, fmt), fmt=fmt,
                    checked_at=time.time(),
                    note="no catalogue match",
                )
                results[book.key][scope.key] = av
                store.cache_put(book.key, fmt, av)
                continue

            raw = (inline.get(book.key) or {}).get(scope.key)
            if raw is None:
                raw = (availability_by_scope.get(scope.key) or {}).get(title_id)
            if raw is None:
                if scope.key in failed_scopes:
                    stragglers.append((book_in, scope))
                else:
                    # The library simply doesn't carry this title.
                    av = Availability(
                        provider=provider.name, scope_key=scope.key, scope_name=scope.name,
                        status="not_owned", format=FORMAT_LABEL.get(fmt, fmt), fmt=fmt,
                    checked_at=time.time(),
                    )
                    results[book.key][scope.key] = av
                    store.cache_put(book.key, fmt, av)
                continue

            av = provider.availability_from_raw(
                raw, scope, fmt, title_id=title_id,
                matched_title=entry.get("matched_title", ""),
                matched_author=entry.get("matched_author", ""),
                score=entry.get("score", 0.0) or 0.0,
            )
            results[book.key][scope.key] = av
            store.cache_put(book.key, fmt, av)

    if stragglers:
        log.info("falling back to single lookups for %d entries", len(stragglers))
        singles = await asyncio.gather(*[
            lookup_one(to_book(b), scope, fmt, threshold, refresh) for b, scope in stragglers
        ])
        for (b, scope), av in zip(stragglers, singles):
            results[to_book(b).key][scope.key] = av

    return _assemble(pairs, scopes, results)


def _assemble(pairs, scopes, results) -> list[BookResult]:
    out = []
    for book_in, book in pairs:
        ordered = [results[book.key][s.key] for s in scopes if s.key in results[book.key]]
        out.append(BookResult(
            title=book_in.title, author=book_in.author, key=book.key,
            added=book_in.added, pages=book_in.pages, results=ordered,
        ))
    return out


async def lookup_stream(
    books: list[BookIn], scopes: list[Scope], formats: list[str],
    threshold: float, refresh: bool,
) -> AsyncIterator[BookResult]:
    """Yield each book's result as it lands, so a long list fills in visibly."""
    tasks = [
        asyncio.create_task(lookup_book(b, scopes, formats, threshold, refresh))
        for b in books
    ]
    try:
        for coro in asyncio.as_completed(tasks):
            yield await coro
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()


async def lookup_all(
    books: list[BookIn], scopes: list[Scope], formats: list[str],
    threshold: float, refresh: bool, stats=None,
) -> list[BookResult]:
    """Every book, in the order given.

    Goes through lookup_chunk like the job runner does. It used to call
    lookup_book per title, which quietly meant the nightly refresh — the thing
    that runs against the biggest lists, every night — was the only caller not
    getting the bulk-availability batching. Same answers, a fraction of the
    requests.
    """
    size = max(1, settings.bulk_availability_size)
    out: list[BookResult] = []
    for start in range(0, len(books), size):
        out.extend(await lookup_chunk(
            books[start:start + size], scopes, formats, threshold, refresh, stats,
        ))
    return out


# ------------------------------------------------------------- grouping

def group_results(results: list[BookResult], short_wait_days: int) -> dict:
    groups = {"available": [], "short": [], "long": [], "none": []}
    for r in results:
        rank, wait = r.best_rank, r.best_wait
        if rank == 0:
            groups["available"].append(r)
        elif rank == 1 and isinstance(wait, int) and wait <= short_wait_days:
            groups["short"].append(r)
        elif rank == 1:
            groups["long"].append(r)
        else:
            groups["none"].append(r)
    groups["short"].sort(key=lambda r: r.best_wait or 9999)
    groups["long"].sort(key=lambda r: r.best_wait or 9999)
    return groups
