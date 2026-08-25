"""End-to-end tests against the mock catalogue. No network, no fixtures server.

Run:  python -m pytest tests/ -q      (or: python tests/test_app.py)
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before app.config is imported.
TMP = tempfile.mkdtemp(prefix="shelfwatch-test-")
os.environ.update(
    SHELFWATCH_MOCK="1",
    SHELFWATCH_DB=str(Path(TMP) / "test.db"),
    SHELFWATCH_REFRESH="0",
    SHELFWATCH_RPM="6000",
)

from fastapi.testclient import TestClient  # noqa: E402

from app import changes as changes_mod  # noqa: E402
from app import store  # noqa: E402
from app.csvimport import parse_reading_list  # noqa: E402
from app.main import app  # noqa: E402
from app.matching import Book, score_candidate  # noqa: E402
from app.models import Availability  # noqa: E402
from app.providers import mock as mockprovider  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

# Entered as a context manager on purpose: TestClient otherwise spins up a fresh
# event loop per request, which would cancel the background job tasks the moment
# the request that created them returned. This keeps one loop for the whole run,
# like a real server has.
client = TestClient(app)
client.__enter__()


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok — {message}")


# ---------------------------------------------------------------- parsing


def test_csv_parsing():
    print("\nCSV parsing")
    for name, expected_source in (("storygraph.csv", "storygraph"), ("goodreads.csv", "goodreads")):
        raw = (FIXTURES / name).read_bytes()
        books, report = parse_reading_list(raw)
        titles = [b.title for b in books]
        check(report["source"] == expected_source, f"{name} detected as {expected_source}")
        check(not any("Dune" == t for t in titles), f"{name}: read books excluded")
        check(any("Fifth Season" in t for t in titles), f"{name}: to-read books kept")

    raw = (FIXTURES / "goodreads.csv").read_bytes()
    books, _ = parse_reading_list(raw)
    by_title = {b.title: b for b in books}
    schwarm = next(b for t, b in by_title.items() if "Schwarm" in t)
    check(schwarm.author == "Frank Schätzing", "accented author names survive")
    fifth = next(b for t, b in by_title.items() if "Fifth Season" in t)
    check(fifth.isbn == "9780316229296", f'Goodreads ="..." ISBN guard stripped (got {fifth.isbn!r})')

    # Owned books: counted but kept by default, dropped on request. StoryGraph
    # says Yes/No, Goodreads gives a copy count; both fixtures mark one book.
    for name in ("storygraph.csv", "goodreads.csv"):
        raw = (FIXTURES / name).read_bytes()
        kept, report = parse_reading_list(raw)
        check(report["has_owned"], f"{name}: owned column found")
        dropped, report2 = parse_reading_list(raw, exclude_owned=True)
        check(report2["skipped_owned"] == report["owned"] == 1,
              f"{name}: exclude_owned drops exactly the owned rows ({report2['skipped_owned']})")
        check(len(kept) - len(dropped) == report2["skipped_owned"],
              f"{name}: book count shrinks by the owned count")

    # A file that isn't a reading list at all
    try:
        parse_reading_list(b"foo,bar\n1,2\n")
        raise AssertionError("should have rejected a CSV with no Title column")
    except ValueError as exc:
        check("No Title column" in str(exc), "unrecognised CSV is rejected with a useful message")


# --------------------------------------------------------------- matching


def test_matching():
    print("\nMatching")
    babel = Book(title="Babel: Or the Necessity of Violence", authors=("R. F. Kuang",))
    wrong = score_candidate(babel, "Babel-17", "", ["Samuel R. Delany"])
    right = score_candidate(babel, "Babel", "Or the Necessity of Violence", ["R. F. Kuang"])
    check(wrong < 0.78 <= right, f"Babel-17/Delany rejected ({wrong}), Babel/Kuang accepted ({right})")

    fifth = Book(title="The Fifth Season", authors=("N. K. Jemisin",))
    sub = score_candidate(fifth, "The Fifth Season", "The Broken Earth, Book 1", ["N. K. Jemisin"])
    check(sub >= 0.78, f"subtitled edition still matches ({sub})")

    # Same author, genuinely different book, must not match
    other = score_candidate(fifth, "The Obelisk Gate", "", ["N. K. Jemisin"])
    check(other < 0.78, f"different book by the same author rejected ({other})")

    # "Book 3" is stripped as series noise, which means titles that are *only*
    # that collapse to an empty key. Worth knowing about: it bit a test.
    from app.matching import norm_title
    check(norm_title("Book 3") == "", "bare 'Book 3' normalises away entirely")
    check(norm_title("The Fifth Season: Book 1") == "fifth season",
          "a series number in a real title is stripped, the title survives")


# ------------------------------------------------------------------- api


def test_libraries():
    print("\nLibrary picker")
    r = client.get("/api/libraries", params={"q": "queens"})
    check(r.status_code == 200, "library search responds")
    check(any(i["key"] == "queenslibrary" for i in r.json()["items"]), "finds Queens")

    r2 = client.get("/api/libraries", params={"q": "queens"})
    check(r2.json()["source"] == "cache", "second search is served from the local directory")


def test_import_and_lookup():
    print("\nImport and lookup")
    raw = (FIXTURES / "goodreads.csv").read_bytes()
    r = client.post("/api/import", files={"file": ("goodreads.csv", raw, "text/csv")})
    check(r.status_code == 200, "CSV upload accepted")
    books = r.json()["books"]
    check(len(books) == 4, f"4 to-read books imported (got {len(books)})")

    r = client.post("/api/lookup", json={
        "books": books,
        "scopes": ["westmount", "queenslibrary", "aubora"],
    })
    check(r.status_code == 200, "lookup responds")
    results = {b["title"]: b for b in r.json()["results"]}

    piranesi = {a["scope_key"]: a for a in results["Piranesi"]["results"]}
    check(piranesi["westmount"]["status"] == "available", "Piranesi available at Westmount")
    check(piranesi["queenslibrary"]["status"] == "holdable", "Piranesi holdable at Queens")
    check(piranesi["queenslibrary"]["wait_days"] == 11, "reported wait used verbatim")

    fifth = {a["scope_key"]: a for a in results["The Fifth Season"]["results"]}
    check(fifth["westmount"]["wait_estimated"] is True, "missing estimate is derived and flagged")

    babel = {a["scope_key"]: a for a in results["Babel, or the Necessity of Violence"]["results"]}
    check(babel["aubora"]["status"] == "not_owned", "Babel-17 does not match Babel across libraries")
    check(babel["queenslibrary"]["status"] == "available", "Babel available at Queens")

    schwarm = {a["scope_key"]: a for a in results["Der Schwarm"]["results"]}
    check(schwarm["aubora"]["status"] == "holdable", "German title matched at the Zürich library")

    # Second identical call must be served from cache
    r2 = client.post("/api/lookup", json={"books": books, "scopes": ["westmount"]})
    cached = [a["from_cache"] for b in r2.json()["results"] for a in b["results"]]
    check(all(cached), "repeat lookups come from cache")


def _drain_job(job_id, max_frames=20000):
    """Follow a job's SSE stream to completion and return the books."""
    books, state_seen, frames, event = [], None, 0, ""
    with client.stream("GET", f"/api/jobs/{job_id}/stream") as resp:
        for line in resp.iter_lines():
            frames += 1
            if frames > max_frames:
                break
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[5:])
            if event == "results":
                books.extend(payload["books"])
            elif event == "done":   # note: the "start" frame also carries a state
                state_seen = payload["state"]
                break
    return books, state_seen


def test_jobs():
    print("\nBackground jobs")
    r = client.post("/api/jobs", json={
        "books": [{"title": "Piranesi", "author": "Susanna Clarke"},
                  {"title": "The Fifth Season", "author": "N. K. Jemisin"}],
        "scopes": ["westmount", "queenslibrary"],
    })
    check(r.status_code == 200, "job accepted")
    job = r.json()
    check(job["total"] == 2, "job knows its size")
    check("estimated_seconds" in job, "job gives an up-front time estimate")

    books, final = _drain_job(job["job_id"])
    check(final == "done", "job reaches done")
    check(len(books) == 2, f"both books came back (got {len(books)})")

    # Results survive the stream — this is what makes closing the tab safe.
    status = client.get(f"/api/jobs/{job['job_id']}").json()
    check(status["state"] == "done" and len(status["books"]) == 2, "results persist after the stream ends")

    # Resuming from an offset returns only the remainder.
    partial = client.get(f"/api/jobs/{job['job_id']}?after=1").json()
    check(len(partial["books"]) == 1, "a resumed fetch returns only what's missing")

    r = client.get("/api/jobs/nosuchjob")
    check(r.status_code == 404, "unknown job is a 404")


def test_large_list():
    print("\nA list bigger than anyone's attention span")
    many = [{"title": f"Piranesi {i}", "author": "Susanna Clarke"} for i in range(300)]
    r = client.post("/api/jobs", json={"books": many, "scopes": ["westmount"]})
    check(r.status_code == 200, "300 books accepted without complaint")
    job = r.json()
    check(job["estimated_seconds"] > 0, f"estimate given: {job['estimated_seconds']}s")

    books, final = _drain_job(job["job_id"])
    check(final == "done", "the whole run completes")
    check(len(books) == 300, f"all 300 results stored (got {len(books)})")

    # Cancellation has to work, or a mistaken 5,000-book run is unstoppable.
    # Cancellation is checked between chunks, so give each lookup enough time
    # for the cancel to actually land mid-run rather than after the finish.
    mockprovider.DELAY = 0.01
    try:
        r2 = client.post("/api/jobs", json={
            # Not "Book 1", "Book 2": the matcher strips "book <n>" as series
            # noise, so those all normalise to the same key and dedupe to one.
            "books": [{"title": f"Quiet Harbour {i}"} for i in range(400)],
            "scopes": ["westmount"]})
        jid = r2.json()["job_id"]
        time.sleep(0.2)
        client.post(f"/api/jobs/{jid}/cancel")
        _drain_job(jid)
        final_state = client.get(f"/api/jobs/{jid}").json()
        check(final_state["state"] == "cancelled", "a job can be cancelled")
        check(final_state["done"] < 400, f"cancelling actually stopped the work at {final_state['done']}")
    finally:
        mockprovider.DELAY = 0

    over = client.post("/api/jobs", json={
        "books": [{"title": "x"}] * 6000, "scopes": ["westmount"]})
    check(over.status_code == 413, "an absurd list is refused with a clear limit")


# --------------------------------------------------------------- changes


def test_change_detection():
    print("\nChange detection")

    def av(status, wait=None, scope="queenslibrary"):
        return Availability(scope_key=scope, scope_name="Queens", status=status, wait_days=wait)

    cases = [
        ({"status": "holdable", "wait_days": 40}, av("available", 0), "now_available"),
        ({"status": "not_owned", "wait_days": None}, av("holdable", 30), "newly_holdable"),
        ({"status": "holdable", "wait_days": 60}, av("holdable", 20), "wait_dropped"),
        ({"status": "holdable", "wait_days": 20}, av("holdable", 60), "wait_grew"),
    ]
    for before, after, expected in cases:
        got = changes_mod.compare(before, "A Book", "An Author", after)
        check(got is not None and got.kind == expected, f"{before['status']} -> {after.status} = {expected}")

    # Books leaving the shelf are deliberately not reported.
    gone = changes_mod.compare({"status": "available", "wait_days": 0}, "A", "X", av("holdable", 14))
    check(gone is None, "a book being borrowed by someone else is not reported")
    dropped = changes_mod.compare({"status": "holdable", "wait_days": 30}, "A", "X", av("not_owned"))
    check(dropped is None, "a book leaving the catalogue is not reported")

    # Noise must not fire
    quiet = changes_mod.compare({"status": "holdable", "wait_days": 60}, "A Book", "X", av("holdable", 57))
    check(quiet is None, "a 3-day wobble on a 60-day wait is not reported")
    grew_a_little = changes_mod.compare({"status": "holdable", "wait_days": 60}, "A", "X", av("holdable", 68))
    check(grew_a_little is None, "an 8-day increase on a 60-day wait is not reported")
    first = changes_mod.compare(None, "A Book", "X", av("available", 0))
    check(first is None, "a first sighting is not a change")
    failed = changes_mod.compare({"status": "available", "wait_days": 0}, "A", "X", av("unknown"))
    check(failed is None, "a failed lookup is never reported as a change")

    sentence = changes_mod.compare(
        {"status": "holdable", "wait_days": 90}, "Piranesi", "Susanna Clarke", av("holdable", 20)
    ).sentence()
    check("down from" in sentence and "Piranesi" in sentence, f"readable sentence: {sentence!r}")


# -------------------------------------------------------------- profiles


def test_profile_watch_and_run():
    print("\nProfiles, watching, notifications")
    raw = (FIXTURES / "storygraph.csv").read_bytes()
    books = client.post("/api/import", files={"file": ("sg.csv", raw, "text/csv")}).json()["books"]

    r = client.post("/api/profile", json={
        "name": "Test list",
        "scopes": [{"key": "westmount", "name": "Westmount Public Library"},
                   {"key": "queenslibrary", "name": "Queens Public Library"}],
        "books": books,
    })
    slug = r.json()["slug"]
    check(len(slug) == 8, f"profile saved as {slug}")

    first = client.post(f"/api/profile/{slug}/run").json()
    check(first["first_run"] is True, "first run is marked as such")
    check(first["changes"] == [], "first run reports no changes (nothing to compare)")

    stored = client.get(f"/api/profile/{slug}/report").json()
    check(stored["report"] is not None, "report is stored for instant loading")

    # Now move the catalogue and run again.
    mockprovider.CATALOGUE["queenslibrary"][0]["available"] = 3  # Fifth Season frees up
    mockprovider.CATALOGUE["westmount"][0]["available"] = 0      # Piranesi runs out
    mockprovider.CATALOGUE["westmount"][0]["owned"] = 2
    mockprovider.CATALOGUE["westmount"][0]["holds"] = 8
    second = client.post(f"/api/profile/{slug}/run").json()
    kinds = {c["kind"] for c in second["changes"]}
    check("now_available" in kinds, "picks up a book becoming available")
    check("no_longer_available" not in kinds, "does not report the book that went off the shelf")
    check(second["first_run"] is False, "second run is not a first run")

    # Watch settings
    r = client.post(f"/api/profile/{slug}/watch", json={
        "enabled": True, "frequency": "weekly", "notify_type": "ntfy", "notify_target": "test-topic",
    })
    check(r.status_code == 200, "watch settings saved")

    prof = store.profile_get(slug)
    check(prof["watch_enabled"] and prof["watch_frequency"] == "weekly", "settings persisted")

    check(store.profiles_due(now=prof["last_run_at"] + 60) == [], "weekly list not due an hour later")
    due = store.profiles_due(now=prof["last_run_at"] + 8 * 86400)
    check(slug in due, "weekly list is due after 8 days")

    r = client.post(f"/api/profile/{slug}/watch", json={
        "enabled": True, "frequency": "daily", "notify_type": "email", "notify_target": "a@b.com",
    })
    check(r.status_code == 400, "email is refused when the server has no SMTP configured")

    r = client.post(f"/api/profile/{slug}/watch", json={
        "enabled": True, "frequency": "daily", "notify_type": "ntfy", "notify_target": "  ",
    })
    check(r.status_code == 400, "a blank notification target is refused")

    # Saving the book list again must not silently disable the user's alerts.
    client.post(f"/api/profile?slug={slug}", json={
        "name": "Test list", "scopes": [{"key": "westmount", "name": "W"}], "books": books,
    })
    check(store.profile_get(slug)["watch_enabled"] is True, "re-saving a list keeps watching on")


def test_schema_migration():
    print("\nUpgrading an existing database")
    import sqlite3 as sq
    from app import store as st

    path = Path(TMP) / "old.db"
    old = sq.connect(path)
    # A database from before watch settings and job stats existed.
    old.executescript("""
        CREATE TABLE profile (slug TEXT PRIMARY KEY, name TEXT DEFAULT '',
          scopes TEXT NOT NULL DEFAULT '[]', formats TEXT NOT NULL DEFAULT '[]',
          books TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE job (id TEXT PRIMARY KEY, total INTEGER NOT NULL DEFAULT 0,
          done INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'running',
          created_at REAL NOT NULL);
        INSERT INTO profile (slug, name, created_at, updated_at) VALUES ('oldlist', 'Before', 1, 1);
    """)
    old.commit()
    old.close()

    previous = st.settings.db_path
    try:
        object.__setattr__(st.settings, "db_path", str(path))
        st.reset_connection()
        applied = st.init_db()
        check(any("profile.watch_enabled" in a for a in applied),
              f"missing columns are added on startup: {applied[:3]}")
        prof = st.profile_get("oldlist")
        check(prof is not None, "and the existing row survives")
        check(prof["watch_enabled"] is False, "with a sensible default for the new column")
        check(st.init_db() == [], "running again changes nothing")
    finally:
        object.__setattr__(st.settings, "db_path", previous)
        st.reset_connection()


def test_cross_origin_writes():
    print("\nCross-origin writes")
    body = {"books": [{"title": "Piranesi"}], "scopes": ["westmount"]}
    blocked = client.post("/api/jobs", json=body, headers={"Origin": "https://evil.example"})
    check(blocked.status_code == 403, "a write from another origin is refused")
    same = client.post("/api/jobs", json=body, headers={"Origin": "http://testserver"})
    check(same.status_code == 200, "the app's own origin is fine")
    plain = client.post("/api/jobs", json=body)
    check(plain.status_code == 200, "and a request with no Origin at all still works (curl, scripts)")
    reads = client.get("/api/health", headers={"Origin": "https://evil.example"})
    check(reads.status_code == 200, "reads are unaffected")


def test_bad_input():
    print("\nBad input")
    r = client.post("/api/lookup", json={"books": [{"title": "X"}], "scopes": []})
    check(r.status_code == 400, "lookup with no library is refused")
    r = client.post("/api/import", files={"file": ("x.csv", b"nope\n1\n", "text/csv")})
    check(r.status_code == 400, "an unparseable CSV gives a 400, not a 500")
    r = client.get("/api/profile/nosuchslug")
    check(r.status_code == 404, "unknown profile is a 404")
    r = client.get("/")
    check(r.status_code == 200 and b"Shelfwatch" in r.content, "the page itself is served")


def test_progress_reporting():
    print("\nProgress reporting")
    # Genuinely cold: earlier tests have taught the server these ids, and since
    # every lookup path now shares the batch cache, "refresh" alone no longer
    # means "search again".
    store.cache_clear()
    store.title_map_clear()
    r = client.post("/api/jobs", json={
        "books": [{"title": "Piranesi", "author": "Susanna Clarke"},
                  {"title": "Der Schwarm", "author": "Frank Schätzing"}],
        "scopes": ["westmount", "aubora"], "refresh": True,
    })
    jid = r.json()["job_id"]
    _drain_job(jid)
    status = client.get(f"/api/jobs/{jid}").json()

    for field in ("done", "total", "searches", "bulk_calls", "cache_hits", "requests", "started_at"):
        check(field in status, f"progress payload carries {field}")
    check(status["done"] == status["total"] == 2, "counts add up")
    check(status["requests"] == status["searches"] + status["bulk_calls"],
          "requests is the sum of the work actually done")
    check(status["searches"] > 0, f"a cold run reports its searches ({status['searches']})")

    # A repeat run should be visibly cheaper in the same numbers.
    r2 = client.post("/api/jobs", json={
        "books": [{"title": "Piranesi", "author": "Susanna Clarke"},
                  {"title": "Der Schwarm", "author": "Frank Schätzing"}],
        "scopes": ["westmount", "aubora"],
    })
    jid2 = r2.json()["job_id"]
    _drain_job(jid2)
    again = client.get(f"/api/jobs/{jid2}").json()
    check(again["cache_hits"] > 0, f"the second run reports cache hits ({again['cache_hits']})")
    check(again["searches"] == 0, "and no searches at all")

    # The stream must carry the same numbers, not just the REST endpoint.
    seen = set()
    with client.stream("GET", f"/api/jobs/{jid}/stream") as resp:
        for line in resp.iter_lines():
            if line.startswith("data:"):
                seen |= set(json.loads(line[5:]).keys())
            if line.strip() == "event: done":
                break
    check("searches" in seen and "requests" in seen,
          "the live stream carries progress detail, not just a count")


def test_out_of_order_streaming():
    print("\nStreaming results that arrive out of order")
    from app.models import BookResult

    # Simulate what cheapest-first ordering does: later indices land first.
    job_id = store.job_create([{}] * 5, [], [])
    for idx in (4, 0, 3, 1, 2):
        store.job_add_result(job_id, idx, BookResult(title=f"Book at {idx}", key=f"k{idx}"))

    rows = store.job_results(job_id, after=0, limit=100)
    check(len(rows) == 5, f"every result is returned regardless of index order (got {len(rows)})")
    check([r["idx"] for r in rows] == [4, 0, 3, 1, 2],
          "results page in the order they were produced")

    # Paging must not skip anything — the bug was a cursor keyed on index.
    seen, cursor = [], 0
    while True:
        page = store.job_results(job_id, after=cursor, limit=2)
        if not page:
            break
        seen += [r["idx"] for r in page]
        cursor = page[-1]["seq"]
    check(sorted(seen) == [0, 1, 2, 3, 4], f"paging visits every result exactly once (got {seen})")

    payload = client.get(f"/api/jobs/{job_id}").json()
    check([b["idx"] for b in payload["books"]] == [4, 0, 3, 1, 2],
          "the API hands back each result's display position")


def test_adaptive_rate():
    print("\nAdaptive rate limiting")
    import asyncio as aio
    from app.providers.base import RateLimiter

    async def scenario():
        lim = RateLimiter(120, floor=30, ceiling=300, probe_after=5)
        # A clean stretch should speed it up, but never past the ceiling.
        for _ in range(200):
            await lim.record_success()
        check(lim.rate == 300, f"climbs to the ceiling and stops ({lim.rate})")
        # One complaint halves it.
        await lim.record_throttle()
        check(lim.rate == 150, f"a 429 halves the rate ({lim.rate})")
        for _ in range(10):
            await lim.record_throttle()
        check(lim.rate == 30, f"repeated 429s bottom out at the floor ({lim.rate})")

        # Pinned mode ignores all of it.
        fixed = RateLimiter(120, adaptive=False)
        for _ in range(100):
            await fixed.record_success()
        check(fixed.rate == 120, "with adaptive off the rate never moves")
        await fixed.record_throttle()
        check(fixed.rate == 120, "…not even downwards, but back_off still applies")

        # And it actually paces: 6 requests at 600/min is ~0.5s.
        paced = RateLimiter(600, adaptive=False)
        t0 = time.monotonic()
        await aio.gather(*[paced.wait() for _ in range(6)])
        elapsed = time.monotonic() - t0
        check(0.35 < elapsed < 0.75, f"6 requests at 600/min took {elapsed:.2f}s")

    aio.run(scenario())


def test_bulk_size_probe():
    print("\nBulk batch size probing")
    import asyncio as aio
    from app.models import BookIn, Scope
    from app import service as svc

    provider = svc.get_provider()
    previous_cap, previous_size = provider.MAX_BULK, provider.bulk_size
    provider.MAX_BULK, provider.bulk_size = 25, 0
    mockprovider.SYNTHETIC = True
    try:
        books = [BookIn(title=f"Probe Title {i}", author=f"Writer {i}") for i in range(96)]
        out = aio.run(svc.lookup_chunk(books, [Scope(key="westmount", name="W")],
                                       ["audiobook-overdrive"], 0.78, True))
        check(len(out) == 96, "every book still gets an answer while probing")
        check(provider.bulk_size <= 25, f"settles on a size the server accepts ({provider.bulk_size})")
        check(provider.bulk_size >= 8, "doesn't shrink past the floor")
        check(all(b.results for b in out), "no book left without a result")
    finally:
        provider.MAX_BULK, provider.bulk_size = previous_cap, previous_size
        mockprovider.SYNTHETIC = False


def test_cheapest_first():
    print("\nOrdering: known books first")
    from app import jobs
    from app.models import BookIn
    from app.service import to_book

    books = [BookIn(title=f"Ordering Case {i}", author="Someone") for i in range(6)]
    # Pretend we already know ids for a couple of them.
    for i in (3, 5):
        store.title_map_put(to_book(books[i]).key, "audiobook-overdrive", f"id-{i}")
    order = jobs._cheapest_first(books, ["audiobook-overdrive"])
    check(order[:2] == [3, 5], f"already-resolved books go first (got {order})")
    check(sorted(order) == list(range(6)), "and nothing is dropped or duplicated")


def test_email_digest():
    print("\nEmail digest")
    from app.changes import Change
    from app.notify import build_email

    changes = [
        Change(kind="now_available", title="Piranesi", author="Susanna Clarke",
               library="Queens Public Library", scope_key="q"),
        Change(kind="wait_dropped", title="The Fifth Season", author="N. K. Jemisin",
               library="Westmount", scope_key="w", wait_before=90, wait_after=20),
        Change(kind="wait_grew", title="Babel", author="R. F. Kuang",
               library="Queens Public Library", scope_key="q", wait_before=10, wait_after=60),
    ]
    subject, text, html = build_email(
        "Giulian's audiobooks", changes, period="week",
        available_now=[("Der Schwarm", "Aubora")], link="http://home:8080/?p=abc",
    )
    check("1 available now" in subject, f"subject summarises: {subject!r}")
    check("This week" in html, "weekly digest says so")
    check("Available now" in html and "Shorter wait" in html and "Longer wait" in html,
          "digest groups changes by kind")
    check("On the shelf right now" in html, "digest lists what's currently borrowable")
    check("http://home:8080/?p=abc" in html, "digest links back to the report")
    check("Piranesi" in text and "down from" in text, "plain-text alternative is readable")
    # A title containing markup: the old version of this check used only clean
    # titles, so it passed whether or not anything was being escaped.
    # The author never appears in the digest, so the ampersand has to ride in on
    # a field that does — the library name.
    nasty = [Change(kind="now_available", title="<script>alert(1)</script>",
                    author="X", library='Smith & Jones "Memorial"', scope_key="q")]
    _, _, hostile = build_email("L", nasty, period="week")
    check("<script>" not in hostile, "markup in a title is escaped, not emitted")
    check("&lt;script&gt;" in hostile, "and comes through as text")
    check("&amp;" in hostile and "&quot;" in hostile, "ampersands and quotes too")

    daily_subject, _, daily_html = build_email("L", changes, period="day")
    check("Today" in daily_html and "This week" not in daily_html, "daily digest says Today")

    # Nothing should blow up on an empty change set
    _, _, empty_html = build_email("L", [], period="week")
    check("No changes" in empty_html, "an empty digest still renders")


def test_update_list_from_upload():
    print("\nUpdating a saved list from a new export")
    sg = (FIXTURES / "storygraph.csv").read_bytes()
    books = client.post("/api/import", files={"file": ("sg.csv", sg, "text/csv")}).json()["books"]
    slug = client.post("/api/profile", json={
        "name": "Updatable", "scopes": [{"key": "westmount", "name": "Westmount"}], "books": books,
    }).json()["slug"]
    client.post(f"/api/profile/{slug}/run")
    before_states = len(store.profile_state_get(slug))
    check(before_states > 0, "the first run remembered some state")

    # A fresh export: one book dropped, one added.
    trimmed = [b for b in books if "Piranesi" not in b["title"]]
    trimmed.append({"title": "Der Schwarm", "author": "Frank Schätzing", "isbn": ""})
    out = client.post(f"/api/profile/{slug}/books", json={
        "name": "Updatable", "scopes": [{"key": "westmount", "name": "Westmount"}], "books": trimmed,
    }).json()
    check(out["total"] == len(trimmed), f"list now has {out['total']} books")
    check(any("Schwarm" in t for t in out["added"]), f"reports what was added: {out['added']}")
    check(any("Piranesi" in t for t in out["removed"]), f"reports what was removed: {out['removed']}")
    check(out["state_rows_dropped"] > 0, "forgets the removed book's remembered state")

    reloaded = client.get(f"/api/profile/{slug}").json()
    check(len(reloaded["books"]) == len(trimmed), "the saved list really changed")
    check(not any("Piranesi" in b["title"] for b in reloaded["books"]), "the dropped book is gone")

    r = client.post("/api/profile/nosuchslug/books", json={"books": []})
    check(r.status_code == 404, "updating an unknown list is a 404")


def test_real_storygraph_export():
    """A real StoryGraph export, not a hand-written fixture.

    Kept because synthetic fixtures agree with whatever the parser already does.
    This one carries the things a real export has and a made-up one doesn't:
    2,000 rows across five read-statuses, translators and narrators inside the
    author field, subtitles with colons and commas, non-Latin names, and one
    book with no author at all.
    """
    print("\nA real StoryGraph export")
    path = FIXTURES / "real_storygraph.csv"
    if not path.exists():
        print("  skipped — fixture not present")
        return

    books, report = parse_reading_list(path.read_bytes())
    check(report["source"] == "storygraph", "recognised as StoryGraph")
    check(report["rows"] > 2000, f"the whole export is read ({report['rows']} rows)")
    check(set(report["statuses_found"]) >= {"read", "to-read", "did-not-finish"},
          f"every read-status is counted: {sorted(report['statuses_found'])}")
    check(len(books) > 1000, f"and only to-read is imported ({len(books)})")
    check(report["skipped_duplicate"] > 0, "duplicates are merged, not counted twice")
    check(report["has_dates"] and not report["has_pages"],
          "StoryGraph gives dates but no page counts")
    check(all(b.added for b in books), "every book has a parsed date to sort by")

    # A quarter of real authors carry a translator or narrator. The surname has
    # to be the writer's, or an author sort is nonsense and matching suffers.
    from app.matching import surname
    from app.service import to_book
    with_role = [b for b in books if "(" in b.author]
    check(len(with_role) > 100,
          f"the export really does carry co-credits ({len(with_role)} of them)")
    sample = next(b for b in with_role if b.author.startswith("Mohamed Choukri"))
    check(surname(sample.author) == "choukri",
          f"the writer's surname wins over the translator's: {sample.author!r}")
    # A couple of books lead with a name in Arabic or Chinese script, which has
    # no ASCII surname to give. What matters is that *some* credited name does,
    # since score_candidate compares the whole set.
    from app.csvimport import split_authors
    no_surname = [b.author for b in books
                  if b.author and not any(surname(a) for a in split_authors(b.author))]
    check(not no_surname, f"every credited book yields a surname somewhere: {no_surname[:2]}")
    lead_only = [b.author for b in books if b.author and not surname(b.author)]
    check(lead_only, f"including ones whose lead name is non-Latin: {lead_only[:1]}")

    keys = {to_book(b).key for b in books}
    check(len(keys) > len(books) * 0.98,
          f"normalised keys stay distinct ({len(keys)} for {len(books)} books) — "
          "no repeat of the 'Book 1' collision")


def test_both_formats():
    """A run asked for two formats must actually check two.

    The batch pipeline is single-format by nature — one title-id map, one
    search, one bulk call per format — and it used to quietly take formats[0]
    and ignore the rest. Asking for audiobooks and ebooks got you audiobooks
    twice over, which nothing would have noticed.
    """
    print("\nBoth formats in one run")
    both = ["audiobook-overdrive", "ebook-overdrive"]
    r = client.post("/api/lookup", json={
        "books": [{"title": "Piranesi", "author": "Susanna Clarke"},
                  {"title": "The Fifth Season", "author": "N.K. Jemisin"}],
        "scopes": ["queenslibrary", "westmount"],
        "formats": both,
    })
    check(r.status_code == 200, "a two-format lookup is accepted")
    results = r.json()["results"]
    for book in results:
        fmts = sorted({row["fmt"] for row in book["results"]})
        check(fmts == both[::1] or fmts == sorted(both),
              f"{book['title']}: rows for both formats, not one twice ({fmts})")
        check(len(book["results"]) == 4,
              f"{book['title']}: one row per library per format ({len(book['results'])})")
        labels = {row["format"] for row in book["results"]}
        check(labels == {"Audiobook", "Ebook"},
              f"{book['title']}: and each row is labelled for a reader ({sorted(labels)})")

    one = client.post("/api/lookup", json={
        "books": [{"title": "Piranesi", "author": "Susanna Clarke"}],
        "scopes": ["queenslibrary"],
        "formats": ["ebook-overdrive"],
    }).json()["results"][0]
    check({row["fmt"] for row in one["results"]} == {"ebook-overdrive"},
          "asking for one format still gives exactly that one")


def test_state_keys_by_format():
    """Change detection has to tell the two formats apart.

    Before `fmt` joined the key, the audiobook and ebook rows for one
    (list, book, library) overwrote each other, so every run saw the status
    flip and reported it as news.
    """
    print("\nRemembered state, per format")
    from app.models import Availability

    slug = store.profile_save(None, name="fmt", scopes=[], formats=[], books=[])
    audio = Availability(scope_key="queenslibrary", scope_name="Queens",
                         status="available", fmt="audiobook-overdrive")
    ebook = Availability(scope_key="queenslibrary", scope_name="Queens",
                         status="holdable", wait_days=30, fmt="ebook-overdrive")
    store.profile_state_put(slug, "bk", audio)
    store.profile_state_put(slug, "bk", ebook)

    state = store.profile_state_get(slug)
    check(len(state) == 2, f"both rows survive rather than one clobbering the other ({len(state)})")
    check(state[("bk", "queenslibrary", "audiobook-overdrive")]["status"] == "available",
          "the audiobook keeps its own status")
    check(state[("bk", "queenslibrary", "ebook-overdrive")]["wait_days"] == 30,
          "and the ebook keeps its own wait")

    store.profile_state_put(slug, "bk", audio.model_copy(update={"status": "holdable"}))
    state = store.profile_state_get(slug)
    check(len(state) == 2, "updating one leaves the other alone")
    check(state[("bk", "queenslibrary", "ebook-overdrive")]["status"] == "holdable",
          "with the ebook untouched")


if __name__ == "__main__":
    store.init_db()
    for fn in [
        test_csv_parsing, test_real_storygraph_export, test_matching, test_libraries, test_import_and_lookup,
        test_both_formats, test_state_keys_by_format,
        test_jobs, test_large_list, test_progress_reporting, test_out_of_order_streaming, test_adaptive_rate, test_bulk_size_probe,
        test_cheapest_first, test_change_detection, test_email_digest,
        test_profile_watch_and_run, test_update_list_from_upload,
        test_schema_migration, test_cross_origin_writes, test_bad_input,
    ]:
        fn()
    print("\nAll tests passed.")
