"""An upgrade must throw away ids the old resolver learned.

The format bug didn't only produce wrong answers, it wrote them down: an
audiobook lookup could resolve to the ebook's id and be remembered under
`audiobook-overdrive` for thirty days. Shipping the fix could not undo that on
its own, so a deploy has to retire those rows. This builds a database in the
old shape — no `resolver` column at all — and checks that starting up against
it does.

Run: python tests/test_resolver_retire.py
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = Path(tempfile.mkdtemp(prefix="retire-")) / "r.db"
os.environ.update(SHELFWATCHR_DB=str(DB), SHELFWATCHR_REFRESH="0")

from app import store  # noqa: E402

failures = []


def check(ok, label):
    print(f"  {'ok' if ok else 'FAIL'} - {label}")
    if not ok:
        failures.append(label)


# A pre-fix database: title_map exactly as it was, with no resolver column.
conn = sqlite3.connect(DB)
conn.executescript("""
  CREATE TABLE title_map (
    book_key TEXT NOT NULL, fmt TEXT NOT NULL, title_id TEXT,
    matched_title TEXT DEFAULT '', matched_author TEXT DEFAULT '',
    score REAL DEFAULT 0, resolved_at REAL NOT NULL,
    PRIMARY KEY (book_key, fmt));
""")
# The exact shape of the bug: one book, both formats, the same ebook id.
conn.executemany(
    "INSERT INTO title_map (book_key, fmt, title_id, matched_title, resolved_at) "
    "VALUES (?,?,?,?,?)",
    [("everything i never told you|ng", "audiobook-overdrive", "1438572",
      "Everything I Never Told You", 9e9),
     ("everything i never told you|ng", "ebook-overdrive", "1438572",
      "Everything I Never Told You", 9e9)],
)
conn.commit()
conn.close()

print("\nUpgrading a database written before the fix")
applied = store.init_db()
check(any("resolver" in a for a in applied),
      f"the migration adds title_map.resolver ({applied})")
check(any("retired" in a for a in applied),
      f"and says how many ids it retired ({applied})")

with store.db() as c:
    left = c.execute("SELECT COUNT(*) n FROM title_map").fetchone()["n"]
check(left == 0, f"the ids learned by the old resolver are gone ({left} left)")

print("\nAnd a fresh id is trusted again")
store.title_map_put("everything i never told you|ng", "audiobook-overdrive",
                    "1580098", "Everything I Never Told You", "Celeste Ng", 1.0)
got = store.title_map_get_many(["everything i never told you|ng"],
                               "audiobook-overdrive", 30 * 86400)
check(got.get("everything i never told you|ng", {}).get("title_id") == "1580098",
      "a newly learned id reads back")

# Belt and braces: a row stamped by some future resolver must not be read even
# if retirement never ran, or the read path is trusting the wrong thing.
with store.db() as c:
    c.execute("UPDATE title_map SET resolver='something-else'")
got = store.title_map_get_many(["everything i never told you|ng"],
                               "audiobook-overdrive", 30 * 86400)
check(got == {}, "a row from a different resolver is ignored on read")
check(store.title_map_get("everything i never told you|ng",
                          "audiobook-overdrive", 30 * 86400) is None,
      "including by the single-row getter")

print()
if failures:
    print(f"{len(failures)} failure(s).")
    sys.exit(1)
print("Resolver retirement tests passed.")
