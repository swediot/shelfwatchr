"""SQLite persistence: availability cache, library directory, saved profiles.

One file, WAL mode, short-lived connections. At the traffic this thing sees
(a handful of people, a few hundred lookups a night) that is entirely enough,
and it means a backup is a single file copy.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterable, Optional

from . import libdir
from .auth import token_hash
from .config import settings
from .models import Availability, BookIn, Scope

SCHEMA = """
CREATE TABLE IF NOT EXISTS lookup_cache (
  provider    TEXT NOT NULL,
  scope_key   TEXT NOT NULL,
  book_key    TEXT NOT NULL,
  fmt         TEXT NOT NULL,
  payload     TEXT NOT NULL,
  status      TEXT NOT NULL,
  checked_at  REAL NOT NULL,
  PRIMARY KEY (provider, scope_key, book_key, fmt)
);
CREATE INDEX IF NOT EXISTS idx_cache_checked ON lookup_cache(checked_at);

CREATE TABLE IF NOT EXISTS library_dir (
  provider  TEXT NOT NULL,
  key       TEXT NOT NULL,
  name      TEXT NOT NULL,
  region    TEXT DEFAULT '',
  kind      TEXT NOT NULL DEFAULT '',   -- public | college | company | school
  places    TEXT NOT NULL DEFAULT '',   -- towns served, for the picker's search
  seen_at   REAL NOT NULL,
  PRIMARY KEY (provider, key)
);

CREATE TABLE IF NOT EXISTS profile (
  slug        TEXT PRIMARY KEY,
  name        TEXT DEFAULT '',
  scopes      TEXT NOT NULL DEFAULT '[]',
  formats     TEXT NOT NULL DEFAULT '["audiobook-overdrive"]',
  books       TEXT NOT NULL DEFAULT '[]',
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  watch_enabled   INTEGER NOT NULL DEFAULT 0,
  watch_frequency TEXT    NOT NULL DEFAULT 'daily',   -- daily | weekly
  notify_type     TEXT    NOT NULL DEFAULT 'none',    -- none | ntfy | webhook | email
  notify_target   TEXT    NOT NULL DEFAULT '',
  last_run_at     REAL    NOT NULL DEFAULT 0,
  user_id         INTEGER                        -- NULL = anonymous, reached by slug
);

-- The most recent report for a saved list, so opening the link is instant and
-- the overnight run has somewhere to put its findings.
CREATE TABLE IF NOT EXISTS profile_report (
  slug         TEXT PRIMARY KEY,
  generated_at REAL NOT NULL,
  payload      TEXT NOT NULL,
  changes      TEXT NOT NULL DEFAULT '[]'
);

-- book -> OverDrive title id. Not library-scoped: the same id works at every
-- library that carries the title, which is what lets a repeat run skip
-- searching entirely and go straight to bulk availability.
CREATE TABLE IF NOT EXISTS title_map (
  book_key    TEXT NOT NULL,
  fmt         TEXT NOT NULL,
  title_id    TEXT,              -- NULL = searched and found nothing
  matched_title  TEXT DEFAULT '',
  matched_author TEXT DEFAULT '',
  score       REAL DEFAULT 0,
  resolved_at REAL NOT NULL,
  resolver    TEXT NOT NULL DEFAULT '',   -- which resolver learned it; see RESOLVER
  PRIMARY KEY (book_key, fmt)
);

-- A lookup run. Results land row by row so the work survives a closed tab,
-- a reload, or a different device picking up the same report.
CREATE TABLE IF NOT EXISTS job (
  id          TEXT PRIMARY KEY,
  slug        TEXT NOT NULL DEFAULT '',
  scopes      TEXT NOT NULL DEFAULT '[]',
  formats     TEXT NOT NULL DEFAULT '[]',
  total       INTEGER NOT NULL DEFAULT 0,
  done        INTEGER NOT NULL DEFAULT 0,
  state       TEXT NOT NULL DEFAULT 'running',  -- running|done|failed|cancelled
  error       TEXT NOT NULL DEFAULT '',
  searches    INTEGER NOT NULL DEFAULT 0,   -- titles looked up for the first time
  bulk_calls  INTEGER NOT NULL DEFAULT 0,   -- batched availability requests
  cache_hits  INTEGER NOT NULL DEFAULT 0,   -- answers that cost no request at all
  created_at  REAL NOT NULL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_job_created ON job(created_at);

CREATE TABLE IF NOT EXISTS job_result (
  job_id   TEXT NOT NULL,
  idx      INTEGER NOT NULL,
  payload  TEXT NOT NULL,
  PRIMARY KEY (job_id, idx)
);

-- One row per (profile, book, scope) as of the last refresh, so the UI can
-- say "newly available since you last looked".
CREATE TABLE IF NOT EXISTS profile_state (
  slug       TEXT NOT NULL,
  book_key   TEXT NOT NULL,
  scope_key  TEXT NOT NULL,
  fmt        TEXT NOT NULL DEFAULT 'audiobook-overdrive',
  status     TEXT NOT NULL,
  wait_days  INTEGER,
  seen_at    REAL NOT NULL,
  PRIMARY KEY (slug, book_key, scope_key, fmt)
);

-- Accounts. Optional: a list with user_id NULL is an anonymous one reached by
-- its slug, which is how the app worked before this table existed and still
-- works now.
CREATE TABLE IF NOT EXISTS user (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  created_at    REAL NOT NULL,
  confirmed_at  REAL,
  last_login_at REAL
);
-- Case-insensitive because addresses are stored already-lowercased; this is the
-- backstop that makes a second registration of the same address impossible even
-- if some future caller forgets to normalise.
CREATE UNIQUE INDEX IF NOT EXISTS user_email_idx ON user (email COLLATE NOCASE);

-- Emailed secrets: confirm-your-address and reset-your-password. Only the hash
-- is kept, so a stolen database yields no working links.
CREATE TABLE IF NOT EXISTS auth_token (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  kind       TEXT NOT NULL,          -- confirm | reset
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  used_at    REAL
);
CREATE INDEX IF NOT EXISTS auth_token_user_idx ON auth_token (user_id, kind);

-- Login sessions. Same deal: the cookie's hash, never the cookie.
CREATE TABLE IF NOT EXISTS session (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  seen_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS session_user_idx ON session (user_id);

"""

# Indexes over columns that migrations add. These can't live in SCHEMA: it runs
# before migrate(), so on a database created by an older version the column
# isn't there yet and CREATE INDEX fails the whole script.
POST_MIGRATE = """
-- One list per account. Partial, so the many anonymous lists (user_id NULL) are
-- unconstrained while an account can only ever own one.
CREATE UNIQUE INDEX IF NOT EXISTS profile_user_idx
  ON profile (user_id) WHERE user_id IS NOT NULL;
"""

_local = threading.local()


def _connect() -> sqlite3.Connection:
    path = settings.db_path
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


@contextmanager
def db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# What resolved a learned OverDrive id. Bumping this retires every id learned
# by an older resolver: reads ignore them and the next migration deletes them.
#
# It exists because of what shipped before it. The multi-library search endpoint
# takes a format parameter and ignores it, so until that was filtered client
# side, an audiobook lookup could resolve to the ebook's id and be remembered
# under `audiobook-overdrive` — and remembered for thirty days, which is how
# long a fix would otherwise take to become visible. Code alone could not undo
# that; the wrong answers were already on disk. Anything that changes which id
# a title resolves to needs this bumped in the same commit.
RESOLVER = "fmt-filtered-v2"


# Columns added to existing tables after the first release. CREATE TABLE IF NOT
# EXISTS does nothing to a table that already exists, so without this an upgrade
# against a live database silently keeps the old shape and every query touching
# a new column raises "no such column". Adding a column? Add it here too.
ADDED_COLUMNS = {
    "profile": {
        "watch_enabled": "INTEGER NOT NULL DEFAULT 0",
        "watch_frequency": "TEXT NOT NULL DEFAULT 'daily'",
        "notify_type": "TEXT NOT NULL DEFAULT 'none'",
        "notify_target": "TEXT NOT NULL DEFAULT ''",
        "last_run_at": "REAL NOT NULL DEFAULT 0",
        "user_id": "INTEGER",
    },
    "library_dir": {
        "kind": "TEXT NOT NULL DEFAULT ''",
        "places": "TEXT NOT NULL DEFAULT ''",
    },
    "job": {
        "slug": "TEXT NOT NULL DEFAULT ''",
        "error": "TEXT NOT NULL DEFAULT ''",
        "finished_at": "REAL",
    },
    "title_map": {
        "matched_title": "TEXT DEFAULT ''",
        "matched_author": "TEXT DEFAULT ''",
        "score": "REAL DEFAULT 0",
        # Default '' on purpose: every row that predates this column was learned
        # by a resolver that could not tell an audiobook from an ebook, so the
        # migration below drops them rather than trusting them.
        "resolver": "TEXT NOT NULL DEFAULT ''",
    },
}


def migrate(conn) -> list:
    """Bring an existing database up to the current shape. Returns what changed."""
    applied = []
    for table, columns in ADDED_COLUMNS.items():
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue  # table didn't exist; the schema just created it in full
        for name, spec in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
                applied.append(f"{table}.{name}")
    return applied


def rebuild_profile_state(conn) -> bool:
    """Add `fmt` to profile_state's primary key on an existing database.

    ALTER TABLE can add a column but not widen a primary key, so this is the one
    migration that needs a table rebuild. Without it the audiobook and ebook rows
    for the same (list, book, library) overwrite each other, and every run would
    report the two formats flipping back and forth as real changes.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile_state)").fetchall()}
    if not cols or "fmt" in cols:
        return False
    conn.executescript("""
      CREATE TABLE profile_state_new (
        slug TEXT NOT NULL, book_key TEXT NOT NULL, scope_key TEXT NOT NULL,
        fmt TEXT NOT NULL DEFAULT 'audiobook-overdrive',
        status TEXT NOT NULL, wait_days INTEGER, seen_at REAL NOT NULL,
        PRIMARY KEY (slug, book_key, scope_key, fmt));
      INSERT INTO profile_state_new (slug, book_key, scope_key, fmt, status, wait_days, seen_at)
        SELECT slug, book_key, scope_key, 'audiobook-overdrive', status, wait_days, seen_at
        FROM profile_state;
      DROP TABLE profile_state;
      ALTER TABLE profile_state_new RENAME TO profile_state;
    """)
    return True


def retire_stale_title_ids(conn) -> int:
    """Delete learned ids that an older resolver produced.

    Reads already ignore them, so this is only housekeeping — but it is the
    difference between a deploy that fixes the data and a deploy that waits
    thirty days for it to expire. It runs at startup, so shipping the fix is
    all anyone has to remember to do.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(title_map)").fetchall()}
    if "resolver" not in cols:
        return 0
    cur = conn.execute("DELETE FROM title_map WHERE resolver <> ?", (RESOLVER,))
    retired = cur.rowcount or 0
    if retired:
        # Availability was computed from those ids, so it inherits their
        # doubt: a row saying the audiobook has four copies in was really
        # reading the ebook's. not_owned is cached for a week, so waiting it
        # out is not an option either. Cheap to drop, and it costs one run.
        conn.execute("DELETE FROM lookup_cache")
    return retired


def init_db() -> list:
    with db() as conn:
        conn.executescript(SCHEMA)
        applied = migrate(conn)
        if rebuild_profile_state(conn):
            applied.append("profile_state.fmt")
        conn.executescript(POST_MIGRATE)
        retired = retire_stale_title_ids(conn)
        if retired:
            applied.append(f"title_map: retired {retired} id(s) from an older resolver")
    # Not part of `applied`: the bundle is reloaded on every startup, and the
    # caller reports that as what it is rather than as a schema change.
    load_bundled_libraries()
    return applied


def reset_connection() -> None:
    """Tests point db_path somewhere else between cases."""
    directory_changed()
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ---------------------------------------------------------------- cache


def ttl_for(status: str) -> int:
    return {
        "available": settings.ttl_available,
        "holdable": settings.ttl_holdable,
        "not_owned": settings.ttl_not_owned,
    }.get(status, 0)  # unknown/error are never cached


def cache_get(provider: str, scope_key: str, book_key: str, fmt: str) -> Optional[Availability]:
    with db() as conn:
        row = conn.execute(
            "SELECT payload, status, checked_at FROM lookup_cache "
            "WHERE provider=? AND scope_key=? AND book_key=? AND fmt=?",
            (provider, scope_key, book_key, fmt),
        ).fetchone()
    if not row:
        return None
    ttl = ttl_for(row["status"])
    if not ttl or time.time() - row["checked_at"] > ttl:
        return None
    hit = Availability(**json.loads(row["payload"]))
    hit.from_cache = True
    return hit


def cache_get_stale(provider: str, scope_key: str, book_key: str, fmt: str) -> Optional[Availability]:
    """Anything we ever knew, however old — the fallback when a lookup fails."""
    with db() as conn:
        row = conn.execute(
            "SELECT payload FROM lookup_cache "
            "WHERE provider=? AND scope_key=? AND book_key=? AND fmt=?",
            (provider, scope_key, book_key, fmt),
        ).fetchone()
    if not row:
        return None
    hit = Availability(**json.loads(row["payload"]))
    hit.from_cache = True
    return hit


def cache_put(book_key: str, fmt: str, av: Availability) -> None:
    if av.status in ("unknown", "error"):
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO lookup_cache (provider, scope_key, book_key, fmt, payload, status, checked_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(provider, scope_key, book_key, fmt) DO UPDATE SET "
            "payload=excluded.payload, status=excluded.status, checked_at=excluded.checked_at",
            (av.provider, av.scope_key, book_key, fmt, av.model_dump_json(),
             av.status, av.checked_at or time.time()),
        )


def cache_count_fresh(provider: str, book_keys: list, scope_keys: list, fmts: list) -> int:
    """How many of these lookups are already answered and still fresh.

    Used to estimate how long a run will take: with a warm cache most of the
    work is already done, and quoting the cold-start number would be alarming
    and wrong.
    """
    if not book_keys or not scope_keys or not fmts:
        return 0
    now = time.time()
    windows = (
        now - settings.ttl_available,
        now - settings.ttl_holdable,
        now - settings.ttl_not_owned,
    )
    total = 0
    chunk = 300  # stay well under SQLite's variable limit
    with db() as conn:
        for start in range(0, len(book_keys), chunk):
            batch = book_keys[start:start + chunk]
            placeholders = ",".join("?" * len(batch))
            scopes_ph = ",".join("?" * len(scope_keys))
            fmts_ph = ",".join("?" * len(fmts))
            row = conn.execute(
                f"SELECT COUNT(*) c FROM lookup_cache WHERE provider=? "
                f"AND book_key IN ({placeholders}) AND scope_key IN ({scopes_ph}) "
                f"AND fmt IN ({fmts_ph}) AND ("
                "  (status='available' AND checked_at > ?)"
                "  OR (status='holdable' AND checked_at > ?)"
                "  OR (status='not_owned' AND checked_at > ?))",
                (provider, *batch, *scope_keys, *fmts, *windows),
            ).fetchone()
            total += row["c"]
    return total


def cache_stats() -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, MIN(checked_at) oldest, MAX(checked_at) newest FROM lookup_cache"
        ).fetchone()
    return {"entries": row["n"], "oldest": row["oldest"], "newest": row["newest"]}


def cache_clear() -> int:
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM lookup_cache").fetchone()["c"]
        conn.execute("DELETE FROM lookup_cache")
    return n


# ------------------------------------------------------- library directory


def remember_libraries(scopes: Iterable[Scope]) -> None:
    """Keep a library found live, so the next search answers without asking.

    `kind` is not overwritten on conflict: a live hit has no idea whether it is
    a public library or a company one, and the bundle does.
    """
    now = time.time()
    with db() as conn:
        conn.executemany(
            "INSERT INTO library_dir (provider, key, name, region, kind, seen_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(provider, key) DO UPDATE SET name=excluded.name, "
            "region=excluded.region, seen_at=excluded.seen_at",
            [(s.provider, s.key, s.name, s.region, s.kind, now) for s in scopes],
        )
    directory_changed()


def load_bundled_libraries() -> int:
    """Put the shipped directory in the table, at every startup.

    The bundle is the authority on what exists and what it is called, so this
    overwrites: a library renamed upstream is renamed here on the next deploy.
    Rows learned live that the bundle has never heard of are left alone — they
    cost nothing, and one of them may be the only way somebody reaches a library
    that joined Libby after the bundle was last built.
    """
    rows = libdir.read_bundle()
    if not rows:
        return 0
    now = time.time()
    with db() as conn:
        conn.executemany(
            "INSERT INTO library_dir (provider, key, name, region, kind, places, seen_at) "
            "VALUES ('libby',?,?,?,?,?,?) "
            "ON CONFLICT(provider, key) DO UPDATE SET name=excluded.name, "
            "region=excluded.region, kind=excluded.kind, places=excluded.places, "
            "seen_at=excluded.seen_at",
            [(key, name, region, kind, places, now)
             for key, name, region, kind, places in rows],
        )
    directory_changed()
    return len(rows)


# The search index is built from the table and kept until the table changes.
# Rebuilding it is ~20ms over 2,300 rows, and a keystroke must not pay that.
_index: Optional[list] = None
_index_lock = threading.Lock()


def directory_changed() -> None:
    global _index
    with _index_lock:
        _index = None


def _directory() -> list:
    global _index
    with _index_lock:
        if _index is None:
            with db() as conn:
                rows = conn.execute(
                    "SELECT key, name, region, kind, places FROM library_dir "
                    "WHERE provider = 'libby'").fetchall()
            _index = libdir.build([tuple(r) for r in rows])
        return _index


def library_count() -> int:
    return len(_directory())


def search_known_libraries(query: str, limit: int = 25) -> list[Scope]:
    """The picker's answer. Ranked by app/libdir.py, which explains the order."""
    return [Scope(key=e.key, name=e.name, region=e.region, kind=e.kind)
            for e in libdir.search(_directory(), query, limit)]


def library_by_key(key: str) -> Optional[Scope]:
    """One library, by its exact key — what a saved list stores."""
    with db() as conn:
        row = conn.execute(
            "SELECT key, name, region, kind FROM library_dir "
            "WHERE provider = 'libby' AND key = ?", (key,)).fetchone()
    return Scope(**dict(row)) if row else None


# ------------------------------------------------------------- profiles


def new_slug() -> str:
    alphabet = "abcdefghijkmnopqrstuvwxyz23456789"  # no l/1/0/o
    return "".join(secrets.choice(alphabet) for _ in range(8))


def profile_save(slug: Optional[str], *, name: str, scopes, formats, books,
                 watch_enabled: Optional[bool] = None, watch_frequency: Optional[str] = None,
                 notify_type: Optional[str] = None, notify_target: Optional[str] = None,
                 user_id: Optional[int] = None) -> str:
    """Create or update. Watch settings are only touched when explicitly passed,
    so saving a new book list never silently turns someone's alerts off."""
    now = time.time()
    slug = slug or new_slug()
    scopes_json = json.dumps([s.model_dump() if hasattr(s, "model_dump") else s for s in scopes])
    formats_json = json.dumps(list(formats))
    books_json = json.dumps([b.model_dump() if hasattr(b, "model_dump") else b for b in books])

    with db() as conn:
        conn.execute(
            "INSERT INTO profile (slug, name, scopes, formats, books, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET name=excluded.name, scopes=excluded.scopes, "
            "formats=excluded.formats, books=excluded.books, updated_at=excluded.updated_at",
            (slug, name, scopes_json, formats_json, books_json, now, now),
        )
        sets, args = [], []
        for column, value in (
            ("watch_enabled", None if watch_enabled is None else int(watch_enabled)),
            ("watch_frequency", watch_frequency),
            ("notify_type", notify_type),
            ("notify_target", notify_target),
        ):
            if value is not None:
                sets.append(f"{column}=?")
                args.append(value)
        if sets:
            conn.execute(f"UPDATE profile SET {', '.join(sets)} WHERE slug=?", (*args, slug))
    # Ownership is set through profile_claim, which also retires whatever list
    # the account had: an account holds one list, and that rule belongs in one
    # place rather than in every caller that happens to know a user_id.
    if user_id is not None:
        profile_claim(slug, user_id)
    return slug


def profile_mark_run(slug: str, when: Optional[float] = None) -> None:
    with db() as conn:
        conn.execute("UPDATE profile SET last_run_at=? WHERE slug=?", (when or time.time(), slug))


def profiles_due(now: Optional[float] = None) -> list[str]:
    """Watched lists whose next run is due. Weekly means 7 days, not 'Sunday'."""
    now = now or time.time()
    with db() as conn:
        rows = conn.execute(
            "SELECT slug, watch_frequency, last_run_at FROM profile WHERE watch_enabled=1"
        ).fetchall()
    due = []
    for r in rows:
        interval = 7 * 86400 if r["watch_frequency"] == "weekly" else 86400
        # a few minutes of slack so a run that starts at 04:14:59 still counts
        if now - (r["last_run_at"] or 0) >= interval - 600:
            due.append(r["slug"])
    return due


def report_put(slug: str, payload: dict, changes: list) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO profile_report (slug, generated_at, payload, changes) VALUES (?,?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET generated_at=excluded.generated_at, "
            "payload=excluded.payload, changes=excluded.changes",
            (slug, time.time(), json.dumps(payload), json.dumps(changes)),
        )


def report_get(slug: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM profile_report WHERE slug=?", (slug,)).fetchone()
    if not row:
        return None
    return {
        "generated_at": row["generated_at"],
        "payload": json.loads(row["payload"]),
        "changes": json.loads(row["changes"]),
    }


def profile_get(slug: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM profile WHERE slug=?", (slug,)).fetchone()
    if not row:
        return None
    return {
        "slug": row["slug"],
        "name": row["name"],
        "scopes": [Scope(**s) for s in json.loads(row["scopes"])],
        "formats": json.loads(row["formats"]),
        "books": [BookIn(**b) for b in json.loads(row["books"])],
        "updated_at": row["updated_at"],
        "watch_enabled": bool(row["watch_enabled"]),
        "watch_frequency": row["watch_frequency"],
        "notify_type": row["notify_type"],
        "notify_target": row["notify_target"],
        "last_run_at": row["last_run_at"],
        "user_id": row["user_id"],
    }


def profile_all_slugs() -> list[str]:
    with db() as conn:
        return [r["slug"] for r in conn.execute("SELECT slug FROM profile").fetchall()]


def profile_state_get(slug: str) -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT book_key, scope_key, fmt, status, wait_days FROM profile_state WHERE slug=?",
            (slug,),
        ).fetchall()
    return {(r["book_key"], r["scope_key"], r["fmt"]): dict(r) for r in rows}


def profile_prune_state(slug: str, keep_book_keys: set) -> int:
    """Forget books no longer on the list. Returns how many rows went."""
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT book_key FROM profile_state WHERE slug=?", (slug,)
        ).fetchall()
        stale = [r["book_key"] for r in rows if r["book_key"] not in keep_book_keys]
        for key in stale:
            conn.execute("DELETE FROM profile_state WHERE slug=? AND book_key=?", (slug, key))
    return len(stale)


def profile_state_put(slug: str, book_key: str, av: Availability) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO profile_state (slug, book_key, scope_key, fmt, status, wait_days, seen_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(slug, book_key, scope_key, fmt) DO UPDATE SET status=excluded.status, "
            "wait_days=excluded.wait_days, seen_at=excluded.seen_at",
            (slug, book_key, av.scope_key, av.fmt or "audiobook-overdrive",
             av.status, av.wait_days, time.time()),
        )


# ------------------------------------------------------------------ jobs


def job_create(books, scopes, formats, slug: str = "") -> str:
    job_id = new_slug() + new_slug()[:4]
    with db() as conn:
        conn.execute(
            "INSERT INTO job (id, slug, scopes, formats, total, created_at) VALUES (?,?,?,?,?,?)",
            (job_id, slug,
             json.dumps([s.model_dump() if hasattr(s, "model_dump") else s for s in scopes]),
             json.dumps(list(formats)), len(books), time.time()),
        )
    return job_id


def job_get(job_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def job_add_results(job_id: str, items) -> None:
    """Write a chunk's results in one transaction.

    Was one INSERT plus one COUNT(*) per book, which made progress bookkeeping
    O(n²) over a run and blocked the event loop for a whole chunk at a time.
    Now: one executemany, one COUNT.
    """
    rows = [
        (job_id, idx,
         result.model_dump_json() if hasattr(result, "model_dump_json") else json.dumps(result))
        for idx, result in items
    ]
    if not rows:
        return
    with db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO job_result (job_id, idx, payload) VALUES (?,?,?)", rows
        )
        conn.execute(
            "UPDATE job SET done=(SELECT COUNT(*) FROM job_result WHERE job_id=?) WHERE id=?",
            (job_id, job_id),
        )


def job_add_result(job_id: str, idx: int, result) -> None:
    """Single-result form, kept for tests and one-off writes."""
    job_add_results(job_id, [(idx, result)])


def job_results(job_id: str, after: int = 0, limit: int = 500) -> list:
    """Results in the order they were *produced*, not the order they'll be shown.

    Those differ: a run answers the books it already knows about first, so index
    900 can land before index 3. Streaming therefore pages by an insertion
    sequence (the rowid) rather than by the display index — paging by index
    would silently skip anything that arrived out of order.
    """
    with db() as conn:
        return conn.execute(
            "SELECT rowid AS seq, idx, payload FROM job_result "
            "WHERE job_id=? AND rowid>? ORDER BY rowid LIMIT ?",
            (job_id, after, limit),
        ).fetchall()


def job_add_stats(job_id: str, stats: dict) -> None:
    """Running totals of what the work actually consisted of."""
    if not stats:
        return
    with db() as conn:
        conn.execute(
            "UPDATE job SET searches=searches+?, bulk_calls=bulk_calls+?, cache_hits=cache_hits+? "
            "WHERE id=?",
            (stats.get("searches", 0), stats.get("bulk", 0), stats.get("cached", 0), job_id),
        )


def job_finish(job_id: str, state: str, error: str = "") -> None:
    with db() as conn:
        conn.execute(
            "UPDATE job SET state=?, error=?, finished_at=? WHERE id=?",
            (state, error, time.time(), job_id),
        )


def job_cancel(job_id: str) -> None:
    with db() as conn:
        conn.execute("UPDATE job SET state='cancelled' WHERE id=? AND state='running'", (job_id,))


def job_is_cancelled(job_id: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT state FROM job WHERE id=?", (job_id,)).fetchone()
    return bool(row) and row["state"] == "cancelled"


def job_orphans() -> list[str]:
    """Jobs still marked running — only possible if the server stopped mid-run."""
    with db() as conn:
        rows = conn.execute("SELECT id FROM job WHERE state='running'").fetchall()
    return [r["id"] for r in rows]


def job_prune(max_age_seconds: int) -> int:
    """Jobs are scratch space; the saved report is the durable artefact."""
    cutoff = time.time() - max_age_seconds
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM job WHERE created_at < ? AND state != 'running'", (cutoff,)
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM job_result WHERE job_id=?", (r["id"],))
            conn.execute("DELETE FROM job WHERE id=?", (r["id"],))
    return len(rows)


# ------------------------------------------------------------ title map


def title_map_get(book_key: str, fmt: str, max_age: float) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM title_map WHERE book_key=? AND fmt=? AND resolver=?",
            (book_key, fmt, RESOLVER),
        ).fetchone()
    if not row:
        return None
    if time.time() - row["resolved_at"] > max_age:
        return None
    return dict(row)


def title_map_get_many(book_keys: list, fmt: str, max_age: float) -> dict:
    if not book_keys:
        return {}
    cutoff = time.time() - max_age
    out = {}
    chunk = 300
    with db() as conn:
        for start in range(0, len(book_keys), chunk):
            batch = book_keys[start:start + chunk]
            ph = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT * FROM title_map WHERE fmt=? AND resolved_at>? AND resolver=? "
                f"AND book_key IN ({ph})",
                (fmt, cutoff, RESOLVER, *batch),
            ).fetchall()
            for r in rows:
                out[r["book_key"]] = dict(r)
    return out


def title_map_put(book_key: str, fmt: str, title_id, matched_title="", matched_author="", score=0.0) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO title_map (book_key, fmt, title_id, matched_title, matched_author, "
            "score, resolved_at, resolver) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(book_key, fmt) DO UPDATE SET title_id=excluded.title_id, "
            "matched_title=excluded.matched_title, matched_author=excluded.matched_author, "
            "score=excluded.score, resolved_at=excluded.resolved_at, resolver=excluded.resolver",
            (book_key, fmt, title_id, matched_title, matched_author, score, time.time(), RESOLVER),
        )


def title_map_clear() -> int:
    """Forget every learned OverDrive id. Ops escape hatch, and what makes a
    test able to say 'cold' and mean it."""
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM title_map").fetchone()["c"]
        conn.execute("DELETE FROM title_map")
    return n


def title_map_stats() -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN title_id IS NULL THEN 1 ELSE 0 END) misses FROM title_map"
        ).fetchone()
    return {"resolved": row["n"], "no_match": row["misses"] or 0}


# --------------------------------------------------------------- accounts

def user_create(email: str, password_hash: str) -> Optional[int]:
    """Returns the new id, or None if that address is already registered.

    None rather than an exception because the caller's answer to "already
    registered" is the same page as "registered": telling a stranger which
    addresses have accounts here is a privacy leak, not an error condition.
    """
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO user (email, password_hash, created_at) VALUES (?,?,?)",
                (email, password_hash, time.time()),
            )
        except sqlite3.IntegrityError:
            return None
        return int(cur.lastrowid)


def _user_row(row) -> Optional[dict]:
    if not row:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "password_hash": row["password_hash"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "last_login_at": row["last_login_at"],
        "confirmed": row["confirmed_at"] is not None,
    }


def user_by_email(email: str) -> Optional[dict]:
    with db() as conn:
        return _user_row(conn.execute(
            "SELECT * FROM user WHERE email=? COLLATE NOCASE", (email,)).fetchone())


def user_by_id(user_id: int) -> Optional[dict]:
    with db() as conn:
        return _user_row(conn.execute("SELECT * FROM user WHERE id=?", (user_id,)).fetchone())


def user_confirm(user_id: int) -> None:
    """Idempotent: clicking the link twice is a normal thing people do."""
    with db() as conn:
        conn.execute("UPDATE user SET confirmed_at=COALESCE(confirmed_at, ?) WHERE id=?",
                     (time.time(), user_id))


def user_set_password(user_id: int, password_hash: str) -> None:
    with db() as conn:
        conn.execute("UPDATE user SET password_hash=? WHERE id=?", (password_hash, user_id))


def user_touch_login(user_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE user SET last_login_at=? WHERE id=?", (time.time(), user_id))


def user_delete(user_id: int) -> None:
    """Everything: sessions, tokens, the list and its report and history.

    Deliberately not a soft delete. "Delete my account" from someone who trusted
    a hobby server with their email should mean the row is gone.
    """
    with db() as conn:
        slugs = [r["slug"] for r in conn.execute(
            "SELECT slug FROM profile WHERE user_id=?", (user_id,)).fetchall()]
        for slug in slugs:
            conn.execute("DELETE FROM profile_report WHERE slug=?", (slug,))
            conn.execute("DELETE FROM profile_state WHERE slug=?", (slug,))
            conn.execute("DELETE FROM profile WHERE slug=?", (slug,))
        conn.execute("DELETE FROM session WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM auth_token WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user WHERE id=?", (user_id,))


# ----------------------------------------------------------- email tokens

def token_create(user_id: int, kind: str, ttl: float, raw: str) -> None:
    """Stores the hash of a token the caller generated and will email.

    The raw value comes in rather than out so this module never holds a
    sendable secret it might log.
    """
    now = time.time()
    with db() as conn:
        # One live link per purpose: asking for a new reset email should retire
        # the old one, or a leaked inbox stays useful for hours.
        conn.execute("UPDATE auth_token SET used_at=? WHERE user_id=? AND kind=? AND used_at IS NULL",
                     (now, user_id, kind))
        conn.execute(
            "INSERT OR REPLACE INTO auth_token "
            "(token_hash, user_id, kind, created_at, expires_at) VALUES (?,?,?,?,?)",
            (token_hash(raw), user_id, kind, now, now + ttl),
        )


def token_consume(raw: str, kind: str) -> Optional[int]:
    """The user_id if this token is live, else None. Single use, always."""
    now = time.time()
    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM auth_token WHERE token_hash=? AND kind=? "
            "AND used_at IS NULL AND expires_at > ?",
            (token_hash(raw), kind, now),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE auth_token SET used_at=? WHERE token_hash=?", (now, token_hash(raw)))
    return int(row["user_id"])


def token_prune(now: Optional[float] = None) -> int:
    """Drop spent and expired tokens. Housekeeping, not security."""
    now = now or time.time()
    with db() as conn:
        cur = conn.execute("DELETE FROM auth_token WHERE expires_at < ? OR used_at IS NOT NULL",
                           (now - 86400,))
        return cur.rowcount


# --------------------------------------------------------------- sessions

def session_create(user_id: int, raw: str, ttl: float) -> None:
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO session (token_hash, user_id, created_at, expires_at, seen_at) "
            "VALUES (?,?,?,?,?)",
            (token_hash(raw), user_id, now, now + ttl, now),
        )


def session_user(raw: str, *, extend: float = 0) -> Optional[dict]:
    """The signed-in user for a cookie, or None.

    `extend` slides the expiry forward, so someone who uses the app every week
    is never logged out; a cookie left alone for its full life still dies.
    """
    if not raw:
        return None
    now = time.time()
    key = token_hash(raw)
    with db() as conn:
        row = conn.execute(
            "SELECT u.* FROM session s JOIN user u ON u.id = s.user_id "
            "WHERE s.token_hash=? AND s.expires_at > ?", (key, now)).fetchone()
        if not row:
            return None
        # Once a day is plenty; a write on every request would serialise the
        # whole app behind SQLite's writer lock for no benefit.
        if extend:
            conn.execute(
                "UPDATE session SET seen_at=?, expires_at=? WHERE token_hash=? AND seen_at < ?",
                (now, now + extend, key, now - 86400))
    return _user_row(row)


def session_delete(raw: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM session WHERE token_hash=?", (token_hash(raw),))


def session_delete_all(user_id: int) -> int:
    """Every device. What a password change does, and what a 'sign out
    everywhere' button would call."""
    with db() as conn:
        return conn.execute("DELETE FROM session WHERE user_id=?", (user_id,)).rowcount


def session_prune(now: Optional[float] = None) -> int:
    with db() as conn:
        return conn.execute("DELETE FROM session WHERE expires_at < ?",
                            (now or time.time(),)).rowcount


# -------------------------------------------------- the account's one list

def profile_for_user(user_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT slug FROM profile WHERE user_id=?", (user_id,)).fetchone()
    return profile_get(row["slug"]) if row else None


def profile_owner(slug: str) -> Optional[int]:
    with db() as conn:
        row = conn.execute("SELECT user_id FROM profile WHERE slug=?", (slug,)).fetchone()
    return row["user_id"] if row and row["user_id"] is not None else None


def profile_claim(slug: str, user_id: int) -> str:
    """Attach a list to an account, replacing whatever that account had.

    The old list is deleted rather than orphaned: an account holds one list, and
    leaving the previous one lying around slug-accessible would mean "I replaced
    my list" quietly left the old books readable to anyone who kept the link.
    """
    with db() as conn:
        for row in conn.execute("SELECT slug FROM profile WHERE user_id=? AND slug<>?",
                                (user_id, slug)).fetchall():
            old = row["slug"]
            conn.execute("DELETE FROM profile_report WHERE slug=?", (old,))
            conn.execute("DELETE FROM profile_state WHERE slug=?", (old,))
            conn.execute("DELETE FROM profile WHERE slug=?", (old,))
        conn.execute("UPDATE profile SET user_id=? WHERE slug=?", (user_id, slug))
    return slug
