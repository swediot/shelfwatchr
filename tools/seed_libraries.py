"""Fill the local library directory from OverDrive, so the picker answers offline.

The picker calls /api/libraries, which prefers `library_dir` and only falls back
to a live search. Seeding that table once means the picker is instant, works
with no network, and — more to the point — actually filters, which the upstream
search does not (it ignores `query` and returns the same first page whatever you
type). See the note in app/providers/libby.py.

    python tools/seed_libraries.py            # ~2,900 live libraries
    python tools/seed_libraries.py --all      # all 13,080, dead ones included

Run it again whenever you want a refresh; it upserts, so nothing is duplicated.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.parse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Scope           # noqa: E402
from app.providers.libby import HEADERS, THUNDER  # noqa: E402
from app import store                  # noqa: E402

PER_PAGE = 100

# The directory carries no country field, so the library's own home page is the
# only locality signal there is. A ccTLD is trustworthy; .org/.com/.edu are not
# (overwhelmingly US, but not reliably), so those stay blank rather than guess.
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
    """Where the library is, on the evidence available.

    OverDrive's directory carries no country field — not in the list, not in the
    per-library record — so this reads it off the hostnames the library gives for
    itself, and falls back to unmistakable place names in its own name. Anything
    ambiguous stays blank; a wrong country is worse than none.
    """
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


def usable(item: dict) -> bool:
    """Live and not a demo. The other 78% are Preview, Terminated or Merged —
    they resolve to nothing, and they would bury the real hits in the picker."""
    return item.get("status") == "Live" and not item.get("isDemo")


def scope_of(item: dict) -> Scope | None:
    key = item.get("preferredKey") or item.get("id") or ""
    name = item.get("name") or item.get("fulfillmentId") or key
    return Scope(key=key, name=name, region=region_of(item)) if key else None


def enrich(args) -> int:
    """Second pass: ask each region-less library for its own record."""
    with store.db() as conn:
        blanks = [(r["key"], r["name"]) for r in conn.execute(
            "SELECT key, name FROM library_dir WHERE region = '' ORDER BY key")]
    print(f"{len(blanks)} libraries without a region")
    filled, done = [], 0
    started = time.time()

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        for key, name in blanks:
            done += 1
            try:
                resp = client.get(f"{THUNDER}/libraries/{key}")
                item = resp.json() if resp.status_code == 200 else {}
            except Exception:  # noqa: BLE001
                item = {}
            item.setdefault("name", name)
            region = region_of(item)
            if region:
                filled.append(Scope(key=key, name=name, region=region))
            if len(filled) >= 100 and not args.dry_run:
                store.remember_libraries(filled)
                filled = []
            if done % 25 == 0 or done == len(blanks):
                print(f"\r{done}/{len(blanks)}  placed {len(filled)}", end="", flush=True)
            if args.delay:
                time.sleep(args.delay)

    if filled and not args.dry_run:
        store.remember_libraries(filled)
    with store.db() as conn:
        have = conn.execute("SELECT count(*) FROM library_dir WHERE region <> ''").fetchone()[0]
    print(f"\ndone in {time.time() - started:.0f}s — {have} libraries now carry a region")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true",
                    help="keep Preview/Terminated/Merged entries too")
    ap.add_argument("--delay", type=float, default=0.4,
                    help="seconds between pages (default 0.4)")
    ap.add_argument("--max-pages", type=int, default=0, help="stop early; 0 = no limit")
    ap.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    ap.add_argument("--enrich", action="store_true",
                    help="fill in blank regions from each library's own record "
                         "(one request per library — slow, but the list endpoint "
                         "leaves out links the per-library record has)")
    args = ap.parse_args()

    store.init_db()
    if args.enrich:
        return enrich(args)
    seen: set[str] = set()
    kept = skipped = page = 0
    started = time.time()

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        while True:
            page += 1
            if args.max_pages and page > args.max_pages:
                break
            try:
                resp = client.get(f"{THUNDER}/libraries",
                                  params={"perPage": PER_PAGE, "page": page})
                resp.raise_for_status()
                items = (resp.json() or {}).get("items") or []
            except Exception as exc:  # noqa: BLE001
                print(f"page {page}: {exc} — stopping", file=sys.stderr)
                break
            if not items:
                break

            wanted = items if args.all else [i for i in items if usable(i)]
            skipped += len(items) - len(wanted)
            scopes = [s for s in map(scope_of, wanted) if s and s.key not in seen]
            seen.update(s.key for s in scopes)

            if scopes and not args.dry_run:
                store.remember_libraries(scopes)
            kept += len(scopes)

            print(f"\rpage {page:>4}  kept {kept:>6}  skipped {skipped:>6}", end="", flush=True)
            if args.delay:
                time.sleep(args.delay)

    verb = "would store" if args.dry_run else "stored"
    print(f"\n{verb} {kept} libraries ({skipped} skipped) in {time.time() - started:.0f}s")
    print(f"database: {store.settings.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
