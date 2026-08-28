"""Rebuild app/data/libraries.tsv — the library directory the picker ships with.

    python tools/seed_libraries.py            # write the bundle
    python tools/seed_libraries.py --dry-run  # report what would change

The app loads that file at startup, so this only needs running when the
directory itself moves on: a library joins Libby, leaves, or is renamed. A few
times a year is plenty. Commit the result; that is what ships.

Three passes, because OverDrive spreads a library across three endpoints:

  /libraries              the list — 13,000 rows, of which ~2,200 are live
  /libraries/{key}        the same row plus the links the list leaves out
  /libraries/{key}/branches   every branch: more hostnames, and the town names

The last one earns its requests twice over. The hostnames place hundreds of
consortia the library record alone cannot ("CLEVNET" says nothing; its branches
sit on .oh.us), and the branch names are how someone finds the consortium that
serves their town when its name mentions no town at all — searching "Andover"
has to find CLEVNET.

Note what is *not* here: a search. The upstream `query` parameter is ignored —
/libraries answers with the same first page whatever is asked — which is why
the app searches the bundle locally instead. See app/libdir.py.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import sys
import time
import urllib.parse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.libdir import BUNDLE, KIND_LABEL, read_bundle, words, write_bundle  # noqa: E402
from app.providers.libby import HEADERS, THUNDER                 # noqa: E402

PER_PAGE = 100
CONCURRENCY = 8
PLACE_LIMIT = 150          # words of branch names kept per library

# The directory carries no country field, so the hostnames a library gives for
# itself are the only locality signal there is. A ccTLD is trustworthy;
# .org/.com are not (overwhelmingly US, but not reliably), so those stay blank
# rather than guess. A wrong country is worse than none.
COUNTRY = {
    "uk": "United Kingdom", "ca": "Canada", "au": "Australia", "nz": "New Zealand",
    "ie": "Ireland", "za": "South Africa", "jp": "Japan", "sg": "Singapore",
    "hk": "Hong Kong", "in": "India", "my": "Malaysia", "ph": "Philippines",
    "ae": "United Arab Emirates", "dk": "Denmark", "no": "Norway", "se": "Sweden",
    "fi": "Finland", "nl": "Netherlands", "be": "Belgium", "de": "Germany",
    "fr": "France", "es": "Spain", "it": "Italy", "ch": "Switzerland",
    "at": "Austria", "mx": "Mexico", "br": "Brazil", "cl": "Chile",
    "co": "Colombia", "ar": "Argentina", "kr": "South Korea", "tw": "Taiwan",
    "il": "Israel", "pl": "Poland", "cz": "Czechia", "pt": "Portugal",
    "gr": "Greece", "tr": "Turkey", "th": "Thailand", "id": "Indonesia",
    "qa": "Qatar", "sa": "Saudi Arabia", "eg": "Egypt", "ke": "Kenya",
}

# Two-letter state codes as they appear in .XX.us hostnames.
US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il",
    "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt",
    "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri",
    "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
}

# Names that place a library beyond doubt. Deliberately not exhaustive: only
# entries with no plausible reading in the other country. "Ontario" is a
# California city as well as a province, "Victoria" is in both, "Washington" is
# a state and a county in several others — so none of them are here.
NAME_REGION = [
    ("USA", ("alabama", "alaska", "arizona", "arkansas", "colorado", "connecticut",
             "florida", "hawaii", "idaho", "illinois", "iowa", "kansas", "kentucky",
             "louisiana", "maryland", "massachusetts", "michigan", "minnesota",
             "mississippi", "missouri", "nebraska", "nevada", "new hampshire",
             "new jersey", "new mexico", "north dakota", "oklahoma", "pennsylvania",
             "rhode island", "south dakota", "tennessee", "utah", "vermont",
             "west virginia", "wisconsin", "wyoming", "ohio", "texas")),
    ("Canada", ("saskatchewan", "manitoba", "alberta", "british columbia", "québec",
                "quebec", "nova scotia", "new brunswick", "newfoundland", "labrador",
                "yukon", "nunavut", "northwest territories", "bibliothèques")),
    ("Switzerland", ("schweiz", "suisse", "svizzera", "zürich", "st.gallen",
                     "stgallen", "ostschweiz")),
]

# Link fields worth mining for a hostname, best first.
LINK_FIELDS = ("libraryHome", "librarySupportUrl", "cardAcquisitionUrl")
BRANCH_FIELDS = ("branchUrl", "cardAcquisitionUrl", "supportUrl")

# Words that say what kind of thing a branch is, not where it is. Dropping them
# keeps the place list to the part somebody would actually type.
BRANCH_NOISE = set(
    "branch library libraries public memorial main district county regional "
    "community the of and center centre free township village city town area "
    "school college university campus system service services municipal borough "
    "parish north south east west central bookmobile mobile annex kiosk".split()
)


def _from_host(href: str) -> str:
    host = urllib.parse.urlparse(href).netloc.lower().split(":")[0]
    if "." not in host:
        return ""
    parts = host.split(".")
    tld = parts[-1]
    if tld == "us":
        state = parts[-2] if len(parts) >= 2 else ""
        return f"{state.upper()}, USA" if state in US_STATES else "USA"
    # .gov and .mil are US-only registries; .edu has been US-only since 2001.
    if tld in ("gov", "mil", "edu"):
        return "USA"
    return COUNTRY.get(tld, "")


def region_of(item: dict) -> str:
    """Where the library is, on its own links and its own name."""
    links = item.get("links") or {}
    for field in LINK_FIELDS:
        href = (links.get(field) or {}).get("href") or ""
        if href:
            found = _from_host(href)
            if found:
                return found
    name = (item.get("name") or "").lower()
    for region, needles in NAME_REGION:
        if any(n in name for n in needles):
            return region
    return ""


def region_from_branches(branches: list) -> str:
    """A consortium's own record often has no links at all, but its branches do.

    Bare "USA" wins on count almost every time — most branches sit on .org — so
    a state that every state-qualified branch agrees on beats it. That is what
    turns "Northern California Digital Library", whose 63 .org branches say only
    USA and whose 8 .ca.us ones all say California, into "CA, USA".
    """
    votes = collections.Counter()
    for branch in branches:
        for field in BRANCH_FIELDS:
            found = _from_host(branch.get(field) or "")
            if found:
                votes[found] += 1
        email = branch.get("supportEmail") or ""
        if "@" in email:
            found = _from_host("http://" + email.rsplit("@", 1)[-1])
            if found:
                votes[found] += 1
    if not votes:
        return ""
    qualified = collections.Counter({r: n for r, n in votes.items() if "," in r})
    if qualified:
        best, count = qualified.most_common(1)[0]
        if count >= sum(qualified.values()) / 2:
            return best
    return votes.most_common(1)[0][0]


def places_of(name: str, branches: list) -> str:
    """The towns a library covers, as words, in the order the branches list them.

    Words already in the library's own name are left out — they find it anyway —
    and so are the ones that only say "Branch" or "Memorial". What's left is
    what someone types when they know their town but not their consortium.
    """
    own = set(words(name))
    kept: list[str] = []
    seen: set[str] = set()
    for branch in branches:
        for word in words(branch.get("branchName") or ""):
            if len(word) <= 2 or word.isdigit() or word in BRANCH_NOISE:
                continue
            if word in own or word in seen:
                continue
            seen.add(word)
            kept.append(word)
            if len(kept) >= PLACE_LIMIT:
                return " ".join(kept)
    return " ".join(kept)


def on_libby(item: dict) -> bool:
    """Live, not a demo, and reachable from Libby.

    The other 83% of the directory is Preview, Terminated or Merged — they
    resolve to nothing — and a handful of the live ones are Sora- or
    website-only, where a libbyapp.com link goes nowhere.
    """
    if item.get("status") != "Live" or item.get("isDemo"):
        return False
    return "libby" in (item.get("enabledPlatforms") or [])


# ------------------------------------------------------------- fetching


async def _get(client: httpx.AsyncClient, url: str, params=None, tries: int = 3):
    for attempt in range(tries):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
        except Exception:  # noqa: BLE001  — the retry is the handling
            pass
        await asyncio.sleep(1.0 * (attempt + 1))
    return None


async def fetch_list(client: httpx.AsyncClient, max_pages: int) -> list[dict]:
    first = await _get(client, f"{THUNDER}/libraries", {"perPage": PER_PAGE, "page": 1})
    if not first:
        raise SystemExit("the directory did not answer; nothing written")
    total = first.get("totalItems") or 0
    pages = -(-total // PER_PAGE)
    if max_pages:
        pages = min(pages, max_pages)
    print(f"{total} entries over {pages} pages")

    sem = asyncio.Semaphore(CONCURRENCY)

    async def page(number: int) -> list[dict]:
        async with sem:
            data = await _get(client, f"{THUNDER}/libraries",
                              {"perPage": PER_PAGE, "page": number})
            return (data or {}).get("items") or []

    rest = await asyncio.gather(*[page(n) for n in range(2, pages + 1)])
    return (first.get("items") or []) + [item for chunk in rest for item in chunk]


async def fetch_details(client: httpx.AsyncClient, keys: list[str]) -> dict:
    """Each library's own record and its branches, both needed for the region."""
    sem = asyncio.Semaphore(CONCURRENCY)
    out: dict[str, tuple[dict, list]] = {}
    done = 0

    async def one(key: str) -> None:
        nonlocal done
        async with sem:
            record = await _get(client, f"{THUNDER}/libraries/{key}")
            branches = await _get(client, f"{THUNDER}/libraries/{key}/branches")
        out[key] = (record or {}, (branches or {}).get("items") or [])
        done += 1
        if done % 25 == 0 or done == len(keys):
            print(f"\r  {done}/{len(keys)} libraries", end="", flush=True)

    await asyncio.gather(*[one(k) for k in keys])
    print()
    return out


async def collect(max_pages: int) -> list[tuple[str, str, str, str]]:
    async with httpx.AsyncClient(headers=HEADERS, timeout=40,
                                 follow_redirects=True) as client:
        items = await fetch_list(client, max_pages)
        live = [i for i in items if on_libby(i)]
        print(f"{len(live)} live on Libby, of {len(items)}")

        by_key: dict[str, dict] = {}
        for item in live:
            key = item.get("preferredKey") or item.get("id") or ""
            if key:
                by_key.setdefault(key, item)

        print("asking each library for its links and branches")
        detail = await fetch_details(client, list(by_key))

    rows = []
    for key, item in by_key.items():
        record, branches = detail.get(key, ({}, []))
        merged = {**item, **record}
        name = merged.get("name") or merged.get("fulfillmentId") or key
        region = region_of(merged) or region_from_branches(branches)
        kind = KIND_LABEL.get(merged.get("type") or "", "")
        rows.append((key, name, region, kind, places_of(name, branches)))
    return rows


def report(rows: list[tuple[str, ...]]) -> None:
    before = {r[0]: r for r in read_bundle()}
    now = {r[0]: r for r in rows}
    added = sorted(set(now) - set(before))
    gone = sorted(set(before) - set(now))
    renamed = [k for k in set(now) & set(before) if now[k][1] != before[k][1]]
    placed = sum(1 for r in rows if r[2])
    towns = sum(1 for r in rows if r[4])
    kinds = collections.Counter(r[3] or "unlabelled" for r in rows)
    print(f"{len(rows)} libraries — {placed} with a region, {towns} with town names")
    print("  " + ", ".join(f"{n} {kind}" for kind, n in kinds.most_common()))
    print(f"against the bundle on disk: +{len(added)} new, -{len(gone)} gone, "
          f"{len(renamed)} renamed")
    for key in added[:10]:
        print(f"  + {key}  {now[key][1]}")
    for key in gone[:10]:
        print(f"  - {key}  {before[key][1]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--max-pages", type=int, default=0, help="stop early; 0 = no limit")
    args = ap.parse_args()

    started = time.time()
    rows = asyncio.run(collect(args.max_pages))
    report(rows)
    if args.dry_run:
        print(f"\ndry run — {BUNDLE} untouched ({time.time() - started:.0f}s)")
        return 0
    write_bundle(rows)
    print(f"\nwrote {BUNDLE} in {time.time() - started:.0f}s — commit it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
