"""Passwords, tokens and sessions.

Everything here is standard library on purpose. This is a thing people run on a
box at home, and asking them to get argon2 compiling on a Raspberry Pi to read a
book list is a bad trade. `hashlib.scrypt` is memory-hard, ships with CPython,
and at the parameters below costs about 100ms and 64MB per attempt — enough that
guessing a stolen hash is expensive, little enough that a login still feels
instant.

Three secrets exist and none of them is ever stored in the clear:

  password   the user knows it; we keep a salted scrypt hash
  token      emailed for confirm/reset; we keep sha256(token), single use
  session    set as a cookie after login; we keep sha256(cookie)

Tokens and session cookies are high-entropy random, so a plain sha256 is the
right hash for them: there's nothing to brute-force. Passwords are low-entropy
and chosen by humans, which is why they get the slow one.
"""

import hashlib
import hmac
import re
import secrets
import time
import unicodedata

# Roughly 100ms / 64MB per hash on a modern CPU. n is the cost knob; if this
# ever needs raising, bump it here — verify() reads the parameters back out of
# the stored string, so old hashes keep working and get upgraded on next login.
SCRYPT_N = 2 ** 16
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2   # scrypt refuses without headroom

MIN_PASSWORD = 10
MAX_PASSWORD = 1024          # a hash of a 10MB "password" is a free DoS

CONFIRM_TTL = 48 * 3600
RESET_TTL = 2 * 3600
SESSION_TTL = 30 * 86400

# Deliberately loose. Email validity is decided by whether the confirmation mail
# arrives, not by a regex; the only job here is to reject the obviously-not-an-
# address before we hand it to smtplib.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def normalize_email(raw: str) -> str:
    """Lowercased and trimmed, so Bob@x.com and bob@x.com are one account.

    The local part is case-sensitive per the RFC and case-insensitive in
    practice at every mail host anyone uses. Following the RFC here would mean
    two people could register what they both believe is the same address.
    """
    return unicodedata.normalize("NFKC", (raw or "").strip()).lower()


def valid_email(raw: str) -> bool:
    return bool(EMAIL_RE.match(raw)) and len(raw) <= 254


def password_problem(password: str) -> str:
    """Empty string when the password is acceptable, else why it isn't.

    Length only. Composition rules ("must contain a digit") push people towards
    Passw0rd! and away from four random words, which is the opposite of what
    they're for.
    """
    if len(password or "") < MIN_PASSWORD:
        return f"Password needs at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return "That password is too long."
    return ""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                        p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. False for anything malformed rather than raising:
    a corrupt row shouldn't be a 500 on the login page."""
    try:
        scheme, n, r, p, salt_hex, want_hex = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=n, r=r, p=p, maxmem=128 * n * r * 2,
                            dklen=len(want_hex) // 2)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(dk.hex(), want_hex)


def needs_rehash(stored: str) -> bool:
    """True when a stored hash predates the current cost settings."""
    try:
        scheme, n, r, p, _, _ = (stored or "").split("$")
        return scheme != "scrypt" or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    except ValueError:
        return True


def new_token() -> str:
    """256 bits, url-safe. Goes in a link or a cookie; never stored as-is."""
    return secrets.token_urlsafe(32)


def token_hash(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


class RateLimiter:
    """A fixed window per key, held in memory.

    Deliberately not in the database: this guards the login form on a
    single-process self-hosted app, and the cost of a lost counter on restart is
    that an attacker gets one more window. Buying durability for that would mean
    a write on every failed password, which is a better DoS than the one it
    prevents.
    """

    def __init__(self, limit: int, window: int):
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        """True when the caller is under the limit. Does not record the attempt."""
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        self._hits[key] = hits
        return len(hits) < self.limit

    def hit(self, key: str) -> None:
        now = time.time()
        self._hits.setdefault(key, []).append(now)
        if len(self._hits) > 10000:                 # someone is spraying addresses
            self._prune(now)

    def clear(self, key: str) -> None:
        """After a success, so one fat-fingered password doesn't cost you the hour."""
        self._hits.pop(key, None)

    def retry_after(self, key: str) -> int:
        hits = self._hits.get(key, [])
        if not hits:
            return 0
        return max(1, int(self.window - (time.time() - min(hits))))

    def _prune(self, now: float) -> None:
        for key in list(self._hits):
            fresh = [t for t in self._hits[key] if now - t < self.window]
            if fresh:
                self._hits[key] = fresh
            else:
                del self._hits[key]


# Per-IP is the blunt instrument, per-email the targeted one: without the second,
# a botnet spread over many addresses can still grind one account's password.
login_ip = RateLimiter(limit=20, window=900)
login_email = RateLimiter(limit=8, window=900)
signup_ip = RateLimiter(limit=6, window=3600)
reset_email = RateLimiter(limit=4, window=3600)
