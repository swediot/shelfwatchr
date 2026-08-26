"""The real OverDrive client, offline.

`app/providers/libby.py` does the fiddliest work in the app — retries, 429
handling, backing the rate limiter off, and parsing a response shape nobody
documents — and until now it had no tests at all, because every other test uses
the mock provider. httpx's MockTransport fixes that: a real LibbyProvider with a
real httpx client, answering from a function instead of a socket.

Run: python tests/test_provider.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update(
    SHELFWATCHR_DB=str(Path(tempfile.mkdtemp(prefix="prov-")) / "p.db"),
    SHELFWATCHR_REFRESH="0",
    SHELFWATCHR_RETRIES="3",
    # The backoff is real and correct; these tests just shouldn't sit through
    # 45 seconds of it to prove the retry loop works.
    SHELFWATCHR_BACKOFF_BASE="0.01",
)

import httpx  # noqa: E402

from app.matching import Book  # noqa: E402
from app.models import Scope  # noqa: E402
from app.providers.base import ProviderError, RateLimiter  # noqa: E402
from app.providers.libby import (  # noqa: E402
    LibbyProvider, _authors_of, _availability_of, _items, title_url,
)

failures = []


def check(condition, message):
    if condition:
        print(f"  ok — {message}")
    else:
        failures.append(message)
        print(f"  FAILED — {message}")


def provider(handler, **limiter_kwargs):
    """A real provider whose socket is a function."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    limiter = RateLimiter(6000, adaptive=True, probe_after=2, **limiter_kwargs)
    return LibbyProvider(client=client, limiter=limiter)


SCOPE = Scope(key="westmount", name="Westmount")
BOOK = Book(title="Piranesi", authors=("Susanna Clarke",))


# ------------------------------------------------------------ parsing


def test_availability_parsing():
    print("\nReading an availability payload")
    live = {"isAvailable": True, "availableCopies": 2, "ownedCopies": 4, "holdsCount": 0}
    got = _availability_of(live)
    check(got["available"] == 2 and got["owned"] == 4, "the obvious shape reads correctly")

    # The same numbers one level down, which is where some responses put them.
    nested = {"availability": {"isAvailable": False, "availableCopies": 0,
                               "ownedCopies": 3, "holdsCount": 7, "estimatedWaitDays": 30}}
    got = _availability_of(nested)
    check(got["owned"] == 3 and got["wait"] == 30, "a nested payload reads the same")

    # Alternate spellings seen in older OverDrive documentation.
    alt = {"copiesOwned": 5, "copiesAvailable": 1, "numberOfHolds": 2}
    got = _availability_of(alt)
    check(got["owned"] == 5 and got["available"] == 1 and got["holds"] == 2,
          "alternate field names are understood")

    junk = {"ownedCopies": "lots", "availableCopies": None, "isAvailable": "yes"}
    got = _availability_of(junk)
    check(got["owned"] == 0 and got["available"] == 0, "junk values don't raise, they read as zero")
    check(got["is_available"] is True, "and a truthy string still counts as available")

    check(_availability_of({})["owned"] == 0, "an empty payload is survivable")
    check(_items(None) == [] and _items({"items": [{"a": 1}]}) == [{"a": 1}]
          and _items([{"b": 2}]) == [{"b": 2}],
          "items() copes with dict, list and nothing at all")

    creators = {"creators": [{"name": "A Narrator", "role": "Narrator"},
                             {"name": "The Author", "role": "Author"}]}
    # The bulk endpoint answers positionally, padding with nulls for titles the
    # library doesn't carry. One of those used to take down the whole request.
    check(_items({"items": [{"id": "1"}, None, {"id": "2"}]}) == [{"id": "1"}, {"id": "2"}],
          "a null padding entry is dropped rather than crashing the batch")

    check(_authors_of(creators) == ["The Author"], "narrators aren't mistaken for authors")
    check(_authors_of({"firstCreatorName": "Solo"}) == ["Solo"],
          "falling back to firstCreatorName when there's no creator list")


# ------------------------------------------------------------- retries


def test_retries_and_throttling():
    print("\nRetries, 429s and backing off")

    calls = {"n": 0}

    # Retry-After of "0" is falsy, so the client falls back to its own 5/10/20s
    # schedule and the test would spend half a minute asleep proving nothing.
    # A small positive value exercises the same path in milliseconds.
    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={})
        if calls["n"] == 2:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"items": []})

    p = provider(flaky)
    started = p.limiter.rate
    out = asyncio.run(p._request("GET", "https://example.invalid/x", params={}))
    check(out == {"items": []}, f"it retries through a 429 and a 503 ({calls['n']} attempts)")
    check(p.limiter.throttles == 2, "and records both as throttles")
    check(p.limiter.rate < started, f"which slows the whole server down ({started} -> {p.limiter.rate})")
    asyncio.run(p.aclose())

    def always_429(request):
        return httpx.Response(429, headers={"Retry-After": "0.01"}, json={})

    p = provider(always_429)
    try:
        asyncio.run(p._request("GET", "https://example.invalid/x", params={}))
        check(False, "gives up eventually rather than looping forever")
    except ProviderError as exc:
        check("429" in str(exc), f"gives up eventually with a useful error ({exc})")
    asyncio.run(p.aclose())

    p = provider(lambda r: httpx.Response(404, json={}))
    check(asyncio.run(p._request("GET", "https://example.invalid/x", params={})) is None,
          "a 404 is 'nothing there', not an error")
    asyncio.run(p.aclose())

    p = provider(lambda r: httpx.Response(400, json={}))
    try:
        asyncio.run(p._request("GET", "https://example.invalid/x", params={}))
        check(False, "a 400 raises")
    except ProviderError as exc:
        check(exc.status == 400, "a 400 raises, carrying the status so callers can react")
    asyncio.run(p.aclose())

    # No Retry-After here: this path uses the client's own backoff, so keep the
    # retry count low to stay quick.
    def broken_json(request):
        return httpx.Response(200, content=b"<html>not json</html>",
                              headers={"content-type": "application/json"})

    p = provider(broken_json)
    try:
        asyncio.run(p._request("GET", "https://example.invalid/x", params={}))
        check(False, "malformed JSON raises rather than returning garbage")
    except ProviderError:
        check(True, "malformed JSON is retried, then raises rather than returning garbage")
    asyncio.run(p.aclose())

    def transport_error(request):
        raise httpx.ConnectError("no route to host")

    p = provider(transport_error)
    try:
        asyncio.run(p._request("GET", "https://example.invalid/x", params={}))
        check(False, "a connection failure raises")
    except ProviderError as exc:
        check("ConnectError" in str(exc), f"a connection failure surfaces as ProviderError ({exc})")
    asyncio.run(p.aclose())


# ------------------------------------------------------------ requests


def test_request_shapes():
    print("\nWhat it actually sends")
    seen = {}

    def record(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode() if request.content else ""
        if "media/search" in str(request.url):
            return httpx.Response(200, json={"items": [{
                "id": "9999", "title": "Piranesi", "firstCreatorName": "Susanna Clarke",
                "creators": [{"name": "Susanna Clarke", "role": "Author"}],
                "isAvailable": False, "ownedCopies": 2, "holdsCount": 4,
                "estimatedWaitDays": 21,
            }]})
        return httpx.Response(200, json=[{"id": "9999", "isAvailable": True,
                                          "availableCopies": 1, "ownedCopies": 2}])

    p = provider(record)

    found = asyncio.run(p.search_across(BOOK, ["westmount", "queenslibrary"],
                                        "audiobook-overdrive", 0.78))
    check(seen["url"].count("libraryKey=") == 2,
          "a multi-library search repeats libraryKey rather than joining with commas")
    check("x-client-id=dewey" in seen["url"], "and identifies itself the way Libby's own app does")
    check(found and found["id"] == "9999", f"the match comes back with its id ({found and found['id']})")

    got = asyncio.run(p.availability_bulk("westmount", ["1", "2", "3"]))
    check(seen["method"] == "POST", "bulk availability is a POST")
    check('"ids"' in seen["body"] and '"3"' in seen["body"], f"with the ids in the body: {seen['body']}")
    check("9999" in got, "and the response is keyed by title id")
    asyncio.run(p.aclose())

    # A search that matches nothing must not invent a match.
    p = provider(lambda r: httpx.Response(200, json={"items": [
        {"id": "1", "title": "Babel-17", "firstCreatorName": "Samuel R. Delany",
         "creators": [{"name": "Samuel R. Delany", "role": "Author"}]}]}))
    check(asyncio.run(p.search_across(
        Book(title="Babel", authors=("R. F. Kuang",)), ["westmount"],
        "audiobook-overdrive", 0.78)) is None,
        "a near-miss by the wrong author is not accepted as a match")
    asyncio.run(p.aclose())


def test_format_filter():
    """The edition asked for is the edition reported.

    The multi-library search endpoint takes a format parameter and ignores it:
    it answers with audiobooks and ebooks mixed together whatever is asked. Left
    unfiltered, scoring picked whichever edition matched the title best — for
    most books the ebook — and the ebook's copies were then reported under the
    "Audiobook" heading. Hence a book shown as available that wasn't.
    """
    print("\nAsking for an audiobook and getting one")

    MIXED = {"items": [
        {"id": "eb", "title": "Piranesi", "type": {"id": "ebook"},
         "firstCreatorName": "Susanna Clarke",
         "creators": [{"name": "Susanna Clarke", "role": "Author"}],
         "isAvailable": True, "availableCopies": 4, "ownedCopies": 4},
        {"id": "ab", "title": "Piranesi", "type": {"id": "audiobook"},
         "firstCreatorName": "Susanna Clarke",
         "creators": [{"name": "Susanna Clarke", "role": "Author"}],
         "isAvailable": False, "ownedCopies": 1, "holdsCount": 12},
    ]}

    p = provider(lambda r: httpx.Response(200, json=MIXED))
    found = asyncio.run(p.search_across(BOOK, ["westmount"], "audiobook-overdrive", 0.78))
    check(found and found["id"] == "ab",
          f"a mixed search result yields the audiobook, not the ebook ({found and found['id']})")
    found = asyncio.run(p.search_across(BOOK, ["westmount"], "ebook-overdrive", 0.78))
    check(found and found["id"] == "eb",
          f"and the ebook when the ebook is what was asked for ({found and found['id']})")
    asyncio.run(p.aclose())

    # The same filter on the single-library path.
    p = provider(lambda r: httpx.Response(200, json=MIXED))
    av = asyncio.run(p.lookup(BOOK, SCOPE, "audiobook-overdrive", 0.78))
    check(av.title_id == "ab", f"the per-library lookup filters too ({av.title_id})")
    check(av.status == "holdable",
          f"so a queued audiobook isn't reported available off the ebook's copies ({av.status})")
    asyncio.run(p.aclose())

    # An item that doesn't say what it is must not be dropped: some payloads
    # carry no type at all, and a missing field is not a wrong format.
    p = provider(lambda r: httpx.Response(200, json={"items": [
        {"id": "untyped", "title": "Piranesi", "firstCreatorName": "Susanna Clarke",
         "creators": [{"name": "Susanna Clarke", "role": "Author"}],
         "isAvailable": True, "availableCopies": 1, "ownedCopies": 1}]}))
    found = asyncio.run(p.search_across(BOOK, ["westmount"], "audiobook-overdrive", 0.78))
    check(found and found["id"] == "untyped", "an item with no stated type still matches")
    asyncio.run(p.aclose())


def test_title_link():
    """The link opens the book, not the library's front page.

    `library/<key>/similar-<id>/page-1/<id>` is the shape Libby's own client
    builds. The list segment has to be a list Libby knows; an invented one
    ("format-audiobook") resolves to the library home with no title open.
    """
    print("\nThe shape of a title link")
    url = title_url("westmount", "555", "audiobook-overdrive")
    check(url == "https://libbyapp.com/library/westmount/similar-555/page-1/555",
          f"the title id names both the list and the title opened over it ({url})")
    check(title_url("westmount", "555", "ebook-overdrive") == url,
          "and the id alone picks the edition, so format doesn't change the link")


def test_lookup_end_to_end():
    print("\nOne lookup, start to finish")

    def catalogue(request):
        return httpx.Response(200, json={"items": [{
            "id": "555", "title": "Piranesi", "subtitle": "",
            "firstCreatorName": "Susanna Clarke",
            "creators": [{"name": "Susanna Clarke", "role": "Author"}],
            "isAvailable": False, "availableCopies": 0, "ownedCopies": 3,
            "holdsCount": 9, "estimatedWaitDays": 45,
        }]})

    p = provider(catalogue)
    av = asyncio.run(p.lookup(BOOK, SCOPE, "audiobook-overdrive", 0.78))
    check(av.status == "holdable", "a queued title comes back holdable")
    check(av.wait_days == 45 and not av.wait_estimated, "with the library's own estimate, unaltered")
    check(av.title_id == "555", "carrying the id, so a share link can be built")
    check(av.url.endswith("/555") and SCOPE.key in av.url, f"and a link into Libby: {av.url}")
    check(av.holds == 9 and av.owned_copies == 3, "queue and copies come through")
    asyncio.run(p.aclose())

    # No estimate from the library: we derive one and say so.
    def no_estimate(request):
        return httpx.Response(200, json={"items": [{
            "id": "556", "title": "Piranesi", "firstCreatorName": "Susanna Clarke",
            "creators": [{"name": "Susanna Clarke", "role": "Author"}],
            "isAvailable": False, "availableCopies": 0, "ownedCopies": 2, "holdsCount": 5,
        }]})

    p = provider(no_estimate)
    av = asyncio.run(p.lookup(BOOK, SCOPE, "audiobook-overdrive", 0.78))
    check(av.wait_estimated is True and av.wait_days > 0,
          f"a missing estimate is derived and flagged ({av.wait_days} days)")
    asyncio.run(p.aclose())

    # The library doesn't have it.
    p = provider(lambda r: httpx.Response(200, json={"items": []}))
    av = asyncio.run(p.lookup(BOOK, SCOPE, "audiobook-overdrive", 0.78))
    check(av.status == "not_owned", "an empty result set means not owned, not an error")
    asyncio.run(p.aclose())

    # The API is down: an error, never a silent "not owned".
    p = provider(lambda r: httpx.Response(500, json={}))
    av = asyncio.run(p.lookup(BOOK, SCOPE, "audiobook-overdrive", 0.78))
    check(av.status == "error", "a server error is reported as an error, not as absence")
    asyncio.run(p.aclose())


def test_library_search():
    print("\nFinding a library")

    def libraries(request):
        if "/libraries/westmount" in str(request.url):
            return httpx.Response(200, json={"name": "Westmount Public Library",
                                             "preferredKey": "westmount"})
        return httpx.Response(200, json={"items": [
            {"preferredKey": "queenslibrary", "name": "Queens Public Library"},
            {"preferredKey": "westmount", "name": "Westmount Public Library"},
        ]})

    p = provider(libraries)
    found = asyncio.run(p.search_scopes("westmount"))
    keys = [s.key for s in found]
    check("westmount" in keys, "a slug resolves to its library")
    check(len(keys) == len(set(keys)), f"with no duplicates when it also appears in search ({keys})")
    asyncio.run(p.aclose())

    p = provider(lambda r: httpx.Response(500, json={}))
    check(asyncio.run(p.search_scopes("anything")) == [],
          "a failed library search returns nothing rather than raising")
    asyncio.run(p.aclose())


if __name__ == "__main__":
    for fn in [test_availability_parsing, test_retries_and_throttling,
               test_request_shapes, test_format_filter, test_title_link,
               test_lookup_end_to_end, test_library_search]:
        fn()
    print()
    if failures:
        print(f"{len(failures)} failure(s).")
        sys.exit(1)
    print("Provider tests passed.")
