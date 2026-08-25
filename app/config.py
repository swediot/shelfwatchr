"""Settings, all overridable by environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default=None):
    """SHELFWATCHR_* is the current spelling; SHELFWATCH_* still works so an
    existing deployment keeps running after the rename."""
    if name.startswith("SHELFWATCH_"):
        newer = "SHELFWATCHR_" + name[len("SHELFWATCH_"):]
        if newer in os.environ:
            return os.environ[newer]
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    return str(_env(name, str(default))).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Where the SQLite file lives. In Docker this is a mounted volume.
    db_path: str = _env("SHELFWATCH_DB", "data/shelfwatchr.db")

    # Politeness. One global budget shared by every request the server makes,
    # so ten people searching at once can't turn into ten times the traffic.
    requests_per_minute: float = _float("SHELFWATCH_RPM", 120)
    # The rate tunes itself between these bounds: up while the API is happy,
    # halved the moment it complains. Set adaptive off to pin it at RPM.
    rpm_ceiling: float = _float("SHELFWATCH_RPM_MAX", 300)
    rpm_floor: float = _float("SHELFWATCH_RPM_MIN", 30)
    adaptive_rate: bool = _bool("SHELFWATCH_ADAPTIVE_RATE", True)
    max_concurrency: int = _int("SHELFWATCH_CONCURRENCY", 3)
    http_timeout: float = _float("SHELFWATCH_TIMEOUT", 20)
    http_retries: int = _int("SHELFWATCH_RETRIES", 3)
    # Seconds for the first backoff after a 429/5xx that carries no Retry-After;
    # doubles each attempt. Configurable mainly so tests don't sit through it.
    backoff_base: float = _float("SHELFWATCH_BACKOFF_BASE", 5)

    # Cache lifetimes, seconds. Availability moves; absence from a catalogue
    # barely does.
    ttl_available: int = _int("SHELFWATCH_TTL_AVAILABLE", 6 * 3600)
    ttl_holdable: int = _int("SHELFWATCH_TTL_HOLDABLE", 12 * 3600)
    ttl_not_owned: int = _int("SHELFWATCH_TTL_NOT_OWNED", 7 * 86400)
    ttl_libraries: int = _int("SHELFWATCH_TTL_LIBRARIES", 30 * 86400)
    # A book's OverDrive id doesn't change. Keeping it for a month means repeat
    # runs skip the search step entirely — the single biggest speed-up there is.
    ttl_title_map: int = _int("SHELFWATCH_TTL_TITLE_MAP", 30 * 86400)

    # Batch sizes. The calibre Libby plugin chunks at 24; treat that as the
    # cautious convention until a real response proves otherwise.
    bulk_availability_size: int = _int("SHELFWATCH_BULK_SIZE", 96)
    max_libraries_per_search: int = _int("SHELFWATCH_MAX_LIBS_PER_SEARCH", 24)
    # Turn the batch path off entirely if it ever misbehaves against a real
    # catalogue; the one-request-per-book path still works.
    use_batch: bool = _bool("SHELFWATCH_BATCH", True)

    match_threshold: float = _float("SHELFWATCH_MATCH_THRESHOLD", 0.78)
    short_wait_days: int = _int("SHELFWATCH_SHORT_WAIT_DAYS", 21)
    max_books_per_request: int = _int("SHELFWATCH_MAX_BOOKS", 5000)

    # A job that has produced nothing for this long tells the page so, rather
    # than looking like it hung. It keeps running.
    job_stall_seconds: int = _int("SHELFWATCH_JOB_STALL", 180)
    # Finished jobs are scratch; the saved report is what lasts.
    job_retention_hours: int = _int("SHELFWATCH_JOB_RETENTION_HOURS", 48)

    # Nightly refresh of saved lists. 0 disables it.
    refresh_hour_utc: int = _int("SHELFWATCH_REFRESH_HOUR", 4)
    refresh_enabled: bool = _bool("SHELFWATCH_REFRESH", True)

    # Report books that stopped being available (borrowed out, or dropped from a
    # catalogue). Off: those are things you can't act on, and they make the
    # alerts read like bad news. Change detection still runs — it's only the
    # reporting that's suppressed.
    report_removals: bool = _bool("SHELFWATCH_REPORT_REMOVALS", False)

    # Used in notification links, so the alert on your phone opens the report.
    public_url: str = _env("SHELFWATCH_PUBLIC_URL", "").rstrip("/")

    # Accounts. Optional throughout: with this off the app behaves exactly as it
    # did before there were accounts, and saved lists are reached by their slug.
    accounts_enabled: bool = _bool("SHELFWATCH_ACCOUNTS", True)
    # Turn off to keep an instance private once you've made your own account.
    signups_open: bool = _bool("SHELFWATCH_SIGNUPS", True)
    # Set when a reverse proxy terminates TLS: the session cookie then carries
    # the Secure flag even though the app itself is speaking plain HTTP. Leave
    # it off for a plain-HTTP LAN instance or the cookie is never sent back.
    secure_cookies: bool = _bool("SHELFWATCH_SECURE_COOKIES", bool(
        _env("SHELFWATCH_PUBLIC_URL", "").startswith("https://")))
    # Only enable behind a proxy you control. Without it X-Forwarded-For is
    # ignored, because anyone can send that header and it feeds rate limiting.
    trust_proxy: bool = _bool("SHELFWATCH_TRUST_PROXY", False)

    # Serve fixture data instead of calling OverDrive. Used by the tests and
    # handy for demoing the interface with no network.
    mock: bool = _bool("SHELFWATCH_MOCK", False)


settings = Settings()
