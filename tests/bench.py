"""How many requests does a run actually cost?

Counts provider calls (not wall-clock, which is meaningless against a mock) for
a realistic list, comparing the batch path with the one-request-per-book path it
replaced. Wall-clock is then derived from the configured rate limit.

Run: python tests/bench.py [book_count] [library_count]
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BOOKS = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
LIBS = int(sys.argv[2]) if len(sys.argv) > 2 else 3

os.environ.update(
    SHELFWATCHR_MOCK="1",
    SHELFWATCHR_MOCK_SYNTHETIC="1",
    SHELFWATCHR_DB=str(Path(tempfile.mkdtemp(prefix="bench-")) / "bench.db"),
    SHELFWATCHR_REFRESH="0",
)

from app import service, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import BookIn, Scope  # noqa: E402

SCOPES = [Scope(key=k, name=k) for k in
          ["westmount", "queenslibrary", "aubora", "brooklyn", "nypl"][:LIBS]]
FMT = ["audiobook-overdrive"]


class Counter:
    def __init__(self, provider):
        self.provider = provider
        self.reset()

    def reset(self):
        self.search_across = self.bulk = self.single = 0

    def install(self):
        p = self.provider
        s, b, l = p.search_across, p.availability_bulk, p.lookup

        async def s2(*a, **k):
            self.search_across += 1
            return await s(*a, **k)

        async def b2(*a, **k):
            self.bulk += 1
            return await b(*a, **k)

        async def l2(*a, **k):
            self.single += 1
            return await l(*a, **k)

        p.search_across, p.availability_bulk, p.lookup = s2, b2, l2

    @property
    def total(self):
        return self.search_across + self.bulk + self.single


def _fmt(secs: float) -> str:
    if secs < 90:
        return f"{secs:.0f}s"
    return f"{secs / 60:.0f} min"


def minutes(requests: int) -> str:
    """Time at the starting rate, and at the ceiling the limiter may climb to."""
    at_start = requests / settings.requests_per_minute * 60
    at_max = requests / settings.rpm_ceiling * 60
    if requests == 0:
        return "instant"
    return f"{_fmt(at_start)} → {_fmt(at_max)}"


async def run(books, use_batch: bool, refresh=False):
    counter.reset()
    size = settings.bulk_availability_size if use_batch else 24
    for start in range(0, len(books), size):
        chunk = books[start:start + size]
        if use_batch:
            await service.lookup_chunk(chunk, SCOPES, FMT, 0.78, refresh)
        else:
            await asyncio.gather(*[
                service.lookup_book(b, SCOPES, FMT, 0.78, refresh) for b in chunk
            ])
    return counter.total


store.init_db()
provider = service.get_provider()
counter = Counter(provider)
counter.install()

books = [BookIn(title=f"Invented Title {i}", author=f"Author {i % 300}") for i in range(BOOKS)]
lookups = BOOKS * LIBS

print(f"{BOOKS} books across {LIBS} libraries — {lookups} (book, library) lookups")
print(f"rate: starts at {settings.requests_per_minute:.0f}/min, "
      f"climbs to {settings.rpm_ceiling:.0f}/min while the API stays happy")
print("times below are shown as: at the starting rate → at the ceiling\n")

old = asyncio.run(run(books, use_batch=False))
print(f"one search per book per library : {old:5d} requests  ~{minutes(old)}")
print(f"   (searches {counter.single}, bulk {counter.bulk})")

store.cache_clear()
new_cold = asyncio.run(run(books, use_batch=True))
print(f"\nbatch, cold cache               : {new_cold:5d} requests  ~{minutes(new_cold)}")
print(f"   (multi-library searches {counter.search_across}, bulk availability {counter.bulk})")

store.cache_clear()   # ids stay remembered; only availability is re-fetched
new_warm = asyncio.run(run(books, use_batch=True))
print(f"batch, ids already known        : {new_warm:5d} requests  ~{minutes(new_warm)}")
print(f"   (multi-library searches {counter.search_across}, bulk availability {counter.bulk})")

fully_warm = asyncio.run(run(books, use_batch=True))
print(f"batch, nothing expired          : {fully_warm:5d} requests  ~{minutes(fully_warm)}")

print(f"\ncold run is {old / max(new_cold, 1):.1f}x fewer requests; "
      f"a repeat run is {old / max(new_warm, 1):.0f}x fewer.")
