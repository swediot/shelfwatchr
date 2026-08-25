"""Generate a large Goodreads-shaped export, for testing that big lists behave."""
import csv
import random
import sys

random.seed(7)
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/big.csv"

REAL = [
    ("The Fifth Season", "N.K. Jemisin"),
    ("Piranesi", "Susanna Clarke"),
    ("Babel, or the Necessity of Violence", "R.F. Kuang"),
    ("Der Schwarm", "Frank Schätzing"),
]
WORDS = ["Winter", "Ash", "Salt", "Hollow", "Ember", "Quiet", "Iron", "River", "Glass",
         "Tide", "Moth", "Lantern", "Cinder", "Harbour", "Thorn", "Pale", "Amber",
         "Frost", "Wren", "Marrow"]

rows = []
used = set()
for i in range(COUNT):
    if i < len(REAL):
        title, author = REAL[i]
    else:
        # Titles must be unique: tests look books up by title, and two books
        # sharing one makes every assertion about the pair meaningless.
        author = f"{random.choice(WORDS)} {random.choice(WORDS)}son"
        while True:
            title = f"The {random.choice(WORDS)} {random.choice(WORDS)}"
            if len(used) >= len(WORDS) ** 2:
                title += f" of {random.choice(WORDS)}"
            if title not in used:
                break
        if i % 97 == 0:
            title += f" {i}"
    used.add(title)
    # Dates and page counts vary, so a test can tell a working sort from one
    # that leaves the list alone.
    day = 1 + (i * 7) % 300
    added = f"2025/{1 + (day - 1) // 28:02d}/{1 + (day - 1) % 28:02d}"
    rows.append({
        "Book Id": i, "Title": title, "Author": author, "Additional Authors": "",
        "ISBN": "", "ISBN13": "", "Exclusive Shelf": "to-read", "Date Added": added,
        "Number of Pages": 120 + (i * 37) % 700,
    })

with open(OUT, "w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(f"wrote {COUNT} rows to {OUT}")
