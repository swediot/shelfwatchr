"""Background lookup jobs.

A 1,200-book list across three libraries is ~3,600 catalogue queries. At the
polite rate this server holds itself to, that's most of an hour — far longer
than anyone will sit on a page with a spinner, and much longer than a phone will
keep a connection open.

So a lookup is a *job*, not a request. The work runs server-side and writes each
result to SQLite as it lands. The page subscribes for live progress, but the
progress is a view of the job, not the job itself: close the tab, lose the
connection, come back on a different device, and the work is still going and the
results are all there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from . import store
from .config import settings
from .models import BookIn, BookResult, Scope
from .service import lookup_chunk

log = logging.getLogger("shelfwatchr.jobs")

# job_id -> asyncio.Event, fired whenever a result lands, so streams wake up
# without polling the database.
_waiters: dict[str, asyncio.Event] = {}
_tasks: dict[str, asyncio.Task] = {}


def _wake(job_id: str) -> None:
    event = _waiters.get(job_id)
    if event:
        event.set()
        _waiters[job_id] = asyncio.Event()


def waiter(job_id: str) -> asyncio.Event:
    return _waiters.setdefault(job_id, asyncio.Event())


async def start(
    books: list[BookIn], scopes: list[Scope], formats: list[str],
    threshold: float, refresh: bool, *, slug: str = "",
) -> str:
    job_id = store.job_create(books, scopes, formats, slug=slug)
    _tasks[job_id] = asyncio.create_task(_run(job_id, books, scopes, formats, threshold, refresh))
    return job_id


async def _run(job_id, books, scopes, formats, threshold, refresh) -> None:
    started = time.monotonic()
    try:
        # Books go through in chunks, because that's what lets one bulk
        # availability request answer for two dozen of them at once.
        #
        # Cheap work first: books whose OverDrive id we already know need only a
        # bulk availability call, so hundreds of them resolve in the time a
        # single unknown title takes to search. Doing those first means a mostly
        # complete report exists within a minute or two, with newly-seen titles
        # filling in behind it. Results are written at their original index, so
        # the report's order never depends on this.
        order = _cheapest_first(books, formats)
        size = max(1, settings.bulk_availability_size)
        for start in range(0, len(order), size):
            if store.job_is_cancelled(job_id):
                store.job_finish(job_id, "cancelled")
                _wake(job_id)
                return
            batch = order[start:start + size]
            chunk = [books[i] for i in batch]
            stats = {}
            try:
                results = await lookup_chunk(chunk, scopes, formats, threshold, refresh, stats)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad chunk can't sink the rest
                log.warning("job %s: chunk at %d failed: %s", job_id, start, exc)
                results = [BookResult(title=b.title, author=b.author, key="", results=[])
                           for b in chunk]
            # zip() would truncate silently, writing later results at indices
            # belonging to earlier books. If the counts ever disagree, the
            # results are unusable — say so rather than storing them shifted.
            if len(results) != len(chunk):
                log.error("job %s: chunk at %d returned %d results for %d books; dropping",
                          job_id, start, len(results), len(chunk))
                results = [BookResult(title=b.title, author=b.author, key="", results=[])
                           for b in chunk]
            store.job_add_results(job_id, list(zip(batch, results)))
            store.job_add_stats(job_id, stats)
            _wake(job_id)
        store.job_finish(job_id, "done")
        log.info("job %s finished: %d books in %.0fs", job_id, len(books), time.monotonic() - started)
    except asyncio.CancelledError:
        store.job_finish(job_id, "cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s failed", job_id)
        store.job_finish(job_id, "failed", str(exc)[:200])
    finally:
        _wake(job_id)
        _tasks.pop(job_id, None)
        # Streams hold their own reference while running; dropping ours stops
        # one Event per job ever started accumulating for the process lifetime.
        _waiters.pop(job_id, None)


def cancel(job_id: str) -> bool:
    store.job_cancel(job_id)
    task = _tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    _wake(job_id)
    return True


async def stream(job_id: str, from_index: int = 0):
    """Yield SSE frames: everything already done, then each new result.

    Resumable by design — pass the number of results you already have and you
    get the rest, whether you're reconnecting after a dropped connection or
    opening the report on your phone an hour later.
    """
    job = store.job_get(job_id)
    if not job:
        yield _sse("error", {"message": "no such job"})
        return

    yield _sse("start", {
        "job_id": job_id, "total": job["total"], "done": job["done"], "state": job["state"],
        **_progress(job),
    })

    sent = from_index
    idle_since = time.monotonic()
    while True:
        batch = store.job_results(job_id, after=sent, limit=200)
        if batch:
            idle_since = time.monotonic()
            books = []
            for row in batch:
                sent = max(sent, row["seq"])
                book = json.loads(row["payload"])
                book["idx"] = row["idx"]   # where it belongs in the report
                books.append(book)
            job = store.job_get(job_id) or job
            yield _sse("results", {
                "books": books, "done": job["done"], "total": job["total"], "cursor": sent,
                **_progress(job),
            })
            continue  # drain before waiting

        job = store.job_get(job_id) or job
        if job["state"] in ("done", "failed", "cancelled") and not store.job_results(job_id, after=sent, limit=1):
            yield _sse("done", {"state": job["state"], "done": job["done"],
                                "total": job["total"], "error": job["error"]})
            return

        try:
            await asyncio.wait_for(waiter(job_id).wait(), timeout=15)
        except asyncio.TimeoutError:
            # Keep-alive comment: proxies drop idle connections, and a long
            # job legitimately goes quiet while it waits on the rate limiter.
            yield ": keep-alive\n\n"
            yield _sse("heartbeat", {"done": job["done"], "total": job["total"], **_progress(job)})
            if time.monotonic() - idle_since > settings.job_stall_seconds:
                yield _sse("stalled", {"done": job["done"], "total": job["total"]})
                idle_since = time.monotonic()


def _progress(job) -> dict:
    """What the run is actually spending its time on, for the progress bar."""
    from . import service

    provider = service.get_provider()
    rate = provider.limiter.snapshot() if hasattr(provider, "limiter") else {}
    searches = job["searches"] if "searches" in job.keys() else 0
    bulk = job["bulk_calls"] if "bulk_calls" in job.keys() else 0
    cached = job["cache_hits"] if "cache_hits" in job.keys() else 0
    return {
        "searches": searches,
        "bulk_calls": bulk,
        "cache_hits": cached,
        "requests": searches + bulk,
        "rate_per_minute": rate.get("rate_per_minute"),
        "started_at": job["created_at"],
    }


def _cheapest_first(books, formats) -> list:
    """Indices of books, already-resolved ones first."""
    from .service import to_book

    fmt = formats[0] if formats else "audiobook-overdrive"
    keys = [to_book(b).key for b in books]
    known = store.title_map_get_many(keys, fmt, settings.ttl_title_map)
    hit, miss = [], []
    for index, key in enumerate(keys):
        (hit if key in known else miss).append(index)
    return hit + miss


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def resume_orphans() -> int:
    """Jobs left running when the server stopped are marked, not silently lost."""
    orphans = store.job_orphans()
    for job_id in orphans:
        store.job_finish(job_id, "failed", "server restarted mid-run")
    if orphans:
        log.info("marked %d interrupted job(s) from the last run", len(orphans))
    return len(orphans)
