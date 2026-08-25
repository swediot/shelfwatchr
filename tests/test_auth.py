"""Accounts: signing up, confirming, signing in, resetting, and who owns a list.

Run:  python tests/test_auth.py

Deliberately slow in one place: password hashing is meant to cost ~100ms, and
turning that down for the tests would mean not testing the thing that ships.
The suite keeps the number of hashes small instead.
"""

import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="shelfwatch-auth-")
os.environ.update(
    SHELFWATCH_MOCK="1",
    SHELFWATCH_DB=str(Path(TMP) / "auth.db"),
    SHELFWATCH_REFRESH="0",
    SHELFWATCH_RPM="6000",
    SHELFWATCH_ACCOUNTS="1",
    SHELFWATCH_SIGNUPS="1",
)

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, notify, store  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
client.__enter__()

PASSWORD = "correct horse battery"
sent: list[tuple[str, str, str]] = []   # (kind, address, link)


def capture_mail():
    """Intercept account mail and keep the link, which is what tests need.

    Patching the module attribute rather than SMTP itself: the point is to read
    the link the app generated, not to prove smtplib works.
    """
    async def fake(kind, address, link):
        sent.append((kind, address, link))
        return {"ok": True, "delivered": "test"}
    notify.send_account_mail = fake
    import app.accounts as accounts_mod
    accounts_mod.notify.send_account_mail = fake


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok — {message}")


def token_from(link: str) -> str:
    m = re.search(r"token=([^&]+)", link)
    assert m, f"no token in {link}"
    return m.group(1)


def fresh_client() -> TestClient:
    """A separate cookie jar — a second browser, or a signed-out one."""
    c = TestClient(app)
    c.__enter__()
    return c


def clear_limits():
    for limiter in (auth.login_ip, auth.login_email, auth.signup_ip, auth.reset_email):
        limiter._hits.clear()


# --------------------------------------------------------------- primitives

def test_password_hashing():
    print("\nPassword hashing")
    h = auth.hash_password(PASSWORD)
    check(PASSWORD not in h, "the password itself isn't in the stored string")
    check(h.startswith("scrypt$"), f"it records the scheme and cost: {h[:20]}…")
    check(auth.verify_password(PASSWORD, h), "the right password verifies")
    check(not auth.verify_password(PASSWORD + "!", h), "a wrong one doesn't")
    check(auth.hash_password(PASSWORD) != h, "two hashes of one password differ (salted)")
    for junk in ("", "notahash", "scrypt$x$y$z$q$r", None):
        check(not auth.verify_password(PASSWORD, junk), f"a corrupt hash is False, not a crash: {junk!r}")
    check(not auth.needs_rehash(h), "a current hash doesn't need upgrading")
    check(auth.needs_rehash("scrypt$1024$8$1$aa$bb"), "one at an older cost does")


def test_email_normalising():
    print("\nEmail handling")
    check(auth.normalize_email("  Bob@Example.COM ") == "bob@example.com",
          "addresses are trimmed and lowercased, so one person is one account")
    for good in ("a@b.co", "first.last+tag@mail.example.org"):
        check(auth.valid_email(good), f"accepts {good}")
    for bad in ("", "nope", "a@b", "a b@c.com", "a@@b.com", "x@" + "y" * 260 + ".com"):
        check(not auth.valid_email(bad), f"rejects {bad!r}")


def test_password_rules():
    print("\nPassword rules")
    check(auth.password_problem("short") , "too-short passwords are refused")
    check(not auth.password_problem("a" * 10), "ten characters is enough")
    check(auth.password_problem("a" * 5000), "an absurdly long one is refused before hashing")
    check(not auth.password_problem("this is four words"),
          "a passphrase with no digit or symbol is fine — length is the rule")


# -------------------------------------------------------------- the journey

def test_register_and_confirm():
    print("\nSigning up")
    sent.clear()
    r = client.post("/api/auth/register", json={"email": "reader@example.com", "password": PASSWORD})
    check(r.status_code == 200, "registering is accepted")
    check(len(sent) == 1 and sent[0][0] == "confirm", "and sends a confirmation mail")

    user = store.user_by_email("reader@example.com")
    check(user and not user["confirmed"], "the account exists but isn't confirmed yet")

    r = client.post("/api/auth/login", json={"email": "reader@example.com", "password": PASSWORD})
    check(r.status_code == 403, "signing in before confirming is refused")

    link = sent[0][2]
    token = token_from(link)
    r = client.get(f"/api/auth/confirm?token={token}", follow_redirects=False)
    check(r.status_code == 303, "the link redirects rather than rendering")
    check("confirm=ok" in r.headers["location"], f"back to the app, saying so: {r.headers['location']}")
    check("sw_session" in r.cookies or "sw_session" in client.cookies,
          "and signs you in on the spot")
    check(store.user_by_email("reader@example.com")["confirmed"], "the account is now confirmed")

    r = client.get(f"/api/auth/confirm?token={token}", follow_redirects=False)
    check("confirm=expired" in r.headers["location"], "the same link a second time is spent")

    me = client.get("/api/auth/me").json()
    check(me["user"] and me["user"]["email"] == "reader@example.com", "and /me knows who you are")
    check("password_hash" not in str(me), "which never includes the password hash")


def test_registration_leaks_nothing():
    print("\nRegistering an address that already exists")
    sent.clear()
    clear_limits()
    other = fresh_client()
    first = other.post("/api/auth/register",
                       json={"email": "taken@example.com", "password": PASSWORD})
    sent.clear()
    again = other.post("/api/auth/register",
                       json={"email": "taken@example.com", "password": "a different one"})
    check(first.status_code == again.status_code == 200, "both attempts get the same status")
    check(first.json() == again.json(), f"and the same body: {again.json()['detail']!r}")
    check(store.user_by_email("taken@example.com"), "the first account still exists")
    check(auth.verify_password(PASSWORD, store.user_by_email("taken@example.com")["password_hash"]),
          "with its original password — the second attempt didn't overwrite it")
    check(len(sent) == 1 and sent[0][0] == "confirm",
          "the mail goes to the address's owner, not the person who typed it")
    other.__exit__(None, None, None)


def test_login_and_session():
    print("\nSigning in")
    clear_limits()
    browser = fresh_client()
    check(browser.get("/api/auth/me").json()["user"] is None, "a new browser is signed out")

    r = browser.post("/api/auth/login", json={"email": "reader@example.com", "password": "wrong wrong wrong"})
    check(r.status_code == 401, "a wrong password is refused")
    check("password" in r.json()["detail"].lower(),
          f"without saying which half was wrong: {r.json()['detail']!r}")

    unknown = browser.post("/api/auth/login",
                           json={"email": "nobody@example.com", "password": "wrong wrong wrong"})
    check(unknown.json()["detail"] == r.json()["detail"],
          "an unknown address gets the identical message")

    clear_limits()
    r = browser.post("/api/auth/login", json={"email": "READER@example.com", "password": PASSWORD})
    check(r.status_code == 200, "the right password works, whatever the capitalisation")
    cookie = browser.cookies.get("sw_session")
    check(cookie, "a session cookie comes back")
    check(cookie not in str(store.session_user(cookie)), "the raw cookie isn't what's stored")
    with store.db() as conn:
        rows = conn.execute("SELECT token_hash FROM session").fetchall()
    check(all(cookie != row["token_hash"] for row in rows),
          "the database holds only its hash, so a dump grants nobody a session")

    check(browser.get("/api/auth/me").json()["user"]["email"] == "reader@example.com",
          "the cookie identifies you on the next request")
    browser.post("/api/auth/logout")
    check(browser.get("/api/auth/me").json()["user"] is None, "signing out ends it")
    check(store.session_user(cookie) is None, "server-side too — not just the cookie being dropped")
    browser.__exit__(None, None, None)


def test_login_rate_limit():
    print("\nGuessing passwords")
    clear_limits()
    browser = fresh_client()
    codes = []
    for _ in range(12):
        codes.append(browser.post("/api/auth/login",
                     json={"email": "reader@example.com", "password": "nope nope nope"}).status_code)
    check(401 in codes, "wrong guesses are refused one by one")
    check(429 in codes, f"and then start being throttled: {codes}")
    check(codes.index(429) >= 5, f"but not so eagerly that a typo locks you out: {codes.index(429)} allowed")

    # The right password must still be refused while the throttle holds, or the
    # limit is decoration.
    r = browser.post("/api/auth/login", json={"email": "reader@example.com", "password": PASSWORD})
    check(r.status_code == 429, "even the correct password waits out the lockout")
    clear_limits()
    r = browser.post("/api/auth/login", json={"email": "reader@example.com", "password": PASSWORD})
    check(r.status_code == 200, "and works again once the window passes")
    browser.__exit__(None, None, None)


def test_forgot_and_reset():
    global PASSWORD
    print("\nForgotten password")
    sent.clear()
    clear_limits()
    browser = fresh_client()

    known = browser.post("/api/auth/forgot", json={"email": "reader@example.com"})
    unknown = browser.post("/api/auth/forgot", json={"email": "ghost@example.com"})
    check(known.status_code == unknown.status_code == 200, "both addresses get the same status")
    check(known.json() == unknown.json(), "and the same words — no account enumeration")
    check(len(sent) == 1, "only the real one produces mail")

    token = token_from(sent[0][2])
    check("/reset?token=" in sent[0][2], f"the link points at the reset page: {sent[0][2]}")

    r = browser.post("/api/auth/reset", json={"token": token, "password": "short"})
    check(r.status_code == 400, "a weak new password is refused")

    # The old session, on another device, has to die when the password changes.
    victim = fresh_client()
    clear_limits()
    victim.post("/api/auth/login", json={"email": "reader@example.com", "password": PASSWORD})
    check(victim.get("/api/auth/me").json()["user"], "another device is signed in beforehand")

    new_password = "a whole new phrase"
    r = browser.post("/api/auth/reset", json={"token": token, "password": new_password})
    check(r.status_code == 200, "a good one is accepted")
    check(browser.cookies.get("sw_session"), "and signs you in immediately")
    check(victim.get("/api/auth/me").json()["user"] is None,
          "while every other session is ended — the point of a reset")

    r = browser.post("/api/auth/reset", json={"token": token, "password": "another phrase here"})
    check(r.status_code == 400, "the reset link is single use")

    clear_limits()
    check(fresh_client().post("/api/auth/login",
          json={"email": "reader@example.com", "password": PASSWORD}).status_code == 401,
          "the old password no longer works")
    clear_limits()
    check(fresh_client().post("/api/auth/login",
          json={"email": "reader@example.com", "password": new_password}).status_code == 200,
          "the new one does")

    PASSWORD = new_password
    for c in (browser, victim):
        c.__exit__(None, None, None)


def test_expired_token():
    print("\nExpiry")
    user = store.user_by_email("reader@example.com")
    raw = auth.new_token()
    store.token_create(user["id"], "reset", -1, raw)      # already expired
    check(store.token_consume(raw, "reset") is None, "an expired token is refused")

    raw = auth.new_token()
    store.token_create(user["id"], "reset", 3600, raw)
    check(store.token_consume(raw, "confirm") is None,
          "a reset token can't be spent as a confirmation")
    check(store.token_consume(raw, "reset") == user["id"], "but works for what it's for")

    first = auth.new_token()
    second = auth.new_token()
    store.token_create(user["id"], "reset", 3600, first)
    store.token_create(user["id"], "reset", 3600, second)
    check(store.token_consume(first, "reset") is None,
          "asking for a second reset link retires the first")
    check(store.token_consume(second, "reset") == user["id"], "and the newest one works")


def test_session_expiry():
    print("\nSessions expire")
    user = store.user_by_email("reader@example.com")
    raw = auth.new_token()
    store.session_create(user["id"], raw, -1)
    check(store.session_user(raw) is None, "an expired session cookie identifies nobody")
    check(store.session_prune() >= 1, "and gets cleaned up")


# ------------------------------------------------------------ list and auth

def signed_in_client() -> TestClient:
    clear_limits()
    c = fresh_client()
    r = c.post("/api/auth/login", json={"email": "reader@example.com", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return c


BOOKS = [{"title": "Piranesi", "author": "Susanna Clarke"},
         {"title": "The Fifth Season", "author": "N.K. Jemisin"}]
SCOPES = [{"provider": "libby", "key": "queenslibrary", "name": "Queens Public Library"}]


def test_account_list():
    print("\nThe account's list")
    me = signed_in_client()
    check(me.get("/api/me/list").json()["list"] is None, "a new account has no list")

    r = me.post("/api/me/list", json={"name": "My audiobooks", "scopes": SCOPES,
                                      "formats": ["audiobook-overdrive"], "books": BOOKS})
    check(r.status_code == 200, "saving one works")
    slug = r.json()["slug"]

    got = me.get("/api/me/list").json()["list"]
    check(got["slug"] == slug and got["books"] == 2, f"and it comes back: {got}")

    # Saving again must reuse the slug, or every re-upload orphans the old link.
    again = me.post("/api/me/list", json={"name": "My audiobooks", "scopes": SCOPES,
                                          "formats": ["audiobook-overdrive"],
                                          "books": BOOKS[:1]}).json()
    check(again["slug"] == slug, "re-uploading keeps the same link")
    check(me.get("/api/me/list").json()["list"]["books"] == 1, "with the new books")
    with store.db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM profile WHERE user_id IS NOT NULL").fetchone()["c"]
    check(n == 1, f"and one list per account, not a new row each time ({n})")

    check(fresh_client().get("/api/me/list").status_code == 401,
          "a signed-out browser gets a 401, not somebody's list")
    me.__exit__(None, None, None)


def test_owned_list_is_protected():
    print("\nA list with an owner")
    me = signed_in_client()
    slug = me.get("/api/me/list").json()["list"]["slug"]
    stranger = fresh_client()

    r = stranger.get(f"/api/profile/{slug}")
    check(r.status_code == 200, "the link still opens the report — sharing it is the point")

    r = stranger.post(f"/api/profile/{slug}/books", json={"books": []})
    check(r.status_code == 403, "but a stranger can't replace its books")
    r = stranger.post(f"/api/profile/{slug}/watch",
                      json={"enabled": True, "frequency": "daily",
                            "notify_type": "email", "notify_target": "attacker@example.com"})
    check(r.status_code == 403, "or point its alerts at their own address")

    r = me.post(f"/api/profile/{slug}/books",
                json={"name": "", "scopes": [], "formats": [], "books": BOOKS})
    check(r.status_code == 200, "while the owner still can")
    for c in (me, stranger):
        c.__exit__(None, None, None)


def test_anonymous_lists_still_work():
    print("\nSigned out, as before")
    anon = fresh_client()
    r = anon.post("/api/profile", json={"name": "No account", "scopes": SCOPES,
                                        "formats": ["audiobook-overdrive"], "books": BOOKS})
    check(r.status_code == 200, "anyone can still save a list without an account")
    slug = r.json()["slug"]
    r = anon.post(f"/api/profile/{slug}/books",
                  json={"name": "", "scopes": [], "formats": [], "books": BOOKS[:1]})
    check(r.status_code == 200, "and edit it with nothing but the link")

    me = signed_in_client()
    r = me.post("/api/me/list/claim", json={"slug": slug})
    check(r.status_code == 200, "an unowned list can be claimed into an account")
    check(store.profile_owner(slug) is not None, "and then has an owner")
    r = anon.post(f"/api/profile/{slug}/books",
                  json={"name": "", "scopes": [], "formats": [], "books": []})
    check(r.status_code == 403, "after which the bare link no longer edits it")

    other = fresh_client()
    r = other.post("/api/me/list/claim", json={"slug": slug})
    check(r.status_code == 401, "and a signed-out browser can't claim anything")
    for c in (anon, me, other):
        c.__exit__(None, None, None)


def test_claim_replaces_previous():
    print("\nOne list means one list")
    me = signed_in_client()
    before = me.get("/api/me/list").json()["list"]["slug"]
    anon = fresh_client()
    other_slug = anon.post("/api/profile", json={"name": "Second", "scopes": SCOPES,
                                                 "formats": ["audiobook-overdrive"],
                                                 "books": BOOKS}).json()["slug"]
    me.post("/api/me/list/claim", json={"slug": other_slug})
    now = me.get("/api/me/list").json()["list"]["slug"]
    check(now == other_slug, "claiming a second list makes it the account's list")
    check(store.profile_get(before) is None,
          "and the one it replaced is deleted, not left readable by its old link")
    for c in (me, anon):
        c.__exit__(None, None, None)


def test_change_password_and_delete():
    print("\nChanging the password, and leaving")
    me = signed_in_client()
    other_device = signed_in_client()

    r = me.post("/api/auth/password", json={"current": "not it", "password": "a fine new phrase"})
    check(r.status_code == 403, "changing it needs the old one")

    newer = "yet another good phrase"
    r = me.post("/api/auth/password", json={"current": PASSWORD, "password": newer})
    check(r.status_code == 200, "with the old one it works")
    check(me.get("/api/auth/me").json()["user"], "the device that changed it stays signed in")
    check(other_device.get("/api/auth/me").json()["user"] is None, "every other device is signed out")

    slug = me.get("/api/me/list").json()["list"]["slug"]
    r = me.post("/api/auth/delete", json={"password": "wrong entirely"})
    check(r.status_code == 403, "deleting the account needs the password too")
    r = me.post("/api/auth/delete", json={"password": newer})
    check(r.status_code == 200, "and then goes through")
    check(store.user_by_email("reader@example.com") is None, "the account is gone")
    check(store.profile_get(slug) is None, "and so is its list")
    for c in (me, other_device):
        c.__exit__(None, None, None)


def test_accounts_can_be_turned_off():
    print("\nAccounts turned off")
    # The settings object is frozen, which is the right shape for config and the
    # wrong shape for a test — so the flag is swapped through object.__setattr__
    # and put back in the finally, rather than the dataclass being loosened.
    from app.config import settings
    object.__setattr__(settings, "accounts_enabled", False)
    try:
        c = fresh_client()
        check(c.get("/api/auth/me").json()["accounts"] is False, "the app says so up front")
        check(c.post("/api/auth/register",
                     json={"email": "x@y.com", "password": PASSWORD}).status_code == 404,
              "and registering is a 404")
        r = c.post("/api/profile", json={"name": "still fine", "scopes": SCOPES,
                                         "formats": ["audiobook-overdrive"], "books": BOOKS})
        check(r.status_code == 200, "while the app itself works exactly as before")
        c.__exit__(None, None, None)
    finally:
        object.__setattr__(settings, "accounts_enabled", True)


def test_upgrade_from_before_accounts():
    """A database written by the version before accounts must survive the upgrade.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
    this is the only thing that exercises the ALTER path and the partial index
    that has to be built over a column the migration just added.
    """
    print("\nUpgrading an old database")
    import sqlite3
    import tempfile
    from app.config import settings

    path = str(Path(tempfile.mkdtemp()) / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE profile (
      slug TEXT PRIMARY KEY, name TEXT DEFAULT '', scopes TEXT NOT NULL DEFAULT '[]',
      formats TEXT NOT NULL DEFAULT '[]', books TEXT NOT NULL DEFAULT '[]',
      created_at REAL NOT NULL, updated_at REAL NOT NULL);
    INSERT INTO profile VALUES ('oldslug','Before accounts','[]','[]','[]',1,1);
    CREATE TABLE profile_state (
      slug TEXT NOT NULL, book_key TEXT NOT NULL, scope_key TEXT NOT NULL,
      status TEXT NOT NULL, wait_days INTEGER, seen_at REAL NOT NULL,
      PRIMARY KEY (slug, book_key, scope_key));
    INSERT INTO profile_state VALUES ('oldslug','bk','queenslibrary','holdable',12,1);
    """)
    conn.commit()
    conn.close()

    was = settings.db_path
    object.__setattr__(settings, "db_path", path)
    store.reset_connection()
    try:
        applied = store.init_db()
        check("profile.user_id" in applied, f"the migration adds the column: {applied}")
        check("profile_state.fmt" in applied,
              f"the rebuild is reported as a migration: {applied[-1]}")
        rows = store.profile_state_get("oldslug")
        check(list(rows) == [("bk", "queenslibrary", "audiobook-overdrive")],
              f"the remembered row survives, tagged as an audiobook: {list(rows)}")
        check(rows[("bk", "queenslibrary", "audiobook-overdrive")]["wait_days"] == 12,
              "with its wait intact — history isn't thrown away by the rebuild")

        from app.models import Availability
        store.profile_state_put("oldslug", "bk", Availability(
            scope_key="queenslibrary", scope_name="Queens", status="available",
            fmt="ebook-overdrive"))
        check(len(store.profile_state_get("oldslug")) == 2,
              "and the ebook row now sits beside it instead of overwriting it")

        prof = store.profile_get("oldslug")
        check(prof and prof["name"] == "Before accounts", "the existing list is still there")
        check(prof["user_id"] is None, "and is unowned, so its link keeps working")

        uid = store.user_create("upgraded@example.com", auth.hash_password(PASSWORD))
        store.profile_claim("oldslug", uid)
        check(store.profile_owner("oldslug") == uid, "it can be claimed into a new account")

        store.profile_save(None, name="a second one", scopes=[], formats=[], books=[],
                           user_id=uid)
        # profile_state gained a format in its primary key; the rebuild has to
        # happen on an upgraded database too, not just a fresh one.
        with store.db() as c:
            n = c.execute("SELECT COUNT(*) c FROM profile WHERE user_id=?", (uid,)).fetchone()["c"]
        check(n == 1, f"and one-list-per-account holds on the upgraded database ({n})")
        check(store.profile_get("oldslug") is None, "the replaced list is gone, not orphaned")
    finally:
        object.__setattr__(settings, "db_path", was)
        store.reset_connection()


def test_pages_are_served():
    print("\nPages")
    for path in ("/signin", "/reset?token=abc"):
        r = client.get(path)
        check(r.status_code == 200 and "<form" in r.text, f"{path} serves the sign-in page")


if __name__ == "__main__":
    store.init_db()
    capture_mail()
    for fn in [
        test_password_hashing, test_email_normalising, test_password_rules,
        test_register_and_confirm, test_registration_leaks_nothing,
        test_login_and_session, test_login_rate_limit,
        test_forgot_and_reset, test_expired_token, test_session_expiry,
        test_account_list, test_owned_list_is_protected,
        test_anonymous_lists_still_work, test_claim_replaces_previous,
        test_change_password_and_delete,
        test_accounts_can_be_turned_off, test_upgrade_from_before_accounts,
        test_pages_are_served,
    ]:
        fn()
    print("\nAll auth tests passed.")
