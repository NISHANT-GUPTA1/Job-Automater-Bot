"""LinkedIn in a real browser, using the session you logged in with once (`python linkedin_login.py`).

Nothing here stores or types your password. The browser window opens off-screen so it never gets in your way.
Every action is slow and spaced out like a person's, and anything unusual (login wall, security check, weekly
invitation limit) stops LinkedIn work and tells you instead of retrying.
"""
import asyncio
import logging
import random
import re
from contextlib import asynccontextmanager
from urllib.parse import unquote

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

from . import config, db

log = logging.getLogger(__name__)
PROFILE_DIR = config.DATA_DIR / "linkedin_profile"
_lock = asyncio.Lock()


class LinkedInStop(RuntimeError):
    """Logged out, security check or a LinkedIn limit: stop and tell the user."""


def has_session() -> bool:
    """True once linkedin_login.py saw you signed in (and LinkedIn hasn't logged you out since)."""
    return db.get_setting("li_session") == "1"


def slug(url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", unquote(url or ""), re.I)
    return m.group(1).lower().strip("/") if m else ""


def profile_url(url_or_handle: str) -> str:
    s = slug(url_or_handle) or (url_or_handle or "").strip().strip("/").split("/")[-1]
    return f"https://www.linkedin.com/in/{s}/" if s else ""


async def pause(a: float = 1.5, b: float = 4.0) -> None:
    await asyncio.sleep(random.uniform(a, b))


@asynccontextmanager
async def session(visible: bool = False):
    """A browser with your saved LinkedIn login. One at a time."""
    async with _lock:
        pw = await async_playwright().start()
        args = ["--disable-blink-features=AutomationControlled"]
        if not visible:  # a real (not headless) browser, parked off-screen
            args += ["--window-position=-2400,-2400", "--window-size=1280,900"]
        ctx = await pw.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False, args=args,
                                                          viewport={"width": 1280, "height": 900})
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            yield page
        finally:
            await ctx.close()
            await pw.stop()


async def _check(page: Page) -> None:
    url = page.url.lower()
    if any(k in url for k in ("/login", "/authwall", "/checkpoint", "/uas/")):
        db.set_setting("li_session", "0")
        raise LinkedInStop("LinkedIn wants you to sign in again / pass a security check. "
                           "On the laptop run:  .venv\\Scripts\\python linkedin_login.py")
    try:
        text = (await page.locator("body").inner_text(timeout=25000))[:20000].lower()
    except PWTimeout:  # page still settling: the next step will find out if something's wrong
        return
    if "weekly invitation limit" in text or "you've reached the weekly" in text:
        raise LinkedInStop("LinkedIn's weekly invitation limit is reached - invites pause for a week.")
    if "restricted" in text and "account" in text and "temporarily" in text:
        raise LinkedInStop("LinkedIn says your account is temporarily restricted - all LinkedIn actions stopped.")


async def goto(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await pause(2.5, 5)
    await _check(page)
    await page.mouse.wheel(0, random.randint(200, 600))  # glance down the page like a person
    await pause(1, 2.5)
    await page.mouse.wheel(0, -1000)


async def logged_in(page: Page) -> bool:
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
    await pause(2, 4)
    return "/feed" in page.url


def _top(page: Page):
    """The profile's top card (name, headline, Connect/Message/More buttons)."""
    return page.locator("main section").first


async def _visible(locator) -> bool:
    try:
        return await locator.first.is_visible(timeout=1500)
    except PWTimeout:
        return False


async def profile_state(page: Page) -> str:
    """On a profile page: 'connected' (1st degree), 'pending' (your request is waiting) or 'not_connected'."""
    top = _top(page)
    text = await top.inner_text(timeout=15000)
    if await _visible(top.locator("[aria-label^='Pending']")) or re.search(r"(^|\n)\s*Pending\s*($|\n)", text):
        return "pending"
    if re.search(r"·\s*1st\b", text):
        return "connected"
    return "not_connected"


async def _open_invite(page: Page, handle: str) -> bool:
    """Open the 'Add a note to your invitation?' dialog: Connect on the card, or More -> Connect, else its link."""
    top = _top(page)
    connect = top.get_by_role("link", name=re.compile(r"^Connect$", re.I)).or_(
        top.get_by_role("button", name=re.compile(r"Invite .* to connect|^Connect$", re.I)))
    if not await _visible(connect):
        more = top.get_by_role("button", name=re.compile(r"^More$", re.I))
        if await _visible(more):
            await more.first.click()
            await pause(1, 2)
            connect = page.get_by_role("menuitem", name=re.compile(r"^Connect$", re.I))
    if await _visible(connect):
        await connect.first.click()
    else:
        await page.keyboard.press("Escape")
        await page.goto(f"https://www.linkedin.com/preload/custom-invite/?vanityName={handle}",
                        wait_until="domcontentloaded", timeout=45000)
    await pause(2, 4)
    return await _visible(page.get_by_role("dialog"))


async def invite(page: Page, url: str, note: str, email: str = "", allow_note: bool = True) -> str:
    """Send a connection request and confirm it on the profile. Returns 'invited' (with note), 'invited_no_note',
    'pending' / 'connected' (nothing to do), 'notes_used_up' (nothing sent) or 'failed:<why>'."""
    await goto(page, url)
    state = await profile_state(page)
    if state != "not_connected":
        return state
    if not await _open_invite(page, slug(url)):
        await _check(page)
        return "failed:the invite dialog didn't open"
    dialog = page.get_by_role("dialog").first
    if await _visible(dialog.locator("input[type=email]")):  # "how do you know X?" - needs their email
        if not email:
            await page.keyboard.press("Escape")
            return "failed:LinkedIn asks for their email to connect"
        await dialog.locator("input[type=email]").first.fill(email)
    with_note = False
    add_note = dialog.get_by_role("button", name=re.compile(r"Add a (free )?note", re.I))
    if allow_note and note and await _visible(add_note):
        await add_note.first.click()
        await pause(1, 2)
        box = page.get_by_role("dialog").locator("textarea")
        if not await _visible(box):  # an upsell instead of a text box: free notes used up this month
            await page.keyboard.press("Escape")
            return "notes_used_up"
        await box.first.type(note[:200], delay=random.randint(25, 60))
        with_note = True
    send = page.get_by_role("dialog").get_by_role(
        "button", name=re.compile(r"^Send( invitation| now)?$" if with_note else r"^Send without a note$|^Send$", re.I))
    if not await _visible(send):
        await page.keyboard.press("Escape")
        return "failed:no Send button in the invite dialog"
    await pause(1, 2)
    await send.first.click()
    await pause(3, 5)
    await _check(page)
    await goto(page, url)  # only trust what the profile shows afterwards
    if await profile_state(page) in ("pending", "connected"):
        return "invited" if with_note else "invited_no_note"
    return "failed:LinkedIn didn't show the request as pending"


async def recent_connections(page: Page, scrolls: int = 8) -> set[str]:
    """Profile handles of your most recent connections (newest first on that page)."""
    await goto(page, "https://www.linkedin.com/mynetwork/invite-connect/connections/")
    for _ in range(scrolls):
        await page.mouse.wheel(0, 2500)
        await pause(1.5, 3)
    hrefs = await page.locator("main a[href*='/in/']").evaluate_all("els => els.map(e => e.href)")
    return {slug(h) for h in hrefs if slug(h)}


async def pending_invites(page: Page) -> set[str]:
    """Handles of people whose request from you is still waiting."""
    await goto(page, "https://www.linkedin.com/mynetwork/invitation-manager/sent/")
    for _ in range(6):
        await page.mouse.wheel(0, 2500)
        await pause(1.5, 3)
    hrefs = await page.locator("main a[href*='/in/']").evaluate_all("els => els.map(e => e.href)")
    return {slug(h) for h in hrefs if slug(h)}


async def send_message(page: Page, url: str, text: str, attachment: str | None = None) -> str:
    """Message a 1st-degree connection (optionally with a PDF) and confirm it shows in the conversation.
    'sent' or 'failed:<why>'."""
    await goto(page, url)
    if await profile_state(page) != "connected":
        return "failed:not connected yet"
    link = _top(page).get_by_role("link", name=re.compile(r"^Message$", re.I)).or_(
        _top(page).get_by_role("button", name=re.compile(r"^Message", re.I)))
    if not await _visible(link):
        return "failed:no Message button on their profile"
    href = await link.first.get_attribute("href")
    if href:  # LinkedIn's compose page: one clean message box
        await page.goto(("https://www.linkedin.com" + href) if href.startswith("/") else href,
                        wait_until="domcontentloaded", timeout=45000)
    else:
        await link.first.click()
    await pause(3, 5)
    await _check(page)
    box = page.locator("div.msg-form__contenteditable[role=textbox], div[role=textbox][contenteditable=true]").last
    if not await _visible(box):
        return "failed:the message box didn't open"
    await box.click()
    for i, line in enumerate(text.splitlines()):
        if i:
            await page.keyboard.press("Shift+Enter")
        await page.keyboard.type(line, delay=random.randint(10, 30))
    await pause(1, 2)
    if attachment:
        files = page.locator("input[type=file][accept*='.pdf']")
        if not await files.count():
            return "failed:couldn't find where to attach the resume"
        await files.last.set_input_files(attachment)
        name = attachment.replace("\\", "/").split("/")[-1]
        try:  # wait until the file shows in the draft
            await page.get_by_text(name.rsplit(".", 1)[0], exact=False).last.wait_for(timeout=30000)
        except PWTimeout:
            return "failed:the resume didn't finish uploading"
        await pause(2, 3)
    send = page.locator("button.msg-form__send-button").last
    try:
        await send.wait_for(state="visible", timeout=10000)
        for _ in range(20):  # enabled once the text (and upload) is ready
            if await send.is_enabled():
                break
            await asyncio.sleep(1)
        await send.click()
    except PWTimeout:
        return "failed:no Send button"
    await pause(3, 5)
    first_words = " ".join(text.split()[:6])[:40]
    sent = page.locator(".msg-s-event-listitem, .msg-s-message-list__event").filter(has_text=first_words)
    if not await _visible(sent):
        return "failed:the message didn't show up in the conversation after sending"
    return "sent"


async def conversations(page: Page, scrolls: int = 2) -> list[dict]:
    """Latest conversations: [{text, from_me}] - text holds the person's name, date and last-message preview."""
    await goto(page, "https://www.linkedin.com/messaging/")
    for _ in range(scrolls):
        await page.locator("main").first.hover()
        await page.mouse.wheel(0, 1500)
        await pause(1, 2)
    items = await page.locator("main li").evaluate_all("els => els.map(e => e.innerText)")
    out = []
    for t in items:
        t = re.sub(r"\s+\n", "\n", t or "").strip()
        if t and len(t) < 600:
            out.append({"text": t, "from_me": bool(re.search(r"(^|\n)You:", t))})
    return out
