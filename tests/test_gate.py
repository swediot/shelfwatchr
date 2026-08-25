"""The beta gate: one shared password in front of the whole site.

Run:  python tests/test_gate.py

The password is switched on and off through the frozen settings object rather
than the environment, because config is read once at import and these tests
need both states in one process.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="shelfwatch-gate-")
os.environ.setdefault("SHELFWATCH_MOCK", "1")
os.environ.setdefault("SHELFWATCH_DB", str(Path(TMP) / "gate.db"))
os.environ.setdefault("SHELFWATCH_REFRESH", "0")

from fastapi.testclient import TestClient  # noqa: E402

from app import gate, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PASSWORD = "a password for the beta"


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok — {message}")


class gated:
    """The gate on, with a clean rate limiter and a client that has no cookie."""

    def __enter__(self):
        object.__setattr__(settings, "beta_password", PASSWORD)
        gate.gate_ip = type(gate.gate_ip)(limit=gate.gate_ip.limit, window=gate.gate_ip.window)
        self.client = TestClient(app)
        return self.client.__enter__()

    def __exit__(self, *exc):
        self.client.__exit__(*exc)
        object.__setattr__(settings, "beta_password", "")


def test_off_by_default():
    print("\nNo password set")
    check(settings.beta_password == "", "the gate is off unless a password is configured")
    with TestClient(app) as c:
        check(c.get("/").status_code == 200, "and the site is served straight away")
        check(c.get("/robots.txt").text.startswith("User-agent: *\nAllow:"),
              "with nothing hidden from crawlers")


def test_everything_is_behind_it():
    print("\nGate on")
    with gated() as c:
        r = c.get("/", follow_redirects=False)
        check(r.status_code == 303 and r.headers["location"] == "/gate?next=%2F",
              "the front page redirects to the form")
        r = c.get("/?p=somelist", follow_redirects=False)
        check("%3Fp%3Dsomelist" in r.headers["location"], "keeping where you were headed")
        check(c.get("/static/styles.css", follow_redirects=False).status_code == 303,
              "static files are behind it too")
        check(c.get("/api/libraries?q=zurich").status_code == 401,
              "and the API answers 401 rather than a page of HTML")
        check(c.get("/robots.txt").text.startswith("User-agent: *\nDisallow:"),
              "crawlers are told to stay out")


def test_health_check_stays_open():
    print("\nWhat stays reachable")
    with gated() as c:
        check(c.get("/api/health").status_code == 200,
              "the health check answers, or the host declares the machine dead")
        r = c.get("/gate")
        check(r.status_code == 401 and "<form" in r.text, "the form itself is served")


def test_password():
    print("\nGiving the password")
    with gated() as c:
        r = c.post("/api/gate", json={"password": "not it", "next": "/"})
        check(r.status_code == 401, "a wrong password is refused")
        check(c.get("/", follow_redirects=False).status_code == 303, "and lets nobody in")

        r = c.post("/api/gate", json={"password": PASSWORD, "next": "/?p=somelist"})
        check(r.status_code == 200 and r.json()["next"] == "/?p=somelist",
              "the right one comes back with where to go")
        check(c.cookies.get(gate.COOKIE), "and sets the cookie")
        check(c.get("/", follow_redirects=False).status_code == 200, "the site is now served")
        check(c.get("/api/libraries?q=zurich").status_code == 200, "including the API")
        check(c.get("/gate", follow_redirects=False).status_code == 303,
              "and the form stops asking")


def test_cookie_cannot_be_forged():
    print("\nThe cookie")
    with gated() as c:
        for forged in ("", "nonsense", "9999999999.deadbeef", "abc.def"):
            c.cookies.set(gate.COOKIE, forged)
            check(c.get("/", follow_redirects=False).status_code == 303,
                  f"{forged or '(empty)'!r} does not get in")

        expired = int(time.time()) - 10
        c.cookies.set(gate.COOKIE, f"{expired}.{gate._sign(expired)}")
        check(c.get("/", follow_redirects=False).status_code == 303,
              "a correctly signed but expired cookie does not either")

        c.cookies.set(gate.COOKIE, gate.mint())
        check(c.get("/", follow_redirects=False).status_code == 200, "a fresh one does")

    # Changing the password is how everyone gets signed out; that only works if
    # the cookie is derived from it.
    object.__setattr__(settings, "beta_password", PASSWORD)
    minted = gate.mint()
    object.__setattr__(settings, "beta_password", "something else entirely")
    check(not gate.valid(minted), "changing the password invalidates every cookie")
    object.__setattr__(settings, "beta_password", "")


def test_no_open_redirect():
    print("\nWhere ?next= may point")
    check(gate.safe_next("//evil.example") == "/", "a protocol-relative URL is refused")
    check(gate.safe_next("https://evil.example") == "/", "an absolute one too")
    check(gate.safe_next("/?p=somelist") == "/?p=somelist", "a path on this site is kept")
    with gated() as c:
        r = c.post("/api/gate", json={"password": PASSWORD, "next": "https://evil.example"})
        check(r.json()["next"] == "/", "and the server re-checks what the page sent")


def test_rate_limit():
    print("\nGuessing")
    with gated() as c:
        codes = [c.post("/api/gate", json={"password": f"guess {n}"}).status_code
                 for n in range(12)]
        check(codes[0] == 401 and codes[-1] == 429, "guesses run out after ten tries")
        check(c.post("/api/gate", json={"password": PASSWORD}).status_code == 429,
              "and the right password is refused too while the window lasts")


if __name__ == "__main__":
    store.init_db()
    for fn in [
        test_off_by_default, test_everything_is_behind_it, test_health_check_stays_open,
        test_password, test_cookie_cannot_be_forged, test_no_open_redirect, test_rate_limit,
    ]:
        fn()
    print("\nAll gate tests passed.")
