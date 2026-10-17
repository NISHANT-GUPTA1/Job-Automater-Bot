"""Captcha handling: ignore invisible ones, auto-pass what we can, relay image puzzles to Telegram.

Order of attempts:
1. Invisible reCAPTCHA/hCaptcha and solved widgets are ignored (they never block a person).
2. Cloudflare / Turnstile: wait for the automatic check, click its checkbox if shown.
3. "I'm not a robot" checkboxes are clicked - with a persistent, real browser profile this
   often passes without any puzzle.
4. Image puzzles are screenshotted with numbered tiles and sent to Telegram; you reply with
   the tile numbers (e.g. "1 4 7") from your phone and the bot clicks them.
5. Last resort: solve it in the open browser window.
"""
import logging
import re
import time

from . import config
from .asker import asker

log = logging.getLogger(__name__)

RECAPTCHA = ("recaptcha/api2/", "recaptcha/enterprise/")
BADGE_JS = r"""
(sel) => {
  const tiles = [...document.querySelectorAll(sel)];
  tiles.forEach((t, i) => {
    const b = document.createElement('div');
    b.className = 'ja-badge'; b.textContent = i + 1;
    b.style.cssText = 'position:absolute;top:3px;left:3px;z-index:2147483647;background:#ffeb3b;color:#000;' +
      'font:bold 18px Arial;padding:1px 6px;border-radius:4px;border:1px solid #000;pointer-events:none';
    if (getComputedStyle(t).position === 'static') t.style.position = 'relative';
    t.appendChild(b);
  });
  return tiles.length;
}"""
UNBADGE_JS = "() => document.querySelectorAll('.ja-badge').forEach(b => b.remove())"

# kind -> (tile selector, instructions selector, verify button, reload button)
CHALLENGES = {
    "recaptcha": ("td.rc-imageselect-tile", ".rc-imageselect-instructions",
                  "#recaptcha-verify-button", "#recaptcha-reload-button"),
    "hcaptcha": (".task-image, .task-grid .task", ".prompt-text, .challenge-prompt",
                 ".button-submit", ".refresh, .refresh-button"),
}


async def _visible(frame) -> bool:
    try:
        el = await frame.frame_element()
        if not await el.is_visible():
            return False
        box = await el.bounding_box()
        return bool(box and box["width"] > 30 and box["height"] > 30 and box["y"] > -500)
    except Exception:
        return False


async def _checked(frame, selector: str) -> bool:
    try:
        return await frame.locator(selector).first.get_attribute("aria-checked", timeout=1500) == "true"
    except Exception:
        return False


async def detect(page) -> dict | None:
    """Returns the blocking captcha on the page, or None. Invisible/solved ones are ignored."""
    title = (await page.title()).lower()
    if "just a moment" in title or "attention required" in title:
        return {"kind": "cloudflare", "frame": None, "challenge": True}
    frames = [f for f in page.frames if not f.is_detached()]
    # An open puzzle wins over its (still unchecked) checkbox
    for frame in frames:
        url = frame.url
        if any(r in url for r in RECAPTCHA) and "/bframe" in url and await _visible(frame):
            return {"kind": "recaptcha", "frame": frame, "challenge": True}
        if "hcaptcha" in url and "frame=challenge" in url and await _visible(frame):
            return {"kind": "hcaptcha", "frame": frame, "challenge": True}
    for frame in frames:
        url = frame.url
        if any(r in url for r in RECAPTCHA):
            if "/anchor" in url and "size=invisible" not in url and await _visible(frame) \
                    and not await _checked(frame, "#recaptcha-anchor"):
                return {"kind": "recaptcha", "frame": frame, "challenge": False}
        elif "hcaptcha" in url:
            if "frame=checkbox" in url and "size=invisible" not in url and await _visible(frame) \
                    and not await _checked(frame, "#checkbox"):
                return {"kind": "hcaptcha", "frame": frame, "challenge": False}
        elif "challenges.cloudflare.com" in url and await _visible(frame):
            try:
                solved = await page.evaluate(
                    "() => [...document.querySelectorAll('input[name=\"cf-turnstile-response\"]')].some(i => i.value)")
            except Exception:
                solved = False
            if not solved:
                return {"kind": "turnstile", "frame": frame, "challenge": False}
    return None


async def _wait_clear(page, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        await page.wait_for_timeout(1500)
        cap = await detect(page)
        if not cap:
            return True
        if cap["challenge"] and cap["kind"] in ("recaptcha", "hcaptcha"):
            return False  # a puzzle appeared
    return not await detect(page)


async def _click_checkbox(page, cap: dict) -> None:
    frame = cap["frame"]
    try:
        if cap["kind"] == "recaptcha":
            await frame.locator("#recaptcha-anchor").click(timeout=5000)
        elif cap["kind"] == "hcaptcha":
            await frame.locator("#checkbox").click(timeout=5000)
        elif cap["kind"] == "turnstile":
            box = await (await frame.frame_element()).bounding_box()
            if box:  # the checkbox sits in a closed shadow root; click its position
                await page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
    except Exception as e:
        log.info("captcha checkbox click failed: %s", e)


async def _relay_puzzle(page, cap: dict, job_id: int) -> str:
    """Send the image puzzle to Telegram and click the tiles the user names.
    Returns 'solved', 'browser' or 'skip'."""
    tile_sel, instr_sel, verify_sel, reload_sel = CHALLENGES[cap["kind"]]
    for round_no in range(8):
        cap = await detect(page)
        if not cap:
            return "solved"
        if not cap["challenge"]:
            await _click_checkbox(page, cap)
            if await _wait_clear(page, 6):
                return "solved"
            cap = await detect(page)
            if not cap or not cap["challenge"]:
                continue
        frame = cap["frame"]
        await page.wait_for_timeout(1200)  # let images load
        try:
            n = await frame.evaluate(BADGE_JS, tile_sel)
            instructions = (await frame.locator(instr_sel).first.inner_text(timeout=3000)).strip()
        except Exception:
            n, instructions = 0, ""
        shot = config.SCREENSHOT_DIR / f"job{job_id}_captcha_{int(time.time())}.png"
        await (await frame.frame_element()).screenshot(path=str(shot))
        try:
            await frame.evaluate(UNBADGE_JS)
        except Exception:
            pass
        if not n:
            return "browser"  # not a tile grid (e.g. drag puzzle)

        instructions = re.sub(r"\s+", " ", instructions)
        await asker.send_photo(shot, f"🧩 Job #{job_id} captcha: {instructions}")
        answer = await asker.ask(
            f"Reply with the tile numbers to click (e.g. 1 4 7), or 0 if none match.",
            ["🔄 New puzzle", "🖥 I'll solve it in the browser", "⏭ Skip job"], timeout_min=30, browser=True)
        if not answer or answer.startswith("⏭"):
            return "skip"
        if answer.startswith("🖥"):
            return "browser"
        if answer.startswith("🔄"):
            try:
                await frame.locator(reload_sel).first.click(timeout=4000)
            except Exception:
                pass
            continue

        tiles = frame.locator(tile_sel)
        for num in {int(x) for x in re.findall(r"\d+", answer)}:
            if 1 <= num <= n:
                try:
                    await tiles.nth(num - 1).click(timeout=4000)
                    await page.wait_for_timeout(250)
                except Exception as e:
                    log.info("tile %s click failed: %s", num, e)
        # Dynamic reCAPTCHA ("click verify once there are none left") replaces tiles: ask again
        if "none left" in instructions.lower() and re.search(r"[1-9]", answer):
            await page.wait_for_timeout(4000)
            continue
        try:
            await frame.locator(verify_sel).first.click(timeout=5000)
        except Exception:
            pass
        if await _wait_clear(page, 5):
            await asker.say("✅ Captcha solved, continuing…")
            return "solved"
    return "browser"


async def solve(page, job_id: int) -> str:
    """Make the page captcha-free. Returns 'solved', 'skip' (user chose to skip) or 'failed'."""
    cap = await detect(page)
    if not cap:
        return "solved"
    log.info("job %s captcha detected: %s challenge=%s", job_id, cap["kind"], cap["challenge"])

    # 1-3: automatic attempts
    if cap["kind"] in ("cloudflare", "turnstile"):
        if await _wait_clear(page, 12):
            return "solved"
        cap = await detect(page)
        if cap and cap["kind"] == "turnstile":
            await _click_checkbox(page, cap)
            if await _wait_clear(page, 10):
                return "solved"
    elif not cap["challenge"]:
        await _click_checkbox(page, cap)
        if await _wait_clear(page, 6):
            log.info("job %s captcha passed with checkbox click", job_id)
            return "solved"

    # 4: relay image puzzle to Telegram
    cap = await detect(page)
    if not cap:
        return "solved"
    result = "browser"
    if cap["kind"] in CHALLENGES:
        result = await _relay_puzzle(page, cap, job_id)
        if result in ("solved", "skip"):
            return result

    # 5: solve in the browser window
    if config.HEADLESS:
        return "failed"
    shot = config.SCREENSHOT_DIR / f"job{job_id}_captcha_page_{int(time.time())}.png"
    await page.screenshot(path=str(shot))
    await asker.send_photo(shot, f"🧩 Job #{job_id}: captcha needs you in the browser.", browser=True)
    choice = await asker.ask("Solve only the captcha in the browser — I'll keep filling everything else.",
                             ["✅ Solved", "⏭ Skip job"], timeout_min=30, browser=True)
    if not choice or choice.startswith("⏭"):
        return "skip"
    return "solved" if await _wait_clear(page, 3) else "failed"
