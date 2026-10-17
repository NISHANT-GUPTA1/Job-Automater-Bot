"""Control the bot's browser from your phone: a small private web page with a live screenshot.

Tap the screenshot to click, type text, press keys, scroll, go back, switch tabs. Reached through a free
Cloudflare quick tunnel (no account needed) with a secret token in the link; only your Telegram chat gets it.
"""
import asyncio
import logging
import re
import secrets
import shutil

from aiohttp import web

from . import config
from .browser import browser

log = logging.getLogger(__name__)

TOKEN = secrets.token_urlsafe(24)
PORT = int(config.env("REMOTE_PORT", "8765"))
_tunnel: asyncio.subprocess.Process | None = None
_tab = {"index": -1}  # which tab is shown (-1 = newest)
_stopping = False

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=yes">
<title>Job Applier - browser</title>
<style>
 body{margin:0;font-family:system-ui,sans-serif;background:#111;color:#eee}
 #bar{position:sticky;top:0;background:#1d1d1d;padding:6px;display:flex;flex-wrap:wrap;gap:6px;z-index:2}
 button{background:#2f6fde;color:#fff;border:0;border-radius:8px;padding:9px 11px;font-size:15px}
 button.g{background:#444} input{flex:1;min-width:140px;padding:9px;border-radius:8px;border:0;font-size:16px}
 #wrap{position:relative} #shot{width:100%;display:block;touch-action:manipulation}
 #dot{position:absolute;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;background:#ff3b30aa;display:none}
 #info{font-size:12px;color:#aaa;padding:4px 8px;word-break:break-all}
</style></head><body>
<div id="bar">
 <input id="txt" placeholder="Text to type into the focused field" autocomplete="off">
 <button onclick="act({type:'type',text:txt.value});txt.value=''">Type</button>
 <button class="g" onclick="act({type:'key',key:'Enter'})">Enter</button>
 <button class="g" onclick="act({type:'key',key:'Tab'})">Tab</button>
 <button class="g" onclick="act({type:'key',key:'Backspace'})">&#9003;</button>
 <button class="g" onclick="act({type:'scroll',dy:-600})">&#9650;</button>
 <button class="g" onclick="act({type:'scroll',dy:600})">&#9660;</button>
 <button class="g" onclick="act({type:'back'})">Back</button>
 <button class="g" onclick="act({type:'reload'})">Reload</button>
 <button class="g" onclick="act({type:'tab'})">Next tab</button>
</div>
<div id="info">Loading…</div>
<div id="wrap"><img id="shot" alt=""><div id="dot"></div></div>
<script>
const base = location.pathname.replace(/\\/$/, '');
let busy = false;
async function refresh(){
  if (busy) return; busy = true;
  try {
    const r = await fetch(base + '/shot?t=' + Date.now());
    if (r.status === 200) { const b = await r.blob(); shot.src = URL.createObjectURL(b);
      info.textContent = decodeURIComponent(r.headers.get('X-Url') || ''); }
    else info.textContent = await r.text();
  } catch(e) { info.textContent = 'Connection lost - retrying…'; }
  busy = false;
}
async function act(a){
  await fetch(base + '/act', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(a)});
  setTimeout(refresh, 350); setTimeout(refresh, 1300);
}
shot.addEventListener('click', e => {
  const r = shot.getBoundingClientRect();
  const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
  dot.style.left = (e.clientX - r.left) + 'px'; dot.style.top = (e.clientY - r.top) + 'px';
  dot.style.display = 'block'; setTimeout(() => dot.style.display = 'none', 600);
  act({type:'click', x: fx, y: fy});
});
txt.addEventListener('keydown', e => { if (e.key === 'Enter') { act({type:'type',text:txt.value}); txt.value=''; } });
refresh(); setInterval(refresh, 1500);
</script></body></html>"""


def _page():
    if not browser.ctx or not browser.ctx.pages:
        return None
    pages = browser.ctx.pages
    return pages[_tab["index"] % len(pages)]


def _check(request: web.Request) -> None:
    if not secrets.compare_digest(request.match_info.get("token", ""), TOKEN):
        raise web.HTTPNotFound()


async def page_view(request: web.Request) -> web.Response:
    _check(request)
    return web.Response(text=PAGE, content_type="text/html")


async def shot(request: web.Request) -> web.Response:
    _check(request)
    page = _page()
    if not page:
        return web.Response(status=503, text="The bot's browser isn't open right now (it opens while applying).")
    try:
        img = await page.screenshot(type="jpeg", quality=60, timeout=8000)
    except Exception as e:
        return web.Response(status=503, text=f"Screenshot failed: {str(e)[:120]}")
    return web.Response(body=img, content_type="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Url": page.url[:300].encode("ascii", "ignore").decode()})


async def act(request: web.Request) -> web.Response:
    _check(request)
    page = _page()
    if not page:
        return web.json_response({"ok": False})
    a = await request.json()
    try:
        if a["type"] == "click":
            w, h = await page.evaluate("[window.innerWidth, window.innerHeight]")
            await page.mouse.click(float(a["x"]) * w, float(a["y"]) * h)
        elif a["type"] == "type":
            await page.keyboard.type(str(a.get("text", ""))[:500], delay=30)
        elif a["type"] == "key" and a.get("key") in ("Enter", "Tab", "Backspace", "Escape"):
            await page.keyboard.press(a["key"])
        elif a["type"] == "scroll":
            await page.mouse.wheel(0, int(a.get("dy", 600)))
        elif a["type"] == "back":
            await page.go_back(timeout=10000)
        elif a["type"] == "reload":
            await page.reload(timeout=20000)
        elif a["type"] == "tab":
            _tab["index"] = (_tab["index"] + 1) % max(1, len(browser.ctx.pages))
    except Exception as e:
        log.info("remote action %s failed: %s", a.get("type"), e)
        return web.json_response({"ok": False, "error": str(e)[:200]})
    return web.json_response({"ok": True})


_PINNED = config.DATA_DIR / "pinned_link_msg.txt"


async def _announce() -> None:
    """Send the (new) phone-control link and pin it at the top of the chat, replacing the old pin."""
    from .asker import asker
    if not asker.bot:
        return
    try:
        mid = await asker.say("🖥 Phone control for the bot's browser — tap the button whenever I need you "
                              "(captchas, logins, codes, stuck pages) or to watch it work.\n"
                              "Pinned here; a fresh link replaces it after every restart. Keep it private.",
                              browser=True, disable_notification=True)
        old = _PINNED.read_text().strip() if _PINNED.exists() else ""
        await asker.bot.pin_chat_message(asker.chat_id, mid, disable_notification=True)
        if old.isdigit():
            try:
                await asker.bot.unpin_chat_message(asker.chat_id, message_id=int(old))
            except Exception:
                pass  # old pin already gone
        _PINNED.write_text(str(mid))
    except Exception as e:
        log.info("could not send/pin the phone-control link: %s", e)


def _cloudflared() -> str | None:
    local = config.ROOT / "tools" / "cloudflared" / "cloudflared.exe"
    return str(local) if local.exists() else shutil.which("cloudflared")


async def _run_tunnel() -> None:
    """Keep a Cloudflare quick tunnel up; publish its URL as the 'Open the bot's browser' link."""
    global _tunnel
    exe = _cloudflared()
    if not exe:
        log.warning("cloudflared not found - phone browser control disabled (see README)")
        return
    # a previous bot run may have left its tunnel behind (e.g. stopped via Task Scheduler): close it
    import subprocess, sys
    if sys.platform == "win32":
        await asyncio.to_thread(subprocess.run, ["powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | Where-Object { $_.CommandLine -like "
            f"'*127.0.0.1:{PORT}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
            capture_output=True, timeout=60)
    else:
        await asyncio.to_thread(subprocess.run, ["pkill", "-f", f"cloudflared.*127.0.0.1:{PORT}"], capture_output=True)
    while not _stopping:
        _tunnel = await asyncio.create_subprocess_exec(
            exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        async for raw in _tunnel.stderr:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", raw.decode(errors="ignore"))
            if m:
                config.BROWSER_VIEW_URL = f"{m.group(0)}/r/{TOKEN}"
                log.info("Phone browser control ready")
                asyncio.get_running_loop().create_task(_announce())
        await _tunnel.wait()
        config.BROWSER_VIEW_URL = ""
        if _stopping:
            break
        log.warning("Cloudflare tunnel stopped - restarting in 15s")
        await asyncio.sleep(15)


async def start() -> None:
    if config.env_bool("REMOTE_CONTROL", True) is False:
        return
    app = web.Application()
    app.router.add_get("/r/{token}", page_view)
    app.router.add_get("/r/{token}/shot", shot)
    app.router.add_post("/r/{token}/act", act)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()  # local only; the tunnel is the way in
    asyncio.get_running_loop().create_task(_run_tunnel())


async def stop() -> None:
    global _stopping
    _stopping = True
    if _tunnel and _tunnel.returncode is None:
        _tunnel.kill()
