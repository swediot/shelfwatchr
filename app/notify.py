"""Telling you something changed.

Three ways, none of which need an account with anyone:

  ntfy     — a topic name; push notification on your phone via ntfy.sh, or your
             own ntfy server if you run one. Easiest thing that reaches a pocket.
  webhook  — any URL that takes a JSON POST (Discord, Slack, Home Assistant,
             your own script).
  email    — plain SMTP, configured once on the server, not per user, so nobody's
             mail password ends up in the database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from .changes import Change, summarise
from .config import _env

log = logging.getLogger("shelfwatchr.notify")

NTFY_DEFAULT_SERVER = _env("SHELFWATCH_NTFY_SERVER", "https://ntfy.sh")

SMTP_HOST = _env("SHELFWATCH_SMTP_HOST", "")
SMTP_PORT = int(_env("SHELFWATCH_SMTP_PORT", "587"))
SMTP_USER = _env("SHELFWATCH_SMTP_USER", "")
SMTP_PASS = _env("SHELFWATCH_SMTP_PASS", "")
SMTP_FROM = _env("SHELFWATCH_SMTP_FROM", SMTP_USER or "shelfwatchr@localhost")
SMTP_TLS = str(_env("SHELFWATCH_SMTP_TLS", "starttls")).lower()  # starttls | ssl | none


# A push notification is skimmed, not read. The dot says how soon the book
# reaches you — green now, orange a shorter queue, red a longer one — so the
# shape of the alert lands before any of the words do.
GREEN, ORANGE, RED, GREY = "\U0001F7E2", "\U0001F7E0", "\U0001F534", "\U000026AA"
DOT = {
    "now_available": GREEN,
    "newly_holdable": ORANGE,
    "wait_dropped": ORANGE,
    "wait_grew": RED,
    "no_longer_available": RED,
    "left_catalogue": RED,
}
NTFY_TAG = {GREEN: "green_circle", ORANGE: "orange_circle", RED: "red_circle"}


def build_message(list_name: str, changes: list[Change], link: str = "") -> tuple[str, str]:
    """(title, body) — plain text, because every channel accepts that."""
    title = f"{list_name or 'Your list'}: {summarise(changes)}"
    # brief() leaves the library name out, and once it's gone the same book at
    # two libraries is the same line twice — fromkeys keeps the first of each.
    lines = list(dict.fromkeys(
        f"{DOT.get(c.kind, GREY)} {c.brief()}" for c in changes))
    if link:
        lines += ["", link]
    return title, "\n".join(lines)


def ntfy_tags(body: str) -> str:
    """The circles used in the body, as ntfy emoji shortcodes.

    ntfy draws tagged emoji beside the title, so the alert is colour-coded on the
    lock screen before it's opened. Changes arrive sorted good news first, so the
    tags come out green, orange, red in that order.
    """
    dots = dict.fromkeys(
        NTFY_TAG[line[0]] for line in body.splitlines() if line[:1] in NTFY_TAG)
    return ",".join([*dots, "books"])


SECTIONS = [
    ("now_available", "Available now"),
    ("newly_holdable", "Newly holdable"),
    ("wait_dropped", "Shorter wait"),
    ("wait_grew", "Longer wait"),
    ("no_longer_available", "No longer on the shelf"),
    ("left_catalogue", "Gone from the catalogue"),
]


def build_email(list_name: str, changes: list[Change], *, period: str = "week",
                available_now: list | None = None, link: str = "") -> tuple[str, str, str]:
    """(subject, plain_text, html) for the digest.

    Deliberately table-free, inline-styled and light-background: email clients
    apply their own dark-mode inversion in wildly different ways, and a design
    that survives all of them is a design that doesn't fight them.
    """
    esc = _esc
    when = "This week" if period == "week" else "Today"
    subject = f"{list_name or 'Shelfwatchr'}: {summarise(changes)}"

    grouped = {}
    for c in changes:
        grouped.setdefault(c.kind, []).append(c)

    blocks = []
    for kind, heading in SECTIONS:
        items = grouped.get(kind)
        if not items:
            continue
        rows = "".join(
            f'<li style="margin:0 0 6px;line-height:1.45">'
            f'<strong style="color:#1b1a18">{esc(c.title)}</strong>'
            f'<span style="color:#6b6660"> — {esc(_detail(c))}</span></li>'
            for c in items
        )
        accent = "#1d6b35" if kind in ("now_available", "newly_holdable", "wait_dropped") else "#8a5a12"
        blocks.append(
            f'<h2 style="font:600 15px/1.3 -apple-system,Segoe UI,Roboto,sans-serif;'
            f'color:{accent};margin:22px 0 8px">{esc(heading)}</h2>'
            f'<ul style="margin:0;padding-left:18px;font:14px -apple-system,Segoe UI,Roboto,sans-serif">{rows}</ul>'
        )

    if available_now:
        shown = available_now[:12]
        rows = "".join(
            f'<li style="margin:0 0 4px;color:#6b6660">'
            f'<strong style="color:#1b1a18">{esc(t)}</strong> — {esc(libs)}</li>'
            for t, libs in shown
        )
        more = (f'<p style="margin:6px 0 0;color:#6b6660;font-size:13px">'
                f'…and {len(available_now) - len(shown)} more.</p>'
                if len(available_now) > len(shown) else "")
        blocks.append(
            '<h2 style="font:600 15px/1.3 -apple-system,Segoe UI,Roboto,sans-serif;'
            'color:#1b1a18;margin:26px 0 8px">On the shelf right now</h2>'
            f'<ul style="margin:0;padding-left:18px;font:14px -apple-system,Segoe UI,Roboto,sans-serif">{rows}</ul>{more}'
        )

    button = (
        f'<p style="margin:28px 0 0"><a href="{esc(link)}" '
        'style="background:#3a6ea5;color:#fff;text-decoration:none;padding:11px 18px;'
        'border-radius:8px;font:600 14px -apple-system,Segoe UI,Roboto,sans-serif;'
        'display:inline-block">Open the full report</a></p>'
    ) if link else ""

    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:#f4f2ee">
<div style="max-width:600px;margin:0 auto;padding:28px 20px 40px">
  <p style="margin:0 0 2px;color:#6b6660;font:12px -apple-system,Segoe UI,Roboto,sans-serif;
     text-transform:uppercase;letter-spacing:.06em">{esc(when)} on Shelfwatchr</p>
  <h1 style="margin:0 0 4px;font:600 21px/1.25 -apple-system,Segoe UI,Roboto,sans-serif;color:#1b1a18">
    {esc(list_name or 'Your reading list')}</h1>
  <p style="margin:0;color:#6b6660;font:14px -apple-system,Segoe UI,Roboto,sans-serif">
    {esc(summarise(changes))}</p>
  <div style="background:#fff;border:1px solid #e6e2dc;border-radius:12px;padding:4px 20px 22px;margin-top:18px">
    {''.join(blocks)}
    {button}
  </div>
  <p style="margin:22px 0 0;color:#8a857e;font:12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif">
    From your libraries' Libby catalogues. Waits marked ~ are estimated from the hold queue.
    Turn this off in the list's settings.</p>
</div></body></html>"""

    text_lines = []
    for kind, heading in SECTIONS:
        items = grouped.get(kind)
        if items:
            text_lines += [f"{heading}:"] + [f"  - {c.title} — {_detail(c)}" for c in items] + [""]
    if available_now:
        text_lines += ["On the shelf right now:"] + [f"  - {t} — {libs}" for t, libs in available_now[:12]] + [""]
    if link:
        text_lines.append(link)
    return subject, "\n".join(text_lines), html


def _detail(c: Change) -> str:
    """The sentence minus the title, which the HTML shows separately."""
    sentence = c.sentence()
    prefix = f"{c.title} — "
    return sentence[len(prefix):] if sentence.startswith(prefix) else sentence


def _esc(value) -> str:
    return (str(value or "")
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


async def send(notify_type: str, target: str, title: str, body: str,
               html: str | None = None) -> dict:
    """Never raises — a failed notification must not break the refresh run."""
    try:
        if notify_type == "ntfy":
            return await _send_ntfy(target, title, body)
        if notify_type == "webhook":
            return await _send_webhook(target, title, body)
        if notify_type == "email":
            # smtplib is blocking and can sit for its full timeout on a slow
            # server. On the event loop that stalls every other request, so it
            # goes to a thread.
            return await asyncio.to_thread(_send_email, target, title, body, html)
        return {"ok": True, "skipped": "notifications off"}
    except Exception as exc:  # noqa: BLE001
        log.warning("notification via %s failed: %s", notify_type, exc)
        return {"ok": False, "error": str(exc)[:200]}


async def _send_ntfy(target: str, title: str, body: str) -> dict:
    topic = target.strip().rstrip("/")
    url = topic if topic.startswith("http") else f"{NTFY_DEFAULT_SERVER}/{topic}"
    if not topic:
        return {"ok": False, "error": "no ntfy topic set"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            content=body.encode("utf-8"),
            headers={
                "Title": title.encode("ascii", "replace").decode(),  # header must be latin-1 safe
                "Tags": ntfy_tags(body),
                "Priority": "default",
            },
        )
    return {"ok": resp.status_code < 400, "status": resp.status_code}


async def _send_webhook(target: str, title: str, body: str) -> dict:
    if not target.startswith("http"):
        return {"ok": False, "error": "webhook target must be a URL"}
    payload = {
        "title": title,
        "text": body,
        "content": f"**{title}**\n{body}",  # Discord uses "content"
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(target, json=payload)
    return {"ok": resp.status_code < 400, "status": resp.status_code}


def _send_email(target: str, title: str, body: str, html: str | None = None) -> dict:
    if not SMTP_HOST:
        return {"ok": False, "error": "no SMTP server configured on this instance"}
    if "@" not in target:
        return {"ok": False, "error": "not an email address"}

    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = SMTP_FROM
    msg["To"] = target
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    if SMTP_TLS == "ssl":
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=20) as s:
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            if SMTP_TLS == "starttls":
                s.starttls(context=context)
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    return {"ok": True}


def channel_status() -> dict:
    """What this instance can actually do — the UI greys out the rest."""
    return {
        "ntfy": True,
        "webhook": True,
        "email": bool(SMTP_HOST),
        "ntfy_server": NTFY_DEFAULT_SERVER,
    }


# ------------------------------------------------------------ account mail

ACCOUNT_MAIL = {
    "confirm": (
        "Confirm your Shelfwatchr address",
        "Confirm your address",
        "Click below and your account is ready. The link works once, and expires "
        "in 48 hours.",
        "Confirm my address",
        "If you didn't sign up for Shelfwatchr, ignore this — no account is "
        "created until the link is used.",
    ),
    "reset": (
        "Reset your Shelfwatchr password",
        "Set a new password",
        "Click below to choose a new password. The link works once, and expires "
        "in 2 hours.",
        "Choose a new password",
        "If you didn't ask for this, ignore it — your current password still "
        "works and nothing has changed.",
    ),
}


def build_account_email(kind: str, link: str) -> tuple[str, str, str]:
    """(subject, plain_text, html) for confirm and reset mail.

    Both say plainly what to do if it wasn't you, because these are the two
    messages people receive when someone else has typed their address in.
    """
    subject, heading, blurb, cta, footer = ACCOUNT_MAIL[kind]
    esc = _esc
    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:#f4f2ee">
<div style="max-width:520px;margin:0 auto;padding:32px 20px 40px">
  <p style="margin:0 0 2px;color:#6b6660;font:12px -apple-system,Segoe UI,Roboto,sans-serif;
     text-transform:uppercase;letter-spacing:.06em">Shelfwatchr</p>
  <div style="background:#fff;border:1px solid #e6e2dc;border-radius:12px;padding:24px 22px">
    <h1 style="margin:0 0 8px;font:600 20px/1.25 -apple-system,Segoe UI,Roboto,sans-serif;color:#1b1a18">
      {esc(heading)}</h1>
    <p style="margin:0;color:#4a463f;font:14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif">
      {esc(blurb)}</p>
    <p style="margin:22px 0 0"><a href="{esc(link)}"
      style="background:#3a6ea5;color:#fff;text-decoration:none;padding:11px 18px;
      border-radius:8px;font:600 14px -apple-system,Segoe UI,Roboto,sans-serif;
      display:inline-block">{esc(cta)}</a></p>
    <p style="margin:18px 0 0;color:#8a857e;font:12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
       word-break:break-all">Or paste this into your browser:<br>{esc(link)}</p>
  </div>
  <p style="margin:20px 0 0;color:#8a857e;font:12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif">
    {esc(footer)}</p>
</div></body></html>"""
    text = f"{heading}\n\n{blurb}\n\n{link}\n\n{footer}\n"
    return subject, text, html


async def send_account_mail(kind: str, address: str, link: str) -> dict:
    """Confirm/reset mail, with a log fallback when there's no SMTP server.

    Without the fallback, standing up a fresh instance is a chicken-and-egg
    problem: you can't sign in to configure anything because the mail that would
    let you sign in can't be sent. Printing the link to the server log is a
    deliberate trade — anyone who can read your logs can take over an account,
    which is why it only happens when SMTP is genuinely absent, and says so.
    """
    subject, text, html = build_account_email(kind, link)
    if not SMTP_HOST:
        log.warning(
            "No SMTP configured — printing the %s link for %s instead of mailing it. "
            "Set SHELFWATCH_SMTP_HOST to stop this.\n    %s", kind, address, link)
        return {"ok": True, "delivered": "log"}
    result = await send("email", address, subject, text, html)
    if not result.get("ok"):
        log.warning("account mail (%s) to %s failed: %s", kind, address, result.get("error"))
    return {**result, "delivered": "email"}
