"""Reading-list CSV -> books.

Handles both StoryGraph and Goodreads exports, and tries not to be brittle about
it: columns are found by fuzzy name rather than position, so a renamed or
reordered export still imports. Both services happen to spell the shelf we want
"to-read", which is the one convenient thing about this.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from .matching import Book
from .models import BookIn

DEFAULT_STATUSES = ("to-read",)

# Goodreads writes ISBNs as ="9780441013593" so spreadsheets don't eat the
# leading zeros.
EXCEL_GUARD = re.compile(r'^="?(.*?)"?$')


def find_col(fieldnames, *candidates):
    lowered = {(f or "").strip().lower(): f for f in fieldnames or []}
    for cand in candidates:  # exact first
        if cand in lowered:
            return lowered[cand]
    for cand in candidates:  # then substring
        for low, orig in lowered.items():
            if cand in low:
                return orig
    return None


def clean(value: str) -> str:
    v = (value or "").strip()
    m = EXCEL_GUARD.match(v)
    return (m.group(1) if m else v).strip()


def split_authors(raw: str) -> list[str]:
    return [a.strip() for a in re.split(r"[;,]| and ", raw or "") if a.strip()]


def parse_date(value: str) -> str:
    """Both exports write dates their own way; normalise to ISO so they sort.

    StoryGraph: 2026/01/12 · Goodreads: 2026/01/12 · some locales: 12/01/2026.
    An unparseable date is dropped rather than guessed at — sorting by "added"
    simply puts those last.
    """
    v = (value or "").strip()
    if not v:
        return ""
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_int(value: str):
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def parse_owned(value: str) -> bool:
    """StoryGraph writes Yes/No; Goodreads writes a copy count."""
    v = (value or "").strip().lower()
    if v in ("yes", "y", "true", "owned"):
        return True
    n = parse_int(v)
    return bool(n and n > 0)


def detect_source(fields) -> str:
    lowered = {(f or "").strip().lower() for f in fields or []}
    if "exclusive shelf" in lowered or "book id" in lowered:
        return "goodreads"
    if "read status" in lowered or "moods" in lowered:
        return "storygraph"
    return "unknown"


def normalise_status(value: str) -> str:
    s = (value or "").strip().lower().replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    return {
        "to read": "to-read",
        "want to read": "to-read",
        "currently reading": "currently-reading",
        "did not finish": "did-not-finish",
        "dnf": "did-not-finish",
    }.get(s, s.replace(" ", "-"))


def parse_reading_list(
    data: bytes, statuses=DEFAULT_STATUSES, exclude_owned: bool = False,
) -> tuple[list[BookIn], dict]:
    """Return (books, report). The report explains what was dropped and why.

    exclude_owned drops rows the export marks as owned: a book on your shelf
    is not one you need the library for.
    """
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    source = detect_source(fields)

    c_title = find_col(fields, "title")
    c_auth = find_col(fields, "authors", "author")
    c_extra = find_col(fields, "additional authors", "contributors")
    c_status = find_col(fields, "read status", "exclusive shelf", "status")
    c_isbn = find_col(fields, "isbn13", "isbn/uid", "isbn")
    c_added = find_col(fields, "date added", "added")
    c_pages = find_col(fields, "number of pages", "pages")
    c_owned = find_col(fields, "owned?", "owned copies", "owned")

    report = {
        "source": source,
        "has_dates": bool(c_added),
        "has_pages": bool(c_pages),
        "has_owned": bool(c_owned),
        "exclude_owned": exclude_owned,
        "owned": 0,
        "columns_seen": fields,
        "status_column": c_status,
        "statuses_wanted": list(statuses),
        "statuses_found": {},
        "rows": 0,
        "imported": 0,
        "skipped_status": 0,
        "skipped_owned": 0,
        "skipped_duplicate": 0,
        "skipped_untitled": 0,
    }
    if not c_title:
        raise ValueError(
            "No Title column in that file. Columns found: " + ", ".join(fields[:12])
            + ". Expected a StoryGraph or Goodreads CSV export."
        )

    wanted = {normalise_status(s) for s in statuses if s}
    books: list[BookIn] = []
    seen: set[str] = set()

    for row in reader:
        report["rows"] += 1
        title = clean(row.get(c_title, ""))
        if not title:
            report["skipped_untitled"] += 1
            continue

        if c_status:
            status = normalise_status(row.get(c_status, ""))
            if status:
                report["statuses_found"][status] = report["statuses_found"].get(status, 0) + 1
            if wanted and status and status not in wanted:
                report["skipped_status"] += 1
                continue

        # Counted after the shelf filter, so the number offered in the UI is
        # "owned books on this shelf", which is what the switch would remove.
        if c_owned and parse_owned(row.get(c_owned, "")):
            report["owned"] += 1
            if exclude_owned:
                report["skipped_owned"] += 1
                continue

        authors = split_authors(clean(row.get(c_auth, "")) if c_auth else "")
        if c_extra:
            authors += split_authors(clean(row.get(c_extra, "")))
        # de-dupe while keeping order
        authors = list(dict.fromkeys(a for a in authors if a))

        book = Book(title=title, authors=tuple(authors))
        if book.key in seen:
            report["skipped_duplicate"] += 1
            continue
        seen.add(book.key)
        books.append(
            BookIn(
                title=title,
                author=", ".join(authors),
                isbn=clean(row.get(c_isbn, "")) if c_isbn else "",
                added=parse_date(clean(row.get(c_added, ""))) if c_added else "",
                pages=parse_int(clean(row.get(c_pages, ""))) if c_pages else None,
            )
        )

    report["imported"] = len(books)
    return books, report


# Older name, kept so nothing that imported it breaks.
parse_storygraph = parse_reading_list
