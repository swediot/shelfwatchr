"""Shelfwatchr — Libby availability for a reading list.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import accounts
from . import changes as changes_mod
from . import gate, jobs, notify, service, store
from .config import settings
from .csvimport import parse_reading_list
from .models import LookupRequest, ProfileIn, Scope, WatchIn

log = logging.getLogger("shelfwatchr")
WEB = Path(__file__).resolve().parent.parent / "web"

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    migrated = store.init_db()
    if migrated:
        log.info("database upgraded: added %s", ", ".join(migrated))
    log.info(
        "shelfwatchr up — db=%s mock=%s rate=%s/min concurrency=%s",
        settings.db_path, settings.mock, settings.requests_per_minute, settings.max_concurrency,
    )
    await jobs.resume_orphans()
    store.job_prune(settings.job_retention_hours * 3600)
    task = asyncio.create_task(nightly_refresh()) if settings.refresh_enabled else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        provider = service.get_provider()
        if hasattr(provider, "aclose"):
            await provider.aclose()


app = FastAPI(
    title="Shelfwatchr", version="1.0", docs_url="/api/docs", redoc_url=None, lifespan=lifespan
)


@app.middleware("http")
async def block_cross_origin_writes(request: Request, call_next):
    """Refuse state-changing requests that came from another site.

    A page on another origin can POST to this server if it can reach it, and a
    simple request skips the CORS preflight entirely. The frontend is
    same-origin, so an Origin header that doesn't match this host didn't come
    from it.

    Requests with no Origin at all are still allowed, for curl and scripts.
    That isn't a hole for signed-in users: the session cookie is SameSite=Lax,
    so a cross-site POST never carries it in the first place — such a request
    arrives as an anonymous one and can only touch anonymous lists, where the
    slug was always the only credential.

    Only the host is compared: behind a TLS-terminating proxy the app sees
    itself as http:// while the browser's Origin says https://, and that is
    the same site.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin:
            allowed = {request.url.netloc.lower()}
            extra = urlparse(settings.public_url).netloc.lower()
            if extra:
                allowed.add(extra)
            if urlparse(origin).netloc.lower() not in allowed:
                return JSONResponse(
                    {"detail": "Cross-origin writes are not accepted."}, status_code=403
                )
    return await call_next(request)


app.middleware("http")(gate.middleware)

app.include_router(gate.router)
app.include_router(accounts.router)


def _guard(slug: str, request: Request) -> None:
    """A list that belongs to an account may only be changed by that account.

    Anonymous lists keep the old rule — the slug is the credential — because
    that's the whole design of a share link. The moment one has an owner, the
    link stops being enough: knowing it is exactly the situation this protects
    against.
    """
    owner = store.profile_owner(slug)
    if owner is None:
        return
    user = accounts.current_user(request)
    if not user or user["id"] != owner:
        raise HTTPException(403, "That list belongs to an account. Sign in to change it.")


# ------------------------------------------------------------- libraries


@app.get("/api/libraries")
async def libraries(q: str = Query(..., min_length=2), refresh: bool = False):
    """Typeahead for the library picker.

    Answers instantly from libraries we've seen before, and asks OverDrive only
    when we have nothing good — the directory barely changes.
    """
    known = store.search_known_libraries(q)
    if known and not refresh:
        return {"items": [s.model_dump() for s in known], "source": "cache"}

    provider = service.get_provider()
    try:
        found = await provider.search_scopes(q)
    except Exception as exc:  # noqa: BLE001
        if known:
            return {"items": [s.model_dump() for s in known], "source": "cache",
                    "warning": f"live search failed: {exc}"}
        raise HTTPException(502, f"library search failed: {exc}")

    if found:
        store.remember_libraries(found)
    return {"items": [s.model_dump() for s in found], "source": "live"}


# ---------------------------------------------------------------- lookup


def _scopes_from_keys(keys: list[str]) -> list[Scope]:
    """Turn keys back into named scopes, preferring names we already know."""
    known = {}
    for key in keys:
        hits = store.search_known_libraries(key, limit=5)
        for h in hits:
            known[h.key] = h
    return [known.get(k, Scope(key=k, name=k)) for k in keys]


@app.post("/api/lookup")
async def lookup(req: LookupRequest):
    if not req.scopes:
        raise HTTPException(400, "Pick at least one library.")
    if not req.books:
        raise HTTPException(400, "No books to look up.")
    if len(req.books) > settings.max_books_per_request:
        raise HTTPException(
            413, f"That's {len(req.books)} books; the limit is {settings.max_books_per_request}."
        )

    scopes = _scopes_from_keys(req.scopes)
    results = await service.lookup_all(
        req.books, scopes, req.formats, req.match_threshold, req.refresh
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "short_wait_days": settings.short_wait_days,
        "results": [r.model_dump() for r in results],
    }


@app.post("/api/jobs")
async def job_start(req: LookupRequest):
    """Kick off a lookup. Returns immediately with an id to follow.

    Everything goes through here, one book or twelve hundred — the difference is
    only how long it takes, and a job that outlives the page is what makes the
    long case work at all.
    """
    if not req.scopes:
        raise HTTPException(400, "Pick at least one library.")
    if not req.books:
        raise HTTPException(400, "No books to look up.")
    if len(req.books) > settings.max_books_per_request:
        raise HTTPException(
            413, f"That's {len(req.books)} books; this server's limit is "
                 f"{settings.max_books_per_request}. Raise SHELFWATCH_MAX_BOOKS if you need more."
        )

    scopes = _scopes_from_keys(req.scopes)
    job_id = await jobs.start(
        req.books, scopes, req.formats, req.match_threshold, req.refresh, slug=req.slug or "",
    )
    estimate, cached = _estimate_seconds(req.books, scopes, req.formats, req.refresh)
    return {
        "job_id": job_id,
        "total": len(req.books),
        "estimated_seconds": estimate,
        "already_cached": cached,
    }


def _estimate_seconds(books, scopes, formats, refresh: bool) -> tuple[int, int]:
    """Honest up-front number, so a long run says so before it starts.

    Estimates *requests*, not lookups, which are no longer the same thing: one
    multi-library search covers every library, and one bulk call covers two
    dozen books. Counting lookups would over-quote by an order of magnitude.
    """
    from .service import to_book

    keys = [to_book(b).key for b in books]
    fmts = list(formats) or ["audiobook-overdrive"]
    n_scopes, n_books = max(len(scopes), 1), len(books)
    provider = service.get_provider()
    batching = settings.use_batch and getattr(provider, "batch_ok", False)

    # Per format, because that's how the work actually runs: each format has its
    # own title-id map, its own searches and its own bulk calls. Estimating from
    # formats[0] alone quoted half the time for a two-format run.
    cached_total, requests, outstanding_total = 0, 0, 0
    for fmt in fmts:
        cached = 0
        if not refresh:
            cached = store.cache_count_fresh(
                provider.name, keys, [s.key for s in scopes], [fmt],
            )
        cached_total += cached
        outstanding = max(n_books * n_scopes - cached, 0)
        outstanding_total += outstanding
        if not outstanding:
            continue
        if not batching:
            requests += outstanding
            continue
        books_left = max(1, round(outstanding / n_scopes))
        # Title ids are counted even on a refresh: a refresh re-fetches availability,
        # but a book's OverDrive id doesn't go stale, so the search step is still skipped.
        known_ids = len(store.title_map_get_many(keys, fmt, settings.ttl_title_map))
        searches = max(0, books_left - known_ids)
        chunks = max(1, -(-books_left // max(1, settings.bulk_availability_size)))
        requests += searches + chunks * n_scopes

    if not outstanding_total:
        return 0, cached_total
    return int(requests / max(settings.requests_per_minute, 1) * 60), cached_total


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, after: int = 0, limit: int = 500):
    job = store.job_get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    rows = store.job_results(job_id, after=after, limit=limit)
    books = []
    for row in rows:
        book = json.loads(row["payload"])
        book["idx"] = row["idx"]
        books.append(book)
    return {
        "job_id": job_id,
        "state": job["state"],
        "done": job["done"],
        "total": job["total"],
        "error": job["error"],
        # When it ended, so a page that rejoins a finished run can say when it
        # was checked instead of guessing.
        "finished_at": job["finished_at"],
        **jobs._progress(job),
        "books": books,
        # A cursor into the production order, not a book index — see store.job_results.
        "next": rows[-1]["seq"] if rows else after,
    }


@app.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str, after: int = 0):
    if not store.job_get(job_id):
        raise HTTPException(404, "No such job.")
    return StreamingResponse(
        jobs.stream(job_id, from_index=after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.post("/api/jobs/{job_id}/cancel")
async def job_cancel(job_id: str):
    if not store.job_get(job_id):
        raise HTTPException(404, "No such job.")
    jobs.cancel(job_id)
    return {"ok": True}


# ------------------------------------------------------------------- csv


@app.post("/api/import")
@app.post("/api/import/storygraph")  # older name, still works
async def import_storygraph(
    file: UploadFile = File(...),
    statuses: str = Query("to-read", description="comma-separated read statuses to keep"),
    exclude_owned: bool = Query(False, description="drop books the export marks as owned"),
):
    raw = await file.read()
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(413, "That CSV is over 8 MB — is it definitely a StoryGraph export?")
    try:
        books, report = parse_reading_list(
            raw, [s for s in statuses.split(",") if s.strip()], exclude_owned=exclude_owned,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read that CSV: {exc}")
    return {"books": [b.model_dump() for b in books], "report": report}


# -------------------------------------------------------------- profiles


@app.post("/api/profile")
async def profile_create(body: ProfileIn, slug: str | None = None):
    """Save libraries + list under a slug. The slug is the share link."""
    saved = store.profile_save(
        slug, name=body.name, scopes=body.scopes, formats=body.formats, books=body.books
    )
    return {"slug": saved}


@app.get("/api/profile/{slug}")
async def profile_read(slug: str):
    prof = store.profile_get(slug)
    if not prof:
        raise HTTPException(404, "No saved list with that link.")
    state = store.profile_state_get(slug)
    return {
        "slug": prof["slug"],
        "name": prof["name"],
        "scopes": [s.model_dump() for s in prof["scopes"]],
        "formats": prof["formats"],
        "books": [b.model_dump() for b in prof["books"]],
        "updated_at": prof["updated_at"],
        "known_states": len(state),
        "watch_enabled": prof["watch_enabled"],
        "watch_frequency": prof["watch_frequency"],
        "notify_type": prof["notify_type"],
        "notify_target": prof["notify_target"],
        "last_run_at": prof["last_run_at"],
    }


@app.post("/api/profile/{slug}/books")
async def profile_update_books(slug: str, body: ProfileIn, request: Request):
    """Replace a saved list's books from a fresh export.

    Books that fell off the list have their remembered state dropped, so if one
    comes back later it isn't compared against a stale reading from months ago.
    """
    _guard(slug, request)
    prof = store.profile_get(slug)
    if not prof:
        raise HTTPException(404, "No saved list with that link.")

    before = {b.title for b in prof["books"]}
    after = {b.title for b in body.books}
    store.profile_save(
        slug,
        name=body.name or prof["name"],
        scopes=body.scopes or prof["scopes"],
        formats=body.formats or prof["formats"],
        books=body.books,
    )
    from .service import to_book

    dropped = store.profile_prune_state(slug, {to_book(b).key for b in body.books})
    return {
        "ok": True,
        "total": len(body.books),
        "added": sorted(after - before),
        "removed": sorted(before - after),
        "state_rows_dropped": dropped,
    }


@app.get("/api/profile/{slug}/report")
async def profile_report(slug: str):
    """The stored report — what the overnight run last found. Instant."""
    if not store.profile_get(slug):
        raise HTTPException(404, "No saved list with that link.")
    report = store.report_get(slug)
    if not report:
        return {"slug": slug, "report": None, "changes": []}
    return {
        "slug": slug,
        "generated_at": report["generated_at"],
        "report": report["payload"],
        "changes": report["changes"],
    }


@app.post("/api/profile/{slug}/watch")
async def profile_watch(slug: str, body: WatchIn, request: Request):
    """Turn the daily/weekly check on or off and say where alerts should go."""
    _guard(slug, request)
    prof = store.profile_get(slug)
    if not prof:
        raise HTTPException(404, "No saved list with that link.")
    if body.notify_type not in ("none", "ntfy", "webhook", "email"):
        raise HTTPException(400, f"Unknown notification type: {body.notify_type}")
    if body.notify_type == "email" and not notify.channel_status()["email"]:
        raise HTTPException(400, "This server has no SMTP configured, so it can't send email.")
    if body.notify_type != "none" and not body.notify_target.strip():
        raise HTTPException(400, "Tell me where to send it.")

    store.profile_save(
        slug, name=prof["name"], scopes=prof["scopes"], formats=prof["formats"],
        books=prof["books"], watch_enabled=body.enabled, watch_frequency=body.frequency,
        notify_type=body.notify_type, notify_target=body.notify_target.strip(),
    )
    return {"ok": True, "enabled": body.enabled, "frequency": body.frequency}


@app.post("/api/profile/{slug}/run")
async def profile_run_now(slug: str, request: Request, notify_changes: bool = False):
    """Run a saved list right now — the 'test it' button."""
    _guard(slug, request)
    if not store.profile_get(slug):
        raise HTTPException(404, "No saved list with that link.")
    return await run_profile(slug, notify_on_changes=notify_changes, base_url=settings.public_url)


@app.post("/api/profile/{slug}/test-notify")
async def profile_test_notify(slug: str, request: Request):
    """Send one fake alert, so you find out now if the topic name is wrong."""
    _guard(slug, request)
    prof = store.profile_get(slug)
    if not prof:
        raise HTTPException(404, "No saved list with that link.")
    if prof["notify_type"] == "none":
        raise HTTPException(400, "No notification channel set for this list.")
    result = await notify.send(
        prof["notify_type"], prof["notify_target"],
        f"{prof['name'] or 'Shelfwatch'}: test alert",
        "If you're reading this, alerts work. Real ones list what changed.",
    )
    return result


@app.get("/api/notify/channels")
async def notify_channels():
    return notify.channel_status()


# ----------------------------------------------------------------- admin


@app.get("/api/health")
async def health():
    provider = service.get_provider()
    return {
        "ok": True,
        "mock": settings.mock,
        "cache": store.cache_stats(),
        "title_ids": store.title_map_stats(),
        "rate": (provider.limiter.snapshot() if hasattr(provider, "limiter")
                 else {"rate_per_minute": settings.requests_per_minute}),
        "concurrency": settings.max_concurrency,
        "batch": {
            "enabled": settings.use_batch and getattr(provider, "batch_ok", False),
            "note": getattr(provider, "batch_note", ""),
            "bulk_size": settings.bulk_availability_size,
        },
    }


@app.post("/api/cache/clear")
async def cache_clear(title_ids: bool = False):
    """Drop cached availability. `title_ids=true` also forgets learned
    OverDrive ids, which forces the next run to search again — slow, and only
    wanted if matching has gone wrong."""
    out = {"cleared": store.cache_clear()}
    if title_ids:
        out["title_ids_cleared"] = store.title_map_clear()
    return out


# ------------------------------------------------------- nightly refresh


async def run_profile(slug: str, *, notify_on_changes: bool = True, base_url: str = "") -> dict:
    """Re-check a saved list, work out what moved, store the report, tell the user."""
    prof = store.profile_get(slug)
    if not prof or not prof["books"] or not prof["scopes"]:
        return {"slug": slug, "skipped": "nothing saved to check"}

    previous = store.profile_state_get(slug)
    first_run = not previous

    results = await service.lookup_all(
        prof["books"], prof["scopes"], prof["formats"], settings.match_threshold, refresh=True
    )

    found: list[changes_mod.Change] = []
    for r in results:
        for av in r.results:
            change = changes_mod.compare(
                previous.get((r.key, av.scope_key, av.fmt or "audiobook-overdrive")),
                r.title, r.author, av)
            if change:
                found.append(change)
            if av.status not in ("unknown", "error"):
                store.profile_state_put(slug, r.key, av)

    found = changes_mod.sort_changes(found)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "short_wait_days": settings.short_wait_days,
        "results": [r.model_dump() for r in results],
    }
    store.report_put(slug, payload, [c.dict() for c in found])
    store.profile_mark_run(slug)

    sent = None
    if notify_on_changes and found and not first_run and prof["notify_type"] != "none":
        link = f"{base_url}/?p={slug}" if base_url else ""
        list_name = prof["name"] or "Shelfwatchr"
        if prof["notify_type"] == "email":
            # Email gets the digest treatment: it's read at leisure, so it can
            # carry more than a push notification's one line.
            available = [
                (r.title, ", ".join(a.scope_name for a in r.results if a.status == "available"))
                for r in results if r.best_rank == 0
            ]
            subject, text, html = notify.build_email(
                list_name, found,
                period="week" if prof["watch_frequency"] == "weekly" else "day",
                available_now=available, link=link,
            )
            sent = await notify.send("email", prof["notify_target"], subject, text, html)
        else:
            title, body = notify.build_message(list_name, found, link)
            sent = await notify.send(prof["notify_type"], prof["notify_target"], title, body)

    log.info("ran %s: %d books, %d changes%s", slug, len(results), len(found),
             " (first run, no alerts)" if first_run else "")
    return {
        "slug": slug,
        "books": len(results),
        "first_run": first_run,
        "changes": [c.dict() for c in found],
        "notified": sent,
    }


def seconds_until_refresh(now: datetime) -> float:
    target = now.replace(hour=settings.refresh_hour_utc, minute=15, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def nightly_refresh():
    """Once a night, run every watched list that's due."""
    while True:
        await asyncio.sleep(max(60.0, seconds_until_refresh(datetime.now(timezone.utc))))
        try:
            due = store.profiles_due()
            log.info("nightly refresh: %d list(s) due", len(due))
            for slug in due:
                try:
                    await run_profile(slug, base_url=settings.public_url)
                except Exception:  # noqa: BLE001 — one bad list mustn't stop the rest
                    log.exception("refresh failed for %s", slug)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("nightly refresh failed")


# ------------------------------------------------------------------ web


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/signin")
@app.get("/reset")
async def auth_page():
    """One page for sign in, sign up and choosing a new password.

    Served at both paths so the link in a reset email lands somewhere that
    already knows what to do with the ?token= it carries.
    """
    return FileResponse(WEB / "signin.html")


if WEB.exists():
    app.mount("/static", StaticFiles(directory=WEB), name="static")
