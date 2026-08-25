"""Libby / OverDrive provider.

Talks to thunder.api.overdrive.com — the public catalogue endpoint the Libby web
app itself uses. No authentication, no library card, no personal data: the only
question asked is "does this library have this title, and is a copy in".
"""

from __future__ import annotations

import re
import time
from typing import Optional

import httpx

from ..config import settings
from ..matching import Book, estimate_wait_days, score_candidate
from ..models import FORMAT_LABEL as MODEL_FORMAT_LABEL
from ..models import Availability, Scope
from .base import ProviderError, RateLimiter

THUNDER = "https://thunder.api.overdrive.com/v2"

# Libby's own URL vocabulary for a format-scoped shelf. The id already picks the
# edition — audiobook and ebook are separate records with separate ids — but a
# link that lands in "everything" arrives in a context holding both editions of
# the work, and Libby then opens whichever it treats as primary. Naming the
# format in the path keeps an audiobook link on the audiobook.
LIBBY_COLLECTION = {
    "audiobook-overdrive": "format-audiobook",
    "ebook-overdrive": "format-ebook",
}


def title_url(scope_key: str, title_id: str, fmt: str) -> str:
    """The Libby deep link for one title, in one format, at one library."""
    shelf = LIBBY_COLLECTION.get(fmt, "everything")
    return f"https://libbyapp.com/library/{scope_key}/{shelf}/page-1/{title_id}"
CLIENT_ID = "dewey"  # the client id Libby's own web app sends
HEADERS = {
    "User-Agent": "shelfwatch/1.0 (personal library availability tool)",
    "Accept": "application/json",
    "Referer": "https://libbyapp.com/",
    "Origin": "https://libbyapp.com",
}

# Labels for whatever OverDrive hands back, which is more than the two formats
# the app offers — those live in models.FORMAT_LABEL.
FORMAT_LABEL = {
    **MODEL_FORMAT_LABEL,
    "ebook-kindle": "Kindle",
    "magazine-overdrive": "Magazine",
}


class LibbyProvider:
    name = "libby"

    def __init__(self, client: Optional[httpx.AsyncClient] = None, limiter: Optional[RateLimiter] = None):
        self._client = client
        self.limiter = limiter or RateLimiter(settings.requests_per_minute)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=settings.http_timeout,
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------- http

    async def _request(self, method: str, url: str, *, params=None, json_body=None) -> Optional[dict]:
        last = ""
        for attempt in range(settings.http_retries):
            await self.limiter.wait()
            try:
                resp = await self.client.request(method, url, params=params, json=json_body)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}"
                await self.limiter.back_off(settings.backoff_base * 0.4 * (attempt + 1))
                continue

            if resp.status_code == 404:
                return None
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                try:
                    pause = float(retry_after) if retry_after else 0.0
                except ValueError:
                    pause = 0.0
                pause = pause or min(60.0, settings.backoff_base * (2 ** attempt))
                # Slow the whole server down, and remember that we were told to.
                await self.limiter.record_throttle()
                await self.limiter.back_off(pause)
                last = f"HTTP {resp.status_code}"
                continue
            if resp.status_code >= 400:
                raise ProviderError(f"HTTP {resp.status_code}", status=resp.status_code)
            try:
                data = resp.json()
            except ValueError:
                last = "bad JSON"
                continue
            await self.limiter.record_success()
            return data
        raise ProviderError(last or "request failed")

    async def _get(self, url: str, params: dict) -> Optional[dict]:
        return await self._request("GET", url, params=params)

    # ---------------------------------------------------------- scopes

    async def search_scopes(self, query: str) -> list[Scope]:
        """Find libraries by name, or by their Libby key."""
        found: list[Scope] = []
        seen: set[str] = set()

        # An exact key hit first: people often paste the slug from the URL.
        slug = query.strip().strip("/").split("/")[-1].lower()
        if slug and " " not in slug:
            try:
                direct = await self._get(f"{THUNDER}/libraries/{slug}", {})
            except ProviderError:
                direct = None
            if isinstance(direct, dict) and direct.get("name"):
                key = direct.get("preferredKey") or slug
                seen.add(key)
                found.append(Scope(key=key, name=direct["name"], region=_region_of(direct)))

        try:
            data = await self._get(f"{THUNDER}/libraries", {"query": query, "perPage": 20})
        except ProviderError:
            data = None

        for item in _items(data):
            key = item.get("preferredKey") or item.get("key") or ""
            name = item.get("name") or item.get("fulfillmentId") or key
            if not key or key in seen:
                continue
            seen.add(key)
            found.append(Scope(key=key, name=name, region=_region_of(item)))
        return found

    # ---------------------------------------------------------- lookup

    async def lookup(self, book: Book, scope: Scope, fmt: str, threshold: float) -> Availability:
        av = Availability(
            provider=self.name,
            scope_key=scope.key,
            scope_name=scope.name,
            format=FORMAT_LABEL.get(fmt, fmt), fmt=fmt,
            checked_at=time.time(),
        )
        try:
            data = await self._get(
                f"{THUNDER}/libraries/{scope.key}/media",
                {
                    "query": book.search_query,
                    "format": fmt,
                    "perPage": 12,
                    "page": 1,
                    "x-client-id": CLIENT_ID,
                },
            )
        except ProviderError as exc:
            av.status = "error"
            av.note = str(exc)[:120]
            return av

        best, best_score = None, 0.0
        for item in _items(data):
            score = score_candidate(
                book,
                item.get("title") or "",
                item.get("subtitle") or "",
                _authors_of(item),
            )
            if score > best_score:
                best, best_score = item, score

        av.match_score = round(best_score, 2)
        if not best or best_score < threshold:
            av.status = "not_owned"
            if best:
                av.note = f"closest: {(best.get('title') or '')[:60]}"
            return av

        av.matched_title = best.get("title", "")
        av.matched_author = best.get("firstCreatorName", "")
        media_id = str(best.get("id") or best.get("titleId") or "")
        if media_id:
            av.title_id = media_id
            av.url = title_url(scope.key, media_id, fmt)

        av.duration_seconds = _duration_of(best)
        info = _availability_of(best)
        av.available_copies = info["available"]
        av.owned_copies = info["owned"]
        av.holds = info["holds"]
        av.lucky_day = info["lucky_day"]

        if info["owned"] == 0 and not info["is_available"]:
            av.status = "not_owned"
        elif info["is_available"] or info["available"] > 0 or info["lucky_day"] > 0:
            av.status = "available"
            av.wait_days = 0
        else:
            av.status = "holdable"
            if info["wait"] is not None:
                av.wait_days = info["wait"]
            else:
                av.wait_days = estimate_wait_days(info["holds"], info["owned"])
                av.wait_estimated = True
        return av


    # ----------------------------------------------------------- batch

    # These two endpoints are what make a 1,200-book list bearable. Neither is
    # documented by OverDrive; both are taken from the Libby calibre plugin's
    # client, which is real working code against the same API. If either turns
    # out not to behave as expected against a live catalogue, `batch_ok` flips
    # to False and everything falls back to one search per book per library —
    # slower, but the same answers.
    batch_ok: bool = True
    batch_note: str = "untested against a live catalogue"

    async def search_across(self, book: Book, scope_keys: list[str], fmt: str,
                            threshold: float) -> Optional[dict]:
        """One search covering every library at once.

        Returns {"id", "title", "author", "score", "availability": {scope_key: raw}}
        — availability only for scopes the response happens to carry.
        """
        params = [("query", book.search_query), ("format", fmt),
                  ("maxItems", 24), ("x-client-id", CLIENT_ID)]
        # Repeated libraryKey params, not a comma-joined list.
        params += [("libraryKey", k) for k in scope_keys[:settings.max_libraries_per_search]]

        data = await self._request("GET", f"{THUNDER}/media/search/", params=params)
        best, best_score = None, 0.0
        for item in _items(data):
            score = score_candidate(book, item.get("title") or "", item.get("subtitle") or "",
                                    _authors_of(item))
            if score > best_score:
                best, best_score = item, score
        if not best or best_score < threshold:
            return None

        by_scope = {}
        # Some responses carry per-library availability inline; take it when it's
        # there and save a round trip.
        for holder in (best.get("siteAvailabilities") or {}, best.get("availabilities") or {}):
            if isinstance(holder, dict):
                for key, value in holder.items():
                    if isinstance(value, dict):
                        by_scope[key] = value
            elif isinstance(holder, list):
                for value in holder:
                    key = (value or {}).get("libraryKey") or (value or {}).get("preferredKey")
                    if key:
                        by_scope[key] = value
        return {
            "id": best.get("id") or best.get("titleId") or "",
            "title": best.get("title", ""),
            "author": best.get("firstCreatorName", ""),
            "score": round(best_score, 2),
            "availability": by_scope,
        }

    # How many ids to put in one bulk request. Starts optimistic and shrinks if
    # the server objects — see service.lookup_chunk. 24 is the Libby calibre
    # plugin's convention, but nothing says that's the ceiling.
    bulk_size: int = 0

    async def availability_bulk(self, scope_key: str, title_ids: list[str]) -> dict:
        """Availability for many titles at one library, in one request."""
        if not title_ids:
            return {}
        data = await self._request(
            "POST", f"{THUNDER}/libraries/{scope_key}/media/availability",
            params={"x-client-id": CLIENT_ID}, json_body={"ids": list(title_ids)},
        )
        out = {}
        for item in _items(data):
            key = str(item.get("id") or item.get("titleId") or item.get("reserveId") or "")
            if key:
                out[key] = item
        return out

    def availability_from_raw(self, raw: dict, scope: Scope, fmt: str,
                              *, title_id: str = "", matched_title: str = "",
                              matched_author: str = "", score: float = 0.0) -> Availability:
        """Turn a raw catalogue item into our shape. Shared by both paths."""
        av = Availability(
            provider=self.name, scope_key=scope.key, scope_name=scope.name,
            format=FORMAT_LABEL.get(fmt, fmt), fmt=fmt, checked_at=time.time(),
            matched_title=matched_title, matched_author=matched_author, match_score=score,
        )
        if title_id:
            av.title_id = str(title_id)
            av.url = title_url(scope.key, title_id, fmt)
        av.duration_seconds = _duration_of(raw or {})
        info = _availability_of(raw or {})
        av.available_copies = info["available"]
        av.owned_copies = info["owned"]
        av.holds = info["holds"]
        av.lucky_day = info["lucky_day"]

        if info["owned"] == 0 and not info["is_available"]:
            av.status = "not_owned"
        elif info["is_available"] or info["available"] > 0 or info["lucky_day"] > 0:
            av.status, av.wait_days = "available", 0
        else:
            av.status = "holdable"
            if info["wait"] is not None:
                av.wait_days = info["wait"]
            else:
                av.wait_days = estimate_wait_days(info["holds"], info["owned"])
                av.wait_estimated = True
        return av


# ----------------------------------------------------------- helpers


def _items(data) -> list:
    if isinstance(data, dict):
        return data.get("items") or []
    if isinstance(data, list):
        return data
    return []


def _region_of(item: dict) -> str:
    for key in ("consortiumName", "websiteId", "settings", "parentCrid"):
        val = item.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _authors_of(item: dict) -> list:
    creators = item.get("creators") or []
    names = [
        c.get("name", "")
        for c in creators
        if str(c.get("role", "")).lower() in ("author", "writer", "")
    ]
    return names or [item.get("firstCreatorName", "")]


DURATION_PATTERNS = (
    # 9:51:00 / 551:00 — colon-separated, biggest unit first
    re.compile(r"^(?:(\d+):)?(\d+):(\d{1,2})$"),
    # 9h 51m / 9 hrs 51 mins / 51m
    re.compile(r"^(?:(\d+)\s*h\w*)?\s*(?:(\d+)\s*m\w*)?$", re.I),
    # ISO 8601 duration, PT9H51M
    re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?$", re.I),
)


def parse_duration(value) -> Optional[int]:
    """Seconds from whatever shape the catalogue used.

    Nobody documents this field, and the wild has at least four spellings, so
    each is tried in turn and anything unrecognised returns None rather than a
    wrong number — a missing length sorts last, a wrong one sorts wrong.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = int(value)
        # Some payloads count minutes, not seconds. A 40-second audiobook
        # doesn't exist; 40 minutes does.
        return seconds if seconds > 600 else seconds * 60
    text = str(value).strip()
    if not text:
        return None

    colon, hm, iso = DURATION_PATTERNS
    m = colon.match(text)
    if m:
        hours, minutes, secs = m.groups()
        if hours is None:   # MM:SS
            return int(minutes) * 60 + int(secs)
        return int(hours) * 3600 + int(minutes) * 60 + int(secs)
    for pattern in (hm, iso):
        m = pattern.match(text)
        if m and any(m.groups()):
            hours, minutes = m.groups()[:2]
            return int(hours or 0) * 3600 + int(minutes or 0) * 60
    return None


def _duration_of(item: dict) -> Optional[int]:
    """Find a length anywhere the item might be keeping one."""
    for key in ("duration", "estimatedDuration", "playbackDuration", "length"):
        found = parse_duration(item.get(key))
        if found:
            return found
    for fmt in item.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        for key in ("duration", "estimatedDuration", "length"):
            found = parse_duration(fmt.get(key))
            if found:
                return found
    return None


def _availability_of(item: dict) -> dict:
    """Thunder puts these at the top level, but be forgiving about shape."""
    src: dict = {}
    for holder in (item, item.get("availability") or {}, item.get("copiesAvailable") or {}):
        if isinstance(holder, dict):
            for k, v in holder.items():
                src.setdefault(k, v)

    def as_int(*keys) -> int:
        for k in keys:
            v = src.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    owned = as_int("ownedCopies", "copiesOwned", "totalCopies")
    available = as_int("availableCopies", "copiesAvailable")
    wait = src.get("estimatedWaitDays")
    wait = int(wait) if isinstance(wait, (int, float)) and not isinstance(wait, bool) else None
    return {
        "owned": owned,
        "available": available,
        "holds": as_int("holdsCount", "numberOfHolds"),
        "wait": wait,
        "is_available": bool(src.get("isAvailable")) or available > 0,
        "is_holdable": bool(src.get("isHoldable", owned > 0)),
        "lucky_day": as_int("luckyDayAvailableCopies"),
    }
