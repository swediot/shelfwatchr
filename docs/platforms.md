# Why only Libby

The original idea was a single page showing availability across Libby, Libro.fm,
Spotify and others. That turned out not to be buildable on honest footing. Here's
what the research found, so nobody has to repeat it.

Researched August 2026.

| Platform | Public API? | Verdict |
| --- | --- | --- |
| **Libby / OverDrive** | Yes — `thunder.api.overdrive.com`, keyless | **Used.** The only source with a first-class audiobook filter and real availability data. |
| Libro.fm | No | An audiobookshelf maintainer records Libro.fm saying API access is "aspirational". The affiliate programme is link-only. Scraping would be the sole route. |
| Spotify | Yes, but | See below — technically possible, legally awkward, and missing the field that matters. |
| Audible | No longer | The Product Advertising API was sunset on 30 April 2026. What's left is a reverse-engineered client that needs your real account credentials and risks the account. |
| Storytel / BookBeat / Nextory / Legimi | No | Nothing official, and nothing meaningful unofficial either. |
| hoopla | Partner only | A real API exists, but the credentials are issued to a library, not to a person. |
| Chirp | No | Not even a reverse-engineered REST API — the only known tooling hooked the browser audio element. |

## The Spotify problem specifically

Three separate issues, any one of which would be enough:

1. **The API can't answer the question.** The audiobook object exposes market
   presence and nothing else. Whether a title is inside your Premium listening
   hours, needs an Audiobooks+ add-on, or is purchase-only is not in the schema.
   "Available on Spotify" would mean "exists in your country", which is not what
   anyone means by available.
2. **The terms forbid the layout.** The Branding Guidelines say Spotify content
   "should never be seated next to content from similar services", and the
   Developer Terms bar using the platform for benchmarking. A row in a
   comparison table is the thing being described.
3. **The market list is stale.** The docs still name six countries; the consumer
   rollout reached Switzerland in April 2025 and the Nordics in November 2025.
   There's a documented case of the API 404ing in a market Spotify had already
   launched. Availability answers would have been unreliable in exactly the place
   they'd be used from.

## What replaced the idea

Nothing, deliberately. A row of "search Libro.fm for this title" links was on the
table, but it adds a column of links that lead to a search box — which is what a
browser bookmark already does. The tool does one thing with real data instead.

## The two endpoints that make it fast

Worth recording, since they're undocumented and were hard to pin down. Both come
from the [Libby calibre plugin's client](https://github.com/ping/libby-calibre-plugin/blob/main/calibre-plugin/overdrive/client.py),
which is real working code against the same API:

- `GET /v2/media/search/?libraryKey=a&libraryKey=b&query=…&format=…` — searches
  several libraries in one call. Note *repeated* `libraryKey` params, not a
  comma-joined list. The plugin caps this at 24 libraries.
- `POST /v2/libraries/{key}/media/availability` with body `{"ids": [...]}` —
  availability for many titles at one library. The plugin chunks at 24; whether
  that's a server limit or just caution is unknown.
- `GET /v2/media/bulk?titleIds=a,b,c` — global metadata for many titles. Not used
  here: there's no evidence it carries per-library availability.

Title ids (`titleId` / `reserveId` / `crossRefId`) appear to be one global
identifier space rather than per-library, which is what lets a book be resolved
once and then checked at every library by id. That's inferred from the plugin
calling library-less `media/{id}` endpoints and feeding the results into
library-scoped ones — not independently confirmed against two divergent
catalogues, so the code falls back to a per-library search if an id doesn't
resolve where it should.

No rate limit is published for any of this. The one public data point is an
Apify actor describing itself as "rate-polite by default (~2 requests/second)"
against the same API, which is where this project's 120/minute default comes from.

If any of the above changes — Libro.fm shipping an API is the likeliest — the
provider interface in `app/providers/base.py` is where it would slot in. Each
provider supplies `search_scopes()` and `lookup()`, returns the shared
`Availability` shape, and the report groups it with everything else. Nothing in
the frontend assumes there is only one.
