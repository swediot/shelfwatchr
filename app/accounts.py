"""Sign up, sign in, confirm, reset — and the one list an account owns.

Accounts are optional. Nothing here is required to use the app: uploading a CSV
and getting a report works signed out exactly as it always did, and a saved list
reached by its slug keeps working. An account adds one thing — the list follows
you to another device without keeping a link somewhere.

Two rules run through the whole file:

  Never say whether an address has an account. Register, forgot-password and
  resend all answer the same way for a known and an unknown address. The
  difference goes in the mail, which only the address's owner reads.

  Never let a slow answer say it either. A wrong password and a nonexistent
  account must cost about the same, or the timing tells you what the wording
  wouldn't — hence the deliberate dummy hash in login().
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import auth, notify, store
from .config import settings
from .models import ProfileIn

log = logging.getLogger("shelfwatchr.accounts")
router = APIRouter()

COOKIE = "sw_session"

# One hash of a throwaway password, computed once at import. login() verifies
# against this when the address is unknown, so "no such user" costs the same
# ~100ms as "wrong password" and can't be told apart with a stopwatch.
_DUMMY_HASH = auth.hash_password(secrets.token_urlsafe(16))


# ------------------------------------------------------------------ bodies

class Credentials(BaseModel):
    email: str = Field(default="", max_length=254)
    password: str = Field(default="", max_length=auth.MAX_PASSWORD)


class EmailOnly(BaseModel):
    email: str = Field(default="", max_length=254)


class ResetIn(BaseModel):
    token: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=auth.MAX_PASSWORD)


class PasswordChange(BaseModel):
    current: str = Field(default="", max_length=auth.MAX_PASSWORD)
    password: str = Field(default="", max_length=auth.MAX_PASSWORD)


class ClaimIn(BaseModel):
    slug: str = Field(default="", max_length=64)


# ----------------------------------------------------------------- helpers

def client_ip(request: Request) -> str:
    """The address to rate-limit against.

    X-Forwarded-For is attacker-controlled unless something upstream is
    rewriting it, so it's only believed when the operator has said there's a
    proxy. Otherwise one header would let a single client look like thousands
    and walk straight through every limit here.
    """
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def base_url(request: Request) -> str:
    """Where to point the links in an email.

    The configured public URL wins: a request that arrived at
    http://192.168.1.9:8080 through a proxy would otherwise produce a link that
    only works from inside the house.
    """
    return (settings.public_url or str(request.base_url)).rstrip("/")


def set_session_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        COOKIE, raw,
        max_age=int(auth.SESSION_TTL),
        httponly=True,                    # JavaScript can't read it, so XSS can't steal it
        samesite="lax",                   # not sent on cross-site POSTs — that's the CSRF defence
        secure=settings.secure_cookies,
        path="/",
    )


def current_user(request: Request) -> dict | None:
    """The signed-in user, or None. Never raises — most routes are public."""
    if not settings.accounts_enabled:
        return None
    return store.session_user(request.cookies.get(COOKIE, ""), extend=auth.SESSION_TTL)


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in first.")
    if not user["confirmed"]:
        raise HTTPException(403, "Confirm your email address first.")
    return user


def require_accounts() -> None:
    if not settings.accounts_enabled:
        raise HTTPException(404, "Accounts are turned off on this instance.")


def public_user(user: dict | None) -> dict | None:
    """What the browser is allowed to know about the signed-in account.

    An allowlist, not a blocklist: password_hash lives on the same dict, and a
    `return user` here would put it in an HTTP response the first time someone
    added a field.
    """
    if not user:
        return None
    return {
        "email": user["email"],
        "confirmed": user["confirmed"],
        "created_at": user["created_at"],
    }


async def _mail(kind: str, address: str, link: str) -> None:
    await notify.send_account_mail(kind, address, link)


# ------------------------------------------------------------------ signup

@router.post("/api/auth/register")
async def register(body: Credentials, request: Request):
    """Create an unconfirmed account and mail a confirmation link.

    Answers identically whether or not the address is already registered. If it
    is, the mail that goes out is a password-reset rather than a confirmation —
    which is the useful thing for the actual owner, who has probably forgotten
    they signed up, and tells a stranger nothing.
    """
    require_accounts()
    if not settings.signups_open:
        raise HTTPException(403, "This instance isn't taking new accounts.")

    ip = client_ip(request)
    if not auth.signup_ip.check(ip):
        raise HTTPException(429, "Too many sign-ups from here. Try again later.")

    email = auth.normalize_email(body.email)
    if not auth.valid_email(email):
        raise HTTPException(400, "That doesn't look like an email address.")
    problem = auth.password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)

    auth.signup_ip.hit(ip)
    user_id = store.user_create(email, auth.hash_password(body.password))

    if user_id is None:
        existing = store.user_by_email(email)
        if existing:
            kind = "confirm" if not existing["confirmed"] else "reset"
            token = auth.new_token()
            ttl = auth.CONFIRM_TTL if kind == "confirm" else auth.RESET_TTL
            store.token_create(existing["id"], kind, ttl, token)
            path = "/api/auth/confirm" if kind == "confirm" else "/reset"
            await _mail(kind, email, f"{base_url(request)}{path}?token={token}")
    else:
        token = auth.new_token()
        store.token_create(user_id, "confirm", auth.CONFIRM_TTL, token)
        await _mail("confirm", email, f"{base_url(request)}/api/auth/confirm?token={token}")

    return {"ok": True, "sent": True,
            "detail": "Check your email for a link to finish setting up."}


@router.post("/api/auth/resend")
async def resend(body: EmailOnly, request: Request):
    """Another confirmation link. Same non-answer as register."""
    require_accounts()
    email = auth.normalize_email(body.email)
    if auth.valid_email(email) and auth.reset_email.check(email):
        auth.reset_email.hit(email)
        user = store.user_by_email(email)
        if user and not user["confirmed"]:
            token = auth.new_token()
            store.token_create(user["id"], "confirm", auth.CONFIRM_TTL, token)
            await _mail("confirm", email, f"{base_url(request)}/api/auth/confirm?token={token}")
    return {"ok": True, "sent": True}


@router.get("/api/auth/confirm")
async def confirm(token: str = ""):
    """The link in the mail. Signs you in and drops you on the app.

    A GET that changes state, which is normally wrong — but the thing clicking
    it is a mail client, and mail clients only do GETs. The token being single
    use and short-lived is what keeps that honest.
    """
    require_accounts()
    user_id = store.token_consume(token, "confirm")
    if not user_id:
        return RedirectResponse("/?confirm=expired", status_code=303)
    store.user_confirm(user_id)
    store.user_touch_login(user_id)

    raw = auth.new_token()
    store.session_create(user_id, raw, auth.SESSION_TTL)
    response = RedirectResponse("/?confirm=ok", status_code=303)
    set_session_cookie(response, raw)
    return response


# ------------------------------------------------------------------- login

@router.post("/api/auth/login")
async def login(body: Credentials, request: Request, response: Response):
    require_accounts()
    ip = client_ip(request)
    email = auth.normalize_email(body.email)

    for limiter, key, who in ((auth.login_ip, ip, "here"), (auth.login_email, email, "this account")):
        if not limiter.check(key):
            raise HTTPException(429, f"Too many attempts for {who}. "
                                     f"Try again in {limiter.retry_after(key) // 60 + 1} minutes.")

    user = store.user_by_email(email)
    # Verify something either way: returning early on an unknown address makes
    # the response fast enough to enumerate accounts with a stopwatch.
    ok = auth.verify_password(body.password, user["password_hash"] if user else _DUMMY_HASH)

    if not user or not ok:
        auth.login_ip.hit(ip)
        auth.login_email.hit(email)
        raise HTTPException(401, "That email and password don't match.")

    if not user["confirmed"]:
        raise HTTPException(403, "Confirm your email address first — check your inbox.")

    auth.login_ip.clear(ip)
    auth.login_email.clear(email)

    # Cheap upgrade path if the cost parameters are ever raised: the only moment
    # the plaintext exists is right now.
    if auth.needs_rehash(user["password_hash"]):
        store.user_set_password(user["id"], auth.hash_password(body.password))

    raw = auth.new_token()
    store.session_create(user["id"], raw, auth.SESSION_TTL)
    store.user_touch_login(user["id"])
    set_session_cookie(response, raw)
    return {"ok": True, "user": public_user(store.user_by_id(user["id"]))}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    store.session_delete(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/api/auth/me")
async def me(request: Request):
    """Who's signed in, and whether accounts exist here at all.

    Called on every page load, so it also carries the flags the frontend needs
    to decide whether to show a sign-in link — one request instead of two.
    """
    user = current_user(request)
    return {
        "accounts": settings.accounts_enabled,
        "signups": settings.signups_open,
        "email_configured": bool(notify.SMTP_HOST),
        "user": public_user(user),
    }


# ---------------------------------------------------------------- password

@router.post("/api/auth/forgot")
async def forgot(body: EmailOnly, request: Request):
    """Always the same answer. The mail is the only place the truth appears."""
    require_accounts()
    email = auth.normalize_email(body.email)
    if auth.valid_email(email) and auth.reset_email.check(email):
        auth.reset_email.hit(email)
        user = store.user_by_email(email)
        if user:
            token = auth.new_token()
            store.token_create(user["id"], "reset", auth.RESET_TTL, token)
            await _mail("reset", email, f"{base_url(request)}/reset?token={token}")
    return {"ok": True, "sent": True,
            "detail": "If that address has an account, a reset link is on its way."}


@router.post("/api/auth/reset")
async def reset(body: ResetIn, response: Response):
    require_accounts()
    problem = auth.password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)

    user_id = store.token_consume(body.token, "reset")
    if not user_id:
        raise HTTPException(400, "That link has expired or was already used.")

    store.user_set_password(user_id, auth.hash_password(body.password))
    # A reset is what you do when you think someone else has your password, so
    # it has to end every other session — otherwise the intruder keeps theirs.
    store.session_delete_all(user_id)
    # Reaching a link sent to the address proves the address, so a reset also
    # confirms an account that never got round to it.
    store.user_confirm(user_id)

    raw = auth.new_token()
    store.session_create(user_id, raw, auth.SESSION_TTL)
    set_session_cookie(response, raw)
    return {"ok": True, "user": public_user(store.user_by_id(user_id))}


@router.post("/api/auth/password")
async def change_password(body: PasswordChange, request: Request, response: Response):
    """Change it while signed in. Needs the old one: a borrowed unlocked laptop
    shouldn't be enough to lock the owner out of their own account."""
    user = require_user(request)
    if not auth.verify_password(body.current, user["password_hash"]):
        raise HTTPException(403, "That's not your current password.")
    problem = auth.password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)

    store.user_set_password(user["id"], auth.hash_password(body.password))
    store.session_delete_all(user["id"])
    raw = auth.new_token()
    store.session_create(user["id"], raw, auth.SESSION_TTL)
    set_session_cookie(response, raw)
    return {"ok": True, "detail": "Password changed. Other devices have been signed out."}


@router.post("/api/auth/delete")
async def delete_account(body: Credentials, request: Request, response: Response):
    user = require_user(request)
    if not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(403, "Password doesn't match.")
    store.user_delete(user["id"])
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True, "detail": "Account and list deleted."}


# --------------------------------------------------- the account's one list

@router.get("/api/me/list")
async def my_list(request: Request):
    """The account's list, or null. The frontend calls this on load and, if
    there's something here, restores it without anyone pasting a link."""
    user = require_user(request)
    prof = store.profile_for_user(user["id"])
    if not prof:
        return {"list": None}
    return {"list": {
        "slug": prof["slug"],
        "name": prof["name"],
        "updated_at": prof["updated_at"],
        "books": len(prof["books"]),
        "watch_enabled": prof["watch_enabled"],
    }}


@router.post("/api/me/list")
async def save_my_list(body: ProfileIn, request: Request):
    """Save the current upload to the account, replacing whatever was there.

    Reuses the account's existing slug when it has one, so the share link stays
    the same across re-uploads and any bookmark of it keeps working.
    """
    user = require_user(request)
    existing = store.profile_for_user(user["id"])
    slug = store.profile_save(
        existing["slug"] if existing else None,
        name=body.name, scopes=body.scopes, formats=body.formats, books=body.books,
        user_id=user["id"],
    )
    if existing:
        from .service import to_book
        store.profile_prune_state(slug, {to_book(b).key for b in body.books})
    return {"ok": True, "slug": slug}


@router.post("/api/me/list/claim")
async def claim_list(body: ClaimIn, request: Request):
    """Attach a list you already had as a link to this account.

    Only unowned lists: someone else's slug, if it ever leaked, must not be
    absorbable into a stranger's account.
    """
    user = require_user(request)
    prof = store.profile_get(body.slug)
    if not prof:
        raise HTTPException(404, "No saved list with that link.")
    owner = store.profile_owner(body.slug)
    if owner is not None and owner != user["id"]:
        raise HTTPException(403, "That list already belongs to another account.")
    store.profile_claim(body.slug, user["id"])
    return {"ok": True, "slug": body.slug}
