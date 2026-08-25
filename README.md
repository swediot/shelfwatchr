# Shelfwatchr

Upload your StoryGraph or Goodreads to-read export, pick your library cards, and
get one report: what you can **borrow right now**, and what you can **place a
hold** on and how long the wait is — checked across every card at once.

Then let it watch the list for you. Once a week (or once a day) the server
re-checks everything and emails you a digest of what moved: a book that became
available, one that's newly holdable, a hold queue that shortened or grew.

Audiobooks and ebooks, with a toggle between them. Libby / OverDrive only — [docs/platforms.md](docs/platforms.md)
explains why that isn't a limitation so much as the honest shape of the problem.

---

## Running it

### On a small cloud host

`fly.toml` is set up for [Fly.io](https://fly.io) — one shared-cpu machine and a
1GB volume, which is more than this needs:

```bash
fly launch --no-deploy --copy-config --name shelfwatchr
fly volumes create shelfwatchr_data --size 1 --region fra
fly secrets set SHELFWATCHR_SMTP_HOST=... SHELFWATCHR_SMTP_USER=... SHELFWATCHR_SMTP_PASS=...
fly deploy
fly certs add shelfwatchr.example.com     # then add the DNS records it prints
```

Three settings matter on a public host and are already in `fly.toml`:
`SHELFWATCHR_SECURE_COOKIES=1` (the proxy terminates TLS, so the app has to be
told to mark the cookie `Secure` or nobody stays signed in),
`SHELFWATCHR_TRUST_PROXY=1` (so the login rate limiter sees real client
addresses rather than one proxy IP), and `SHELFWATCHR_PUBLIC_URL` (so
confirmation links point at the public name).

**Make your account, then close the door:** `fly secrets set SHELFWATCHR_SIGNUPS=0`.
Existing accounts keep working; nobody new can register. A public instance sends
real traffic to OverDrive under your IP, and there's no reason to let strangers
add to it.

Any host that runs a Dockerfile works the same way — Railway, Render, a €4 VPS
with `docker compose up -d` behind Caddy. The only requirements are a persistent
volume for the SQLite file and a process that stays up, since the nightly
refresh is a background task inside it rather than a cron job.

### On your home server

```bash
git clone <this repo> shelfwatchr && cd shelfwatchr
docker compose up -d
```

Then open `http://<your-server>:8080`. That's the whole install. State lives in
`./data/shelfwatchr.db`.

**Backing it up.** The database runs in WAL mode, so recent writes may sit in a
`-wal` file beside it — `cp` on a running server can miss them. Use SQLite's own
backup, which is safe on a live database:

```bash
docker compose exec shelfwatchr \
  sqlite3 /data/shelfwatchr.db ".backup '/data/backup.db'"
```

**Upgrading.** `git pull && docker compose up -d --build`. New columns are added
to an existing database automatically on startup (and logged); no manual migration.

The container runs as a non-root user, so if you're bind-mounting `./data`, make
sure it's writable: `mkdir -p data && sudo chown -R 10001:10001 data`.

Edit `docker-compose.yml` first if you want a different port, refresh hour, or
email. Set `SHELFWATCHR_PUBLIC_URL` to whatever address you actually reach it on,
so the links in your digest work.

**Without Docker:**

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

**To try it with no network at all:**

```bash
SHELFWATCHR_MOCK=1 uvicorn app.main:app --port 8080
# add SHELFWATCHR_MOCK_SYNTHETIC=1 to make every title resolve, for seeing
# how a 1,000-book list actually behaves
```

### Reaching it from outside the house

The server has no login — anyone who can reach the port can use it, and anyone
with a saved list's link can read that list. That's deliberate for a tool shared
with a few friends, but **don't port-forward it to the open internet**. Use
Tailscale (install on the server, share the network with whoever should have
access) or a Cloudflare Tunnel with Access in front.

---

## Reading the report

One line per book, and it answers the only question that matters first — can I
get this, and if not, how long:

```
Piranesi                                      [ Available ]
Susanna Clarke                                            ⌄

The Fifth Season                              [  8 days  ]
N.K. Jemisin                                              ⌄
```

Green means it's on the shelf at **at least one** of your libraries; yellow is
the **shortest** wait across all of them. Which library that happens to be is a
tap away — the chevron opens a per-library breakdown, with the queue behind each
wait (people waiting, copies owned) and a small legend explaining the glyphs. On
a desktop, hovering a row in that breakdown spells it out as a sentence:
*"9 people are waiting for 4 copies"*.

The headline pill links straight to that book in Libby, at whichever library is
offering the best terms — so borrowing is one tap, not two.

Colour doesn't reach a screen reader, so each pill keeps a label: "Hold, 8 days
wait", or "Available · 3 copies".

## Using it

1. **Pick your libraries.** Search by name, or paste the slug from your Libby URL
   (`libbyapp.com/library/`**`queenslibrary`**). Remembered in your browser.
2. **Drop in your CSV.** StoryGraph → Manage Account → Manage Your Data → Export.
   Goodreads → My Books → Import and Export → Export Library. Either works, the
   whole export is fine, and you choose which shelf to check.
3. **Read the report.** Available now, short wait, longer wait — each entry
   linking straight to the book in Libby.
4. **Save it, and let it watch.** Saving gives you a link that works on any
   device and loads instantly, because the scheduled run has already done the work.

### Updating the list later

Export again from StoryGraph or Goodreads and drop the new file in while your
saved list is open. The check button then says what it's about to do — *"Updates
'My audiobooks' to the 1,395 books on this shelf — 12 new, 3 gone — and checks
them"* — so pressing it replaces the saved list and re-checks in one go. Books
that left the list have their remembered history dropped too, so if one comes
back next year it isn't compared against a reading from months ago.

### Filtering and sorting a long list

Above the report there's a search box, a sort menu and a filter panel. The three
sections stay put — *Available now*, *Short wait*, *Longer wait* — because
whether you can have the book is still the first question; the sort you pick
orders the books **inside** each section.

Sort by shortest wait (the default), by when you added the book to your list,
by length, by author surname, by title, or at random when you want the list to
stop looking like the same list. Random uses a fixed seed, so scrolling and
re-rendering don't reshuffle under your thumb — there's a *shuffle again* link
for when you do want a new order.

Filter by library, by audiobook length, by whether it's on the shelf right now,
and by how long a wait you're willing to accept. The button shows a count of
what's active, and the line underneath says how many books you're looking at out
of how many. A download taken while filters are on saves exactly what's on
screen, and says on it that it's a filtered copy.

Sort and filters are remembered in your browser, and the filter panel reopens
itself when you come back with filters set — so a short list always shows its
reason.

### Audiobooks or ebooks

A three-way toggle above the report: **Audiobooks**, **Ebooks**, **Both**.

Every run checks both formats, so switching is instant and never goes back to
OverDrive. That costs about twice the requests — a cold 1,395-book run goes from
roughly 11 minutes to 22, and a nightly refresh from ~15 requests to ~30, still
seconds — and buys a toggle you can flip while looking at the list.

On **Both**, each library shows whichever format is easier to get, tagged
*Audio* or *Ebook* so "available at Westmount" says what you'd actually borrow.
On a single format, the tag disappears (every row is that format) and the
headline pill comes only from that format's rows.

The length filter is audiobook running time, so it hides itself in the Ebooks
view rather than sitting there doing nothing.

A report saved before this existed has audiobook rows only. The two
single-format segments disable themselves and a line says to re-check.

### Accounts

Optional, and off to one side of everything else: uploading a list, checking it
and keeping it by its link all work signed out, exactly as they did before
accounts existed.

What an account adds is that the list follows you. Sign in on a phone and the
list is there without pasting a link. One account holds one list; re-uploading
replaces its books and keeps the same link, so a bookmark of it goes on working.

- **Signing up** takes an email and a password of at least ten characters. No
  composition rules — length is what matters, and "must contain a symbol" pushes
  people towards `Passw0rd!`.
- **Confirming** happens by a link mailed to the address. Nothing is usable
  until it's clicked, and clicking it signs you in.
- **Forgot password** mails a link that expires in two hours and works once.
  Using it signs every other device out, because a reset is what you do when you
  think somebody else has your password.
- **Changing your password** while signed in needs the current one, and also
  signs other devices out.
- **Deleting your account** needs your password and removes the account, the
  list, its report and its history. Not a soft delete.

Neither signing up nor asking for a reset will tell you whether an address has
an account here — both answer the same way for every address, and the difference
goes in the mail. Login attempts are rate-limited per address and per IP.

Once a list belongs to an account, its link still **opens** for anybody you share
it with, but only the account can change its books or point its alerts somewhere.
A list you saved before signing up can be attached to your account afterwards;
the app offers, rather than absorbing it silently.

#### If you haven't set up SMTP

Confirmation and reset links are written to the server log instead of being
emailed, and the sign-in page says so. That's what makes a fresh instance usable
before mail is configured — but it does mean anyone who can read your logs can
take over an account, so set `SHELFWATCHR_SMTP_*` before anyone but you uses it.

#### Passwords, tokens and cookies

Passwords are hashed with `scrypt` (n=2¹⁶, r=8, p=1 — about 100ms and 64MB per
attempt), salted per password, and the parameters travel with the hash so they
can be raised later without invalidating anyone. Confirmation links, reset links
and session cookies are 256-bit random values stored only as SHA-256 hashes: a
copy of the database grants nobody a session and no working link. Session cookies
are `HttpOnly` and `SameSite=Lax`, which is also what makes cross-site request
forgery a non-event.

### The weekly email

Pick **Email digest** and set it to weekly, and you get one message a week laid
out by what you can do about it:

- **Available now** — became borrowable since the last check
- **Newly holdable** — a library that didn't have it now does
- **Shorter wait / Longer wait** — hold queues that moved meaningfully
- **On the shelf right now** — everything currently borrowable, changed or not,
  so the digest is useful even in a quiet week

It needs SMTP configured on the server (see `docker-compose.yml`) — set up once by
you, not per person, so nobody's mail password ends up in the database. No SMTP?
The email option is greyed out and the other two channels still work:

| Channel | Setup |
| --- | --- |
| **Phone push (ntfy)** | Install the [ntfy](https://ntfy.sh) app, invent a topic name, subscribe to it, paste the same name in. Nothing to sign up for. Pick something unguessable — anyone who knows the topic can read it. |
| **Webhook** | Any URL that takes a JSON POST: Discord, Slack, Home Assistant, your own script. |
| **Nothing** | Changes still appear at the top of the report next time you open it. |

There's a *Send a test* button. Use it — a typo'd ntfy topic fails silently forever otherwise.

### What counts as a change

Hold queues wobble constantly and OverDrive's wait estimate is itself a guess, so
a naive "the number moved" comparison would alert every week and be worthless
inside a month. The thresholds, in `app/changes.py`:

| Change | Fires when |
| --- | --- |
| Now available | Status becomes available from anything else |
| Newly holdable | A library that didn't have it now does |
| Shorter wait | Wait drops by **≥7 days and ≥15%** |
| Longer wait | Wait grows by **≥14 days and ≥30%** |

**Books leaving the shelf are not reported.** Someone else borrowing a copy, or a
library dropping a title, isn't something you can act on, and it made every
digest read like bad news. The comparison still happens — it's the reporting
that's suppressed. Set `SHELFWATCHR_REPORT_REMOVALS=1` if you want them back.

A first run never alerts — there's nothing to compare against. A failed lookup
never alerts either: it shows the last known value rather than pretending a book
vanished.

---

## Speed

The naive shape of this problem is one search per book per library: 1,200 books
across three libraries is 3,600 requests, which at a polite rate is half an hour
or worse. Two undocumented endpoints that Libby's own clients use collapse that:

- **One search covers every library.** `GET /v2/media/search/?libraryKey=a&libraryKey=b…`
  takes repeated `libraryKey` params and searches them all at once. Three
  libraries stop costing three requests.
- **One request covers two dozen books.** `POST /v2/libraries/{key}/media/availability`
  with `{"ids": [...]}` returns availability for a batch of titles at one library.
- **A book's OverDrive id is remembered for a month.** That's what makes repeat
  runs cheap: once a title is resolved, no search is ever needed again — just
  bulk availability. Misses are remembered too, so an absent book isn't searched
  for every week.

Three more things make it faster still:

- **The rate tunes itself.** Nobody publishes a limit for this API, so picking a
  constant means being either needlessly slow or occasionally rude. Instead the
  limiter does what congestion control does: after every 40 clean responses it
  speeds up 20%, and any 429 or 5xx halves it immediately and pauses everyone.
  It starts at 120 requests/minute and climbs toward 300 if the API is happy —
  or settles at 60 if it isn't. `/api/health` reports where it landed.
- **Batch sizes are discovered, not assumed.** Availability requests start at 96
  ids; if the server rejects one as too large it halves and retries, and the
  working size sticks for the rest of the process.
- **Cheap work goes first.** A run does the books whose ids are already known
  before the ones needing a search, so a mostly-complete report exists within
  seconds rather than after the last new title resolves. Results are stored at
  their original position, so the report's order never depends on this.

Measured with `python tests/bench.py 1200 3` (times: at the starting rate → at
the ceiling):

| | requests | time |
| --- | --- | --- |
| One search per book per library (the old way) | 3,600 | ~30 min → 12 min |
| Batch, cold — nothing known yet | 1,239 | ~10 min → 4 min |
| Batch, ids already learned | 39 | ~20 s → 8 s |
| Batch, availability still fresh in cache | 0 | instant |

So the hour is really a one-off: the first run on a new list pays for the
searches, and every run after it is seconds. A watched list is already warm when
you open it, because the scheduled run did the work overnight. And because the
id map is shared across every list on the server, a second person's list is
partly warm before they even start.

`python tests/order_demo.py` shows the ordering effect against a running server:
in a 500-book list where 100 titles are new, 384 books are answered in the first
1.4 seconds and the newcomers fill in behind them.

**The remaining floor** is one search per never-seen book. Nothing in the API
batches searches, so a genuinely new 1,200-book list can't go below ~1,200
requests — the dial from there is `SHELFWATCHR_RPM` and its ceiling
`SHELFWATCHR_RPM_MAX`. The defaults sit near what a well-behaved public scraper
of the same API uses; raising them is a judgement call about how much of a guest
you want to be.

Both endpoints are undocumented. Neither has been exercised against a live
catalogue from here, so the code detects failure and falls back to the old
one-request-per-book path, which returns identical answers more slowly. Set
`SHELFWATCHR_BATCH=0` to force the slow path; `/api/health` reports which is in use.

## Big lists

Even at 11 minutes, nobody sits on a page waiting, and no phone keeps a
connection open that long, so a lookup is a **job**, not a request:

- The work runs server-side and writes each result to SQLite as it lands.
- The page shows live progress, but the progress is a *view* of the job. Close
  the tab, lose your connection, open the link on your phone — the run continues
  and every result so far is there.
- **The progress panel** appears the moment you click, pins itself to the bottom
  of the screen once there's a report to scroll through, and shows books done,
  a percentage, a time remaining worked out from the *observed* rate rather than
  a guess, elapsed time, requests made, and which phase the run is in — looking
  up new titles is much slower than checking availability, and it says so. The
  bar keeps a moving stripe while it waits its turn at the rate limiter, because
  a run that goes quiet for a minute is normal and shouldn't look like a hang.
- You're told the estimated time before it starts, and there's a cancel button.
- The report renders 100 books per section with a *Show all*, so a thousand-book
  result doesn't take the browser down with it.

Tested end to end at 1,200 books: full re-render 46 ms, DOM capped at 300 cards
until you ask for more.

The default ceiling on one upload is 5,000 books (`SHELFWATCHR_MAX_BOOKS`).

---

## Being a good guest of the API

Shelfwatchr queries `thunder.api.overdrive.com`, the public catalogue endpoint the
Libby web app itself uses. No card number, no login, nothing personal — the only
question asked is "does this library have this title, and is a copy in".

No rate limit is published, so the server assumes it's a guest:

- **One global tap.** Evenly spaced, 3 in flight, shared across *everyone* using
  the server, so five people at once doesn't become five times the traffic.
- **Backs down harder than it climbs.** +20% after 40 clean responses; half on a
  single complaint. It gives back speed far faster than it takes it.
- **Asks for less.** Batching means a full 1,200-book re-check costs ~150
  requests, not 3,600. The politeness budget goes much further.
- **Backs off together.** A 429 or 5xx pauses the whole server, honouring `Retry-After`.
- **Caches by how fast the answer goes stale.** Available/holdable for hours,
  "not in catalogue" for a week. A watched list makes far fewer requests than its
  size suggests.
- **Never blanks a book on failure.** A failed lookup falls back to the last known
  value, labelled as such.

---

## Layout

```
app/
  main.py         HTTP API, job endpoints, the scheduler
  jobs.py         background lookup runs, resumable streaming
  service.py      orchestration: cache, concurrency, fallbacks
  providers/
    base.py       the interface a source of availability implements
    libby.py      OverDrive/Libby
    mock.py       fixture + synthetic catalogues for tests and demos
  matching.py     title/author normalisation and scoring  ← tune here
  changes.py      what counts as a change, and the thresholds
  csvimport.py    StoryGraph and Goodreads parsing
  store.py        SQLite: cache, library directory, lists, jobs, reports, accounts
  notify.py       ntfy / webhook / email digest, confirmation and reset mail
  auth.py         password hashing, tokens, rate limiting — stdlib only
  accounts.py     sign up / in / out, confirm, reset, and the account's list
web/              the frontend: no build step
  index.html      the app
  app.js          library picker, jobs, report, filters, sorting, account bar
  signin.html     sign in, create account, forgot, choose a new password
  signin.js       ← standalone, so the sign-in page doesn't load the whole app
  styles.css
tests/
  test_app.py         end-to-end against the mock catalogue
  test_auth.py        accounts: hashing, sessions, enumeration, ownership
  test_provider.py    the OverDrive client, against a mock transport
  ui_smoke.py         the report's wording and layout, in a browser
  reveal_check.py     hover and tap behaviour for the queue detail
  filter_check.py     filters and sorting, in a browser
  auth_flow_check.py  the whole account journey, in a browser
```

The three Python suites need no network:

```bash
python tests/test_app.py && python tests/test_auth.py && python tests/test_provider.py
```

The four browser suites need a server and Playwright:

```bash
SHELFWATCHR_MOCK=1 SHELFWATCHR_MOCK_SYNTHETIC=1 uvicorn app.main:app --port 8080 &
python tests/ui_smoke.py && python tests/reveal_check.py && python tests/filter_check.py
# auth_flow_check reads the confirmation link out of the server's log, so it
# needs the log in a file and no SMTP configured:
#   ... uvicorn app.main:app --port 8080 > /tmp/srv.log 2>&1 &
python tests/auth_flow_check.py http://127.0.0.1:8080 /tmp/srv.log
```

`python tests/make_big_csv.py 1200` writes a large export for load testing, and
`python tests/bench.py 1200 3` counts the requests a run would actually cost.

### Settings

All environment variables, all optional. `SHELFWATCH_*` (no R) still works.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SHELFWATCHR_DB` | `data/shelfwatchr.db` | SQLite path |
| `SHELFWATCHR_PUBLIC_URL` | — | Base URL used in digest links |
| `SHELFWATCHR_RPM` | `120` | Starting requests per minute |
| `SHELFWATCHR_RPM_MAX` / `_MIN` | `300` / `30` | Bounds the adaptive rate moves between |
| `SHELFWATCHR_ADAPTIVE_RATE` | `1` | `0` pins the rate at `RPM` |
| `SHELFWATCHR_BATCH` | `1` | Use the batch endpoints; `0` forces the slow path |
| `SHELFWATCHR_BULK_SIZE` | `96` | Titles per bulk request; shrinks automatically if refused |
| `SHELFWATCHR_TTL_TITLE_MAP` | 30 days | How long a resolved OverDrive id is trusted |
| `SHELFWATCHR_CONCURRENCY` | `3` | Requests in flight |
| `SHELFWATCHR_MATCH_THRESHOLD` | `0.78` | 0–1, higher is stricter |
| `SHELFWATCHR_SHORT_WAIT_DAYS` | `21` | Boundary between "short" and "longer" wait |
| `SHELFWATCHR_MAX_BOOKS` | `5000` | Ceiling on one upload |
| `SHELFWATCHR_REPORT_REMOVALS` | `0` | Report books that stopped being available |
| `SHELFWATCHR_REFRESH` / `_HOUR` | `1` / `4` | Scheduled run, UTC hour |
| `SHELFWATCHR_TTL_*` | 6h / 12h / 7d | Cache lifetimes per status |
| — | — | Formats aren't configurable: a run always checks audiobooks *and* ebooks, and the toggle decides what you see |
| `SHELFWATCHR_JOB_RETENTION_HOURS` | `48` | How long finished jobs are kept |
| `SHELFWATCHR_MOCK` | `0` | Fake catalogue, no network |
| `SHELFWATCHR_SMTP_*` | — | Email digest, and confirmation/reset mail |
| `SHELFWATCHR_ACCOUNTS` | `1` | `0` turns accounts off entirely |
| `SHELFWATCHR_SIGNUPS` | `1` | `0` closes new registrations, existing accounts unaffected |
| `SHELFWATCHR_SECURE_COOKIES` | from `PUBLIC_URL` | Mark the session cookie `Secure` |
| `SHELFWATCHR_TRUST_PROXY` | `0` | Believe `X-Forwarded-For`. Only behind your own proxy |

---

## Known limits

- **Matching is fuzzy.** Title plus author surname, threshold 0.78. Same-title
  different-author is rejected, and a series number on one side but not the other
  counts against a match — that's what stops *Babel* matching *Babel-17*. If books
  you know your library has come back as "not in catalogue", lower the threshold.
- **Some wait times are estimates.** OverDrive usually reports one; when it
  doesn't, the wait is derived from the hold queue and copy count (assuming
  ~18-day loans) and marked with `~`.
- **No accounts.** Saved lists are protected only by an unguessable link.
- **Ebooks are supported but off.** Add `"ebook-overdrive"` to a saved list's
  formats to check both; the report keeps the better result per library.
- **It can't place holds.** That needs your card, and handing this thing your
  library credentials is not a trade worth making. Every entry links straight
  through to Libby, which is one tap.
