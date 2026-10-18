"""Gmail: send cold emails (SMTP) and read signup verification mails (IMAP).

Needs a Google App Password (Google Account -> Security -> 2-Step Verification -> App passwords).
Mails sent via Gmail SMTP appear in your Gmail "Sent" folder automatically.
"""
import asyncio
import email
import imaplib
import mimetypes
import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

from . import config


def _check_config() -> None:
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing in .env")


def _send(to: str, subject: str, body: str, attachments: list[str], in_reply_to: str | None = None,
          from_name: str | None = None) -> str:
    _check_config()
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, config.GMAIL_ADDRESS)) if from_name else config.GMAIL_ADDRESS
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=config.GMAIL_ADDRESS.split("@")[-1])
    if in_reply_to:  # follow-up: shows up as a reply in the same thread
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    for path in attachments:
        p = Path(path)
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        s.send_message(msg)
    return msg["Message-ID"]


async def send_email(to: str, subject: str, body: str, attachments: list[str], in_reply_to: str | None = None,
                     from_name: str | None = None) -> str:
    """Send through Gmail; returns the Message-ID (used to thread follow-ups)."""
    return await asyncio.to_thread(_send, to, subject, body, attachments, in_reply_to, from_name)


def _messages_from(addresses: list[str], since_ts: float) -> dict[str, str]:
    """Which of these addresses wrote to us since since_ts -> {address: first lines of their latest mail}."""
    _check_config()
    found: dict[str, str] = {}
    if not addresses:
        return found
    days = max(1, int((time.time() - since_ts) / 86400) + 1)
    with imaplib.IMAP4_SSL("imap.gmail.com") as m:
        m.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        m.select("INBOX")
        for i in range(0, len(addresses), 20):  # Gmail search: from:(a OR b OR ...)
            chunk = addresses[i:i + 20]
            query = f"newer_than:{days}d from:({' OR '.join(chunk)})"
            _, data = m.search(None, "X-GM-RAW", f'"{query}"')
            for mid in data[0].split()[-50:]:
                _, msg_data = m.fetch(mid, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                sender = (parseaddr(msg["From"] or "")[1] or "").lower()
                text, html = _body_text(msg)
                body = text or BeautifulSoup(html, "html.parser").get_text(" ")
                if sender in [a.lower() for a in chunk]:
                    found[sender] = re.sub(r"\s+", " ", body).strip()[:300]
    return found


def _bounces(addresses: list[str], since_ts: float) -> set[str]:
    """Addresses that came back as undeliverable (mailer-daemon notices mentioning them)."""
    _check_config()
    bounced: set[str] = set()
    days = max(1, int((time.time() - since_ts) / 86400) + 1)
    with imaplib.IMAP4_SSL("imap.gmail.com") as m:
        m.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        m.select("INBOX")
        _, data = m.search(None, "X-GM-RAW", f'"newer_than:{days}d (from:mailer-daemon OR from:postmaster)"')
        for mid in data[0].split()[-50:]:
            _, msg_data = m.fetch(mid, "(RFC822)")
            text, html = _body_text(email.message_from_bytes(msg_data[0][1]))
            blob = (text + " " + html).lower()
            bounced.update(a for a in addresses if a.lower() in blob)
    return bounced


async def replies_from(addresses: list[str], since_ts: float) -> dict[str, str]:
    return await asyncio.to_thread(_messages_from, addresses, since_ts)


async def bounces_for(addresses: list[str], since_ts: float) -> set[str]:
    return await asyncio.to_thread(_bounces, addresses, since_ts)


# ---- verification mails ---------------------------------------------------

LINK_HINTS = ("verify", "confirm", "activate", "validation", "token", "registration", "reset", "password")
CODE_HINT_RE = re.compile(r"\b(code|otp|pin|passcode|verification|verify|one[- ]time)\b", re.I)
_NOT_CODES = {"verification", "application", "security", "password", "account", "following", "please",
              "submit", "address", "greenhouse", "workday", "minutes", "expires"}


def find_code(text: str) -> str | None:
    """Verification code near a 'code/OTP' word: digits (482913) or letters+digits (A7kQ92xZ)."""
    for m in CODE_HINT_RE.finditer(text):
        window = text[m.end():m.end() + 160]
        for tok in re.findall(r"\b[A-Za-z0-9]{4,10}\b", window):
            if tok.lower() in _NOT_CODES:
                continue
            has_digit = any(c.isdigit() for c in tok)
            mixed = any(c.isupper() for c in tok) and any(c.islower() for c in tok) and len(tok) >= 6
            if (tok.isdigit() and 4 <= len(tok) <= 8) or (has_digit and (mixed or tok.isupper() or len(tok) >= 6)) \
                    or (mixed and len(tok) == 8):
                return tok
    return None


def _body_text(msg: email.message.Message) -> tuple[str, str]:
    text, html = "", ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True) or b""
        decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain":
            text += decoded
        elif ctype == "text/html":
            html += decoded
    return text, html


def _find_verification(since_ts: float, hint: str, exclude: tuple = ()) -> dict | None:
    _check_config()
    with imaplib.IMAP4_SSL("imap.gmail.com") as m:
        m.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        m.select("INBOX")
        # Gmail's own search: only mails from the site or about codes/verification (a busy inbox can't hide them)
        days = max(1, int((time.time() - since_ts) / 86400) + 1)
        query = (f"newer_than:{days}d ({'from:' + hint + ' OR ' if hint else ''}subject:code OR subject:verification "
                 f"OR subject:verify OR subject:otp OR subject:confirm OR subject:activate OR subject:password OR subject:reset)")
        try:
            _, data = m.search(None, "X-GM-RAW", f'"{query}"')
        except imaplib.IMAP4.error:  # non-Gmail server: plain date search
            _, data = m.search(None, "SINCE", time.strftime("%d-%b-%Y", time.localtime(since_ts - 86400)))
        ids = data[0].split()[-20:]
        for mid in reversed(ids):
            _, msg_data = m.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            try:
                if parsedate_to_datetime(msg["Date"]).timestamp() < since_ts - 60:
                    continue
            except (TypeError, ValueError):
                continue
            sender, subject = (msg["From"] or "").lower(), (msg["Subject"] or "").lower()
            text, html = _body_text(msg)
            haystack = f"{sender} {subject}"
            if hint and hint not in haystack and not any(w in subject for w in ("verif", "confirm", "otp", "code", "activate", "password", "reset")):
                continue
            result = {"subject": msg["Subject"], "from": msg["From"]}
            code = find_code(BeautifulSoup(html, "html.parser").get_text(" ") if html else text)
            if code:
                result["code"] = code
            links = [a.get("href", "") for a in BeautifulSoup(html, "html.parser").find_all("a")] if html \
                else re.findall(r"https?://\S+", text)
            link = next((l for l in links if any(h in l.lower() for h in LINK_HINTS)), None)
            if link:
                result["link"] = link
            if result.get("code") in exclude:  # already tried this one - look for a newer email
                result.pop("code")
            if "code" in result or "link" in result:
                return result
    return None


async def wait_for_verification(hint: str, since_ts: float, timeout_s: int = 180,
                                exclude: list | tuple = ()) -> dict | None:
    """Poll Gmail for a verification link/code from a sender matching `hint` (e.g. site name)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        found = await asyncio.to_thread(_find_verification, since_ts, hint.lower(), tuple(exclude))
        if found:
            return found
        await asyncio.sleep(10)
    return None
