"""One shared password in front of the whole site.

This is the beta-testing door, not an account system: everyone who is let in is
the same anonymous visitor afterwards, and the app behaves exactly as it does
with no gate at all. It exists so a public URL can be handed to a handful of
testers without being handed to the open web.

The cookie is derived from the password itself rather than from a separate
server secret, which buys two things: nothing to generate or store, and
changing SHELFWATCHR_BETA_PASSWORD signs everybody out. It carries its own
expiry, signed alongside, so a stolen cookie is not a permanent key.

Turning the gate off is unsetting the password. Nothing else in the app knows
this module exists.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import auth
from .accounts import client_ip
from .config import settings

router = APIRouter()

COOKIE = "sw_beta"
TTL = 30 * 86400
WEB = Path(__file__).resolve().parent.parent / "web"

# Reachable without the password, or the door can't be opened and Fly's health
# check declares the machine dead. Everything else is behind it.
OPEN_PATHS = {"/gate", "/api/gate", "/api/health", "/robots.txt", "/favicon.ico"}

# Ten tries per quarter hour per address. A single shared password is exactly
# the shape of thing worth guessing, and there's no per-account limiter here to
# fall back on.
gate_ip = auth.RateLimiter(limit=10, window=900)


class GateIn(BaseModel):
    password: str = Field(default="", max_length=auth.MAX_PASSWORD)
    next: str = Field(default="/", max_length=500)


def enabled() -> bool:
    return bool(settings.beta_password)


def _key() -> bytes:
    return hashlib.sha256(settings.beta_password.encode("utf-8")).digest()


def _sign(expires: int) -> str:
    return hmac.new(_key(), str(expires).encode("ascii"), hashlib.sha256).hexdigest()


def mint() -> str:
    expires = int(time.time()) + TTL
    return f"{expires}.{_sign(expires)}"


def valid(token: str) -> bool:
    """Constant-time, and False for anything malformed rather than raising."""
    try:
        raw_expires, signature = (token or "").split(".", 1)
        expires = int(raw_expires)
    except (ValueError, AttributeError):
        return False
    if expires < time.time():
        return False
    return hmac.compare_digest(_sign(expires), signature)


def passed(request: Request) -> bool:
    return not enabled() or valid(request.cookies.get(COOKIE, ""))


def safe_next(target: str) -> str:
    """Only ever redirect back into this site.

    `next` arrives from a query string, so `//evil.example` and
    `https://evil.example` both have to be refused: either would turn the gate
    into an open redirect that borrows this domain's credibility.
    """
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@router.get("/gate")
async def gate_page(request: Request):
    """Already inside? Then there's nothing to ask for."""
    if passed(request):
        return RedirectResponse(safe_next(request.query_params.get("next", "/")), 303)
    return FileResponse(WEB / "gate.html", status_code=401)


@router.post("/api/gate")
async def gate_submit(body: GateIn, request: Request, response: Response):
    if not enabled():
        return {"ok": True, "next": "/"}

    ip = client_ip(request)
    if not gate_ip.check(ip):
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.",
                            headers={"Retry-After": str(gate_ip.retry_after(ip))})

    if not hmac.compare_digest(body.password or "", settings.beta_password):
        gate_ip.hit(ip)
        raise HTTPException(401, "That isn't the password.")

    gate_ip.clear(ip)
    response.set_cookie(
        COOKIE, mint(),
        max_age=TTL,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )
    return {"ok": True, "next": safe_next(body.next or "/")}


@router.get("/robots.txt")
async def robots():
    """A beta instance has no business in a search index, and the gate alone
    wouldn't keep it out of one — crawlers index the URL, not the contents."""
    if enabled():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")
    return PlainTextResponse("User-agent: *\nAllow: /\n")


async def middleware(request: Request, call_next):
    """Turn every other request away until the password has been given.

    The two audiences want different answers: a browser following a link should
    land on the form, while the frontend's fetch() wants a status it can act on
    rather than a page of HTML where JSON was expected.
    """
    if passed(request) or request.url.path in OPEN_PATHS:
        return await call_next(request)

    if request.url.path.startswith("/api/"):
        return PlainTextResponse("This instance is in closed beta.", status_code=401)

    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(f"/gate?next={quote(target, safe='')}", 303)
