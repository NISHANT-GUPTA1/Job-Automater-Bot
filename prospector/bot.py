"""Prospector Telegram bot: daily cold-outreach batch to people at small companies that are hiring."""
import asyncio
import json
import logging
import random
import re
from datetime import datetime

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from pathlib import Path

from jobapplier import db as jobdb, jobs
from jobapplier.llm import llm

from . import apollo, config, db, engine, sheet

log = logging.getLogger(__name__)

HELP = """🎯 Prospector — cold outreach to small companies that are hiring

Every day at {hour}:00 I prepare up to {limit} emails (new people + follow-ups) and send them here for review.
I pick companies with 1-50 employees hiring for software / AI / backend / full-stack roles or interns,
find HR or the hiring inbox first (then engineering managers, then founders) and write to them.

Send me companies any time: links (company site, Wellfound, LinkedIn), names, a list, or screenshots.

/run — prepare today's batch now
/drafts — show today's drafts again
/add <company or website> — add a company
/stats — numbers so far      /export — Excel sheet
/companies — recent companies
/stage 1|2|3 — company size: 1-50, 51-200, 201-500
/pause, /resume — stop or restart the daily batch
/check — what your Apollo plan allows"""

JOB_BOARDS = ("wellfound.com", "angel.co", "linkedin.com", "naukri.com", "instahyre.com", "glassdoor", "indeed.",
              "cutshort.io", "internshala.com", "ycombinator.com", "workatastartup.com")
_state: dict = {"bot": None, "editing": None, "sending": False}
send_queue: asyncio.Queue = asyncio.Queue()


def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if not config.TELEGRAM_CHAT_ID or str(chat.id) != config.TELEGRAM_CHAT_ID:
            if update.message:
                await update.message.reply_text(f"Your chat id is {chat.id}. Put TELEGRAM_CHAT_ID={chat.id} in .env.")
            return
        return await func(update, ctx)
    return wrapper


async def say(text: str, **kwargs):
    bot = _state["bot"]
    for i in range(0, len(text), 4000):
        msg = await bot.send_message(int(config.TELEGRAM_CHAT_ID), text[i:i + 4000],
                                     disable_web_page_preview=True, **kwargs)
    return msg


# ---- daily batch ---------------------------------------------------------------------------

def draft_text(m) -> str:
    c = db.q1("SELECT * FROM contacts WHERE id=?", (m["contact_id"],))
    co = db.q1("SELECT * FROM companies WHERE id=?", (c["company_id"],))
    roles = ", ".join(r["title"] for r in json.loads(co["roles_json"] or "[]")[:3]) or "no public listing"
    kind = "↪️ Follow-up" if m["kind"] == "followup" else "✉️ New"
    attach = ""
    if m["kind"] == "initial":
        attach = f"📎 {Path(m['attachment'] or engine.resume_file()).name} — {m['attach_note'] or 'your base resume'}\n"
    return (f"{kind} — {c['name']} ({c['title'] or '?'}) @ {co['name']}\n"
            f"Hiring: {roles}\nTo: {c['email']}\n{attach}Subject: {m['subject']}\n\n{m['body']}")


def draft_buttons(mid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Send", callback_data=f"p|send|{mid}"),
                                  InlineKeyboardButton("✏️ Edit", callback_data=f"p|edit|{mid}"),
                                  InlineKeyboardButton("🗑 Drop", callback_data=f"p|drop|{mid}")]])


async def show_batch(ids: list[int], cost_line: str = "") -> None:
    rows = [db.q1("SELECT * FROM messages WHERE id=?", (i,)) for i in ids]
    rows = [r for r in rows if r and r["status"] in ("draft", "approved")]
    if not rows:
        await say("📭 Nothing to send today. Send me some companies, or check /stats." + (f"\n{cost_line}" if cost_line else ""))
        return
    fups = sum(r["kind"] == "followup" for r in rows)
    await say(f"📬 Today's batch: {len(rows)} emails ({len(rows) - fups} new, {fups} follow-ups). "
              f"Review below, then send." + (f"\n{cost_line}" if cost_line else ""),
              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🚀 Send all {len(rows)}", callback_data="p|all|0"),
                                                  InlineKeyboardButton("❌ Skip today", callback_data="p|skipday|0")]]))
    shown_files = set()
    for r in rows:
        msg = await say(draft_text(r), reply_markup=draft_buttons(r["id"]))
        db.update("messages", r["id"], tg_message_id=msg.message_id)
        if (r["attach_note"] or "").startswith("new") and r["attachment"] not in shown_files:
            shown_files.add(r["attachment"])  # a newly made resume: let you check it before it goes out
            with open(r["attachment"], "rb") as f:
                await _state["bot"].send_document(int(config.TELEGRAM_CHAT_ID), f,
                                                  caption=f"📎 New resume for the email above: {r['attach_note']}"[:1000])


async def daily_batch() -> None:
    db.set_setting("last_batch_date", engine.today())
    await say("☀️ Preparing today's outreach…")
    llm_before = jobdb.q1("SELECT COALESCE(MAX(id),0) m FROM llm_usage")["m"]
    apollo_before = db.credits_this_month()
    hunter_before = db.credits_this_month("hunter:search")
    try:
        ids = await engine.prepare_batch(progress=lambda t: asyncio.create_task(say(t)))
    except Exception as e:
        log.exception("batch failed")
        await say(f"❌ Couldn't prepare today's batch: {str(e)[:300]}")
        return
    ai = jobdb.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM llm_usage WHERE id>?", (llm_before,))["c"]
    cost_line = (f"💸 Preparing it cost: AI {engine.money(ai)} · Apollo credits {db.credits_this_month() - apollo_before} "
                 f"({db.credits_this_month()}/{config.APOLLO_MONTHLY_CREDITS} this month) · Hunter searches "
                 f"{db.credits_this_month('hunter:search') - hunter_before} "
                 f"({db.credits_this_month('hunter:search')}/{config.HUNTER_MONTHLY_SEARCHES})")
    await show_batch(ids, cost_line)


async def sender() -> None:
    """Sends approved emails one by one with a natural pause, never above the daily limit."""
    while True:
        mid = await send_queue.get()
        try:
            m = db.q1("SELECT * FROM messages WHERE id=?", (mid,))
            if not m or m["status"] != "approved":
                continue
            if engine.sent_today() >= config.DAILY_LIMIT:
                db.update("messages", mid, status="draft", batch_date=None)
                await say(f"⏸ Daily limit of {config.DAILY_LIMIT} reached — the rest goes out tomorrow.")
                continue
            result = await engine.send(mid)
            c = db.q1("SELECT ct.name, co.name company FROM contacts ct JOIN companies co ON co.id=ct.company_id "
                      "WHERE ct.id=?", (m["contact_id"],))
            await say(f"{'📨' if result == 'sent' else '⚠️'} {c['name']} @ {c['company']}: {result} "
                      f"({engine.sent_today()}/{config.DAILY_LIMIT} today)")
            if not send_queue.empty():
                await asyncio.sleep(random.uniform(*config.SEND_GAP_MINUTES) * 60)
        except Exception as e:
            log.exception("send failed")
            await say(f"❌ Sending failed: {str(e)[:200]}")
        finally:
            send_queue.task_done()


async def approve(mid: int) -> None:
    db.update("messages", mid, status="approved")
    await send_queue.put(mid)


async def scheduler() -> None:
    """Daily batch at SEND_HOUR, reply/bounce check every 30 minutes."""
    last_reply_check = 0.0
    while True:
        try:
            now = datetime.now()
            if (now.hour >= config.SEND_HOUR and db.get_setting("last_batch_date") != engine.today()
                    and db.get_setting("paused") != "1"):
                await daily_batch()
            if now.timestamp() - last_reply_check > 1800:
                last_reply_check = now.timestamp()
                for r in await engine.check_replies():
                    await say(f"💬 Reply from {r['name']} ({r['title']}) @ {r['company']} <{r['email']}>:\n"
                              f"“{r['snippet']}”\n\nFollow-ups to them are stopped. Reply from your Gmail.")
        except Exception:
            log.exception("scheduler step failed")
        await asyncio.sleep(60)


# ---- adding companies -------------------------------------------------------------------------

EXTRACT_PROMPT = """Extract the companies mentioned in this message (names and websites if given).
<message>
{text}
</message>
Return JSON: {{"companies": [{{"name": str, "website": str}}]}}"""


async def add_companies(update: Update, items: list[dict]) -> None:
    lines, ask_site = [], []
    for it in items:
        name, site = (it.get("name") or "").strip(), (it.get("website") or "").strip()
        domain = engine.domain_of(site) if site and not any(b in site for b in JOB_BOARDS) else ""
        if not domain and name:
            domain = await engine.resolve_domain(name) or ""
        if not domain:
            ask_site.append(name or site)
            continue
        cid, created = engine.add_company(name or domain, domain)
        if not created:
            lines.append(f"• {name or domain} — already tracked")
            continue
        try:
            people = await engine.research(cid)
        except apollo.ApolloBlocked as e:
            lines.append(f"• {name or domain} ({domain}) — added; Apollo people search blocked ({str(e)[:60]})")
            continue
        co = db.q1("SELECT roles_json FROM companies WHERE id=?", (cid,))
        roles = ", ".join(r["title"] for r in json.loads(co["roles_json"] or "[]")[:3]) or "no public listing"
        lines.append(f"• {name or domain} ({domain}) — "
                     + (f"{people} contact ready" if people else "no usable email found yet (or too big)")
                     + f" · hiring: {roles}")
    text = "🏢 Added:\n" + "\n".join(lines) if lines else ""
    if ask_site:
        text += ("\n\n❓ I couldn't find the website for: " + ", ".join(ask_site) +
                 "\nSend it like: /add Acme acme.com")
    await update.effective_message.reply_text(text.strip() or "No companies found in that message.",
                                              disable_web_page_preview=True)
    if lines:
        await update.effective_message.reply_text("They'll be included in the next daily batch (or /run now).")


def items_from_urls(urls: list[str]) -> list[dict]:
    out = []
    for u in urls:
        m = re.search(r"(?:wellfound\.com|angel\.co|linkedin\.com)/company/([^/?#]+)", u)
        if m:
            out.append({"name": m.group(1).replace("-", " ").title(), "website": ""})
        elif not any(b in u for b in JOB_BOARDS):
            out.append({"name": "", "website": u})
    return out


# ---- handlers -------------------------------------------------------------------------------

@owner_only
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    if _state["editing"]:  # the answer to "what should I change?"
        mid, _state["editing"] = _state["editing"], None
        await update.message.reply_text("✍️ Rewriting…")
        await engine.redraft(mid, text)
        m = db.q1("SELECT * FROM messages WHERE id=?", (mid,))
        msg = await say(draft_text(m), reply_markup=draft_buttons(mid))
        db.update("messages", mid, tg_message_id=msg.message_id)
        return
    urls = jobs.URL_RE.findall(text)
    items = items_from_urls(urls)
    rest = jobs.URL_RE.sub("", text).strip()
    if len(rest) > 2:
        out = await llm.complete_json(EXTRACT_PROMPT.format(text=rest[:3000]), purpose="prospect_extract", max_tokens=600)
        items += out.get("companies") or []
    if items:
        await update.message.reply_text(f"🔎 Looking up {len(items)} compan{'y' if len(items) == 1 else 'ies'}…")
        await add_companies(update, items)
    else:
        await update.message.reply_text("Send me company links, names or screenshots — or /help.")


@owner_only
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    f = await (msg.photo[-1] if msg.photo else msg.document).get_file()
    post = await jobs.read_image(bytes(await f.download_as_bytearray()))
    text = (post.get("text") or "") + f"\nCompany: {post.get('company') or ''}\nLinks: {' '.join(post.get('links') or [])}"
    out = await llm.complete_json(EXTRACT_PROMPT.format(text=text[:3000]), purpose="prospect_extract", max_tokens=600)
    items = out.get("companies") or []
    if items:
        await add_companies(update, items)
    else:
        await msg.reply_text("I couldn't find a company in that screenshot.")


@owner_only
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        pass
    _, action, arg = query.data.split("|")
    mid = int(arg)
    if action == "all":
        drafts = db.q("SELECT id FROM messages WHERE batch_date=? AND status='draft'", (engine.today(),))
        for d in drafts:
            await approve(d["id"])
        await query.edit_message_text(f"🚀 Sending {len(drafts)} emails, a few minutes apart. I'll report each one.")
    elif action == "skipday":
        db.run("UPDATE messages SET status='dropped' WHERE batch_date=? AND status='draft'", (engine.today(),))
        await query.edit_message_text("❌ Skipped today's batch. These people stay in the list for later.")
        db.run("""UPDATE contacts SET status='revealed' WHERE id IN (SELECT contact_id FROM messages
                  WHERE batch_date=? AND kind='initial' AND status='dropped')""", (engine.today(),))
    elif action == "send":
        await approve(mid)
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("✅ Queued.")
    elif action == "drop":
        m = db.q1("SELECT * FROM messages WHERE id=?", (mid,))
        db.update("messages", mid, status="dropped")
        if m and m["kind"] == "initial":
            db.update("contacts", m["contact_id"], status="skipped")
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("🗑 Dropped — I won't contact this person.")
    elif action == "edit":
        _state["editing"] = mid
        await query.message.reply_text("✏️ What should I change in this email?")


def _arg(ctx) -> str:
    return " ".join(ctx.args).strip()


@owner_only
async def cmd_help(update: Update, ctx) -> None:
    await update.message.reply_text(HELP.format(hour=config.SEND_HOUR, limit=config.DAILY_LIMIT))


@owner_only
async def cmd_run(update: Update, ctx) -> None:
    asyncio.create_task(daily_batch())


@owner_only
async def cmd_drafts(update: Update, ctx) -> None:
    ids = [r["id"] for r in db.q("SELECT id FROM messages WHERE batch_date=? AND status IN ('draft','approved')",
                                 (engine.today(),))]
    await show_batch(ids)


@owner_only
async def cmd_add(update: Update, ctx) -> None:
    arg = _arg(ctx)
    if not arg:
        await update.message.reply_text("Usage: /add Acme acme.com   or   /add https://acme.com")
        return
    parts = arg.split()
    site = next((p for p in parts if "." in p), "")
    name = " ".join(p for p in parts if p != site)
    await add_companies(update, [{"name": name, "website": site}])


@owner_only
async def cmd_stats(update: Update, ctx) -> None:
    await update.message.reply_text(sheet.stats_text())


@owner_only
async def cmd_export(update: Update, ctx) -> None:
    with open(sheet.export(), "rb") as f:
        await update.message.reply_document(f, caption="📊 Prospects: contacts, companies, emails, credits")


@owner_only
async def cmd_companies(update: Update, ctx) -> None:
    rows = db.q("SELECT name, domain, status, roles_json FROM companies ORDER BY id DESC LIMIT 25")
    await update.message.reply_text("\n".join(
        f"• {r['name']} ({r['domain'] or '?'}) — {r['status']}"
        + (f" · {len(json.loads(r['roles_json'] or '[]'))} open roles" if r["roles_json"] else "") for r in rows)
        or "No companies yet.", disable_web_page_preview=True)


@owner_only
async def cmd_stage(update: Update, ctx) -> None:
    arg = _arg(ctx)
    if arg not in ("1", "2", "3"):
        await update.message.reply_text(f"Now: stage {engine.stage()}. Use /stage 1 (1-50), 2 (51-200) or 3 (201-500).")
        return
    db.set_setting("stage", arg)
    await update.message.reply_text(f"📏 Company size set to stage {arg} ({', '.join(config.STAGES[int(arg)])} employees).")


@owner_only
async def cmd_pause(update: Update, ctx) -> None:
    db.set_setting("paused", "1")
    await update.message.reply_text("⏸ Daily batch paused. /resume to restart.")


@owner_only
async def cmd_resume(update: Update, ctx) -> None:
    db.set_setting("paused", "0")
    await update.message.reply_text(f"▶️ Daily batch on — next one at {config.SEND_HOUR}:00.")


@owner_only
async def cmd_check(update: Update, ctx) -> None:
    await update.message.reply_text("🔎 Checking your Apollo / Hunter access…")
    await update.message.reply_text(await access_report())


async def access_report() -> str:
    res = await apollo.check_access()
    lines = [f"• Apollo {k}: {v}" for k, v in res.items()]
    lines.append(f"• Apollo credits this month: {db.credits_this_month()} / {config.APOLLO_MONTHLY_CREDITS}")
    lines.append(f"• Hunter.io: {'key set' if config.HUNTER_API_KEY else 'no key (only website emails will be used)'}"
                 f" · searches {db.credits_this_month('hunter:search')}/{config.HUNTER_MONTHLY_SEARCHES}"
                 f" · verifications {db.credits_this_month('hunter:verify')}/{config.HUNTER_MONTHLY_VERIFICATIONS}")
    return "\n".join(lines)


async def post_init(app: Application) -> None:
    _state["bot"] = app.bot
    if not config.TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID not set")
        return
    loop = asyncio.get_running_loop()
    loop.create_task(sender())
    loop.create_task(scheduler())
    for r in db.q("SELECT id FROM messages WHERE status='approved'"):  # resume sending after a restart
        await send_queue.put(r["id"])
    await say("🎯 Prospector is online. /help")


def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    for name, fn in [("start", cmd_help), ("help", cmd_help), ("run", cmd_run), ("drafts", cmd_drafts),
                     ("add", cmd_add), ("stats", cmd_stats), ("export", cmd_export), ("companies", cmd_companies),
                     ("stage", cmd_stage), ("pause", cmd_pause), ("resume", cmd_resume), ("check", cmd_check)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^p\|"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
