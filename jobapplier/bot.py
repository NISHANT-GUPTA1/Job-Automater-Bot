"""Telegram bot: send job links / job details, answer questions, check your tracker."""
import asyncio
import hashlib
import logging
import re

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import assistant, config, db, jobs, memory, profile, remote, tracker
from .asker import asker
from .browser import browser
from .pipeline import analyze_and_report, create_outreach_job, queue, worker

log = logging.getLogger(__name__)

HELP = """🤖 Job Applier — commands
Send a job link → I analyze it, apply on the site, and draft a cold email if there's a contact.
Paste job details (text + email) → I analyze and draft a cold email with your resume.
Send a screenshot of a job post → I read it: link → apply; email → cold email (caption = extra email).
Send just an email address → cold email for your latest job.
Mail rahul@acme.com (Rahul, Acme) for SDE Intern → cold outreach, no job post needed. Several people in one message is fine.
/mail <email> <company> <name> <role> — same, as a command
Add an email after the link to cold-mail someone too: <link> hr@company.com

/jobs [status] — recent jobs (e.g. /jobs applied)
/job <id> — full details + timeline
/stats — counts, today's progress, LLM spend
/export — Excel tracker (jobs, emails, accounts, JDs)
/mailed — sheet of everyone emailed: HR name, email, company, position, date
/email <id> [to] — draft a cold email for a job
/resume <id> — draft a tailored resume for a job
/retry <id> — run the application again
/screen — screenshot of the bot's browser right now
/remote — link to see & control the bot's browser from your phone (captchas, logins, stuck pages)
/open <url> — open a page in the bot's browser (e.g. log in to a site yourself from the phone)
/creds — saved site logins + the Site Accounts sheet
/status <id> <status> [note] — e.g. /status 4 interview  (interview/offer/rejected/applied/skipped)
/memory — answers I've learned · /teach question = answer · /forget <id>
/reload — reload profile.yaml & resume after you edit them"""

STATUSES = {"new", "analyzed", "applying", "applied", "emailed", "failed", "skipped", "saved",
            "interview", "offer", "rejected", "ghosted"}


def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if not config.TELEGRAM_CHAT_ID or str(chat.id) != config.TELEGRAM_CHAT_ID:
            if update.message:
                await update.message.reply_text(
                    f"Your chat id is {chat.id}. Put TELEGRAM_CHAT_ID={chat.id} in .env and restart the bot.")
            return
        return await func(update, ctx)
    return wrapper


async def reply(update: Update, text: str) -> None:
    for i in range(0, len(text), 4000):
        await update.effective_message.reply_text(text[i:i + 4000], disable_web_page_preview=True)


async def intake(job_id: int, url: str, text: str | None, email: str | None, next_kind: str = "process",
                 extra: dict | None = None) -> None:
    try:
        await analyze_and_report(job_id, url, text)
    except Exception as e:
        db.set_status(job_id, "failed", f"analyze: {e}")
        await asker.say(f"❌ Couldn't analyze job #{job_id}: {str(e)[:400]}")
        return
    if next_kind != "process":  # only a cover letter / resume: keep it out of auto-apply on restart
        db.set_status(job_id, "saved", next_kind)
    await queue.put((next_kind, job_id, {"email": email, "text": text, **(extra or {})}))
    if queue.qsize() > 1:
        await asker.say(f"⏳ Job #{job_id} queued ({queue.qsize()} waiting).")


@owner_only
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    text = msg.text or msg.caption or ""
    reply_to = msg.reply_to_message.message_id if msg.reply_to_message else None
    urls = [u.rstrip(").,>") for u in jobs.URL_RE.findall(text)]
    emails = jobs.extract_emails(text)
    email_arg = ", ".join(emails) or None
    pending = asker.pending_question()

    # 1) A reply to the bot's question, or an obvious answer (a code, tile numbers, an email it asked for)
    if asker.is_reply_to_pending(reply_to) or (pending and assistant.quick_answer(text, pending)):
        asker.answer_text(text, reply_to)
        await msg.reply_text("👍 Got it.")
        return

    # 2) Bare links / a bare email: no need to think about it
    rest = jobs.URL_RE.sub("", jobs.EMAIL_RE.sub("", text)).strip(" ,;.\n")
    if urls and len(rest) < 15:
        for url in urls:
            await start_job(update, url, None, email_arg)
        return
    if emails and not urls and not rest:
        job = db.q1("SELECT * FROM jobs ORDER BY id DESC LIMIT 1")
        if job:
            await queue.put(("email", job["id"], {"to": email_arg}))
            await reply(update, f"✉️ Drafting a cold email to {email_arg} for job #{job['id']} "
                                f"({job['title'] or '?'} @ {job['company'] or '?'}). You'll see it before it's sent.\n"
                                f"(Different job? Use /email <id> {emails[0]})")
            return

    # 3) Everything else: understand what you want
    await ctx.bot.send_chat_action(msg.chat_id, "typing")
    try:
        r = await assistant.route(text, pending)
    except Exception as e:
        await reply(update, f"❌ Sorry, I couldn't process that: {str(e)[:300]}")
        return
    intent, job_id = r["intent"], r.get("job_id")
    if job_id and not db.get_job(job_id):
        job_id = None
    url = (r.get("url") or "").strip() or (urls[0] if urls else "")
    job_text = (r.get("job_text") or "").strip()

    if intent == "answer" and pending:
        asker.answer_text(text, None)
        await msg.reply_text("👍 Got it.")
    elif intent == "job_post":
        if urls:
            for u in urls:
                await start_job(update, u, None, email_arg)
        else:
            await start_job(update, None, job_text or text, email_arg)
    elif intent == "outreach":
        if not await start_outreach(update, text):
            await start_job(update, url or None, None if url else (job_text or text), email_arg)
    elif intent in ("cover_letter", "tailor_resume"):
        kind = "cover" if intent == "cover_letter" else "tailor"
        what = "cover letter" if kind == "cover" else "tailored resume"
        notes = (r.get("notes") or "").strip()
        if job_id:
            await queue.put((kind, job_id, {"notes": notes}))
            await reply(update, f"✍️ Making the {what} for job #{job_id}…")
        elif url or len(job_text) > 80:
            await start_job(update, url or None, None if url else job_text, email_arg, next_kind=kind,
                            extra={"notes": notes})
        else:
            await reply(update, f"Sure — send me the job link, a screenshot, or paste the job requirements for the {what}.")
    elif intent == "action" and job_id:
        await do_action(update, r.get("action"), job_id, r.get("status"))
    elif intent == "remember":
        memory.save(f"Standing instruction: {text[:80]}", text)
        await reply(update, f"🧠 {r.get('reply') or 'Noted - I will follow this from now on.'}")
    else:
        await reply(update, r.get("reply") or "Send me a job link, a screenshot, or tell me what you need. /help")


async def do_action(update: Update, action: str | None, job_id: int, status: str | None) -> None:
    job = db.get_job(job_id)
    if action == "retry" and not job["url"].startswith("text:"):
        await queue.put(("apply", job_id, {}))
        await reply(update, f"🔁 Job #{job_id} queued for applying.")
    elif action == "skip":
        db.set_status(job_id, "skipped", "by you")
        await reply(update, f"⏭ Job #{job_id} skipped.")
    elif action == "status" and status in STATUSES:
        db.set_status(job_id, status, "by you")
        await reply(update, f"Job #{job_id} → {status}")
    elif action == "email":
        await queue.put(("email", job_id, {}))
        await reply(update, f"✉️ Drafting a cold email for job #{job_id}…")
    elif action == "resume":
        await queue.put(("tailor", job_id, {}))
        await reply(update, f"✍️ Making a tailored resume for job #{job_id}…")
    else:
        await reply(update, f"I couldn't tell what to do with job #{job_id}. Try: retry / skip / mark as interview / email.")


async def start_outreach(update: Update, text: str) -> bool:
    """'Mail rahul@acme.com at Acme for SDE intern' -> one tracked outreach + cold-email draft per recipient.
    Returns False if the message turned out to be a pasted job post instead."""
    try:
        parsed = await jobs.parse_outreach(text)
    except Exception as e:
        await reply(update, f"❌ Couldn't understand that: {str(e)[:300]}")
        return True
    if parsed.get("intent") != "outreach" or not parsed["recipients"]:
        return False
    lines = []
    for r in parsed["recipients"]:
        job_id = create_outreach_job(r, parsed.get("note") or "")
        await queue.put(("email", job_id, {"to": r["email"]}))
        lines.append(f"• #{job_id} {r.get('person_name') or r['email']} @ {r.get('company') or '?'} — "
                     f"{r.get('role') or 'any suitable role'}")
    await reply(update, "✉️ Cold outreach queued — you'll see each draft before it's sent:\n" + "\n".join(lines))
    return True


async def start_job(update: Update, url: str | None, text: str | None, email: str | None,
                    usage_tags: list[str] | None = None, next_kind: str = "process",
                    extra: dict | None = None) -> int | None:
    """Track a job from a link, or from text (pasted or read from a screenshot), and start processing."""
    if not url:
        url = "text:" + hashlib.sha1(text.encode()).hexdigest()[:12]
    job_id, created = db.add_job(url, text or "")
    for tag in usage_tags or []:  # charge the screenshot-reading calls to this job
        db.run("UPDATE llm_usage SET job_id=? WHERE job_id IS NULL AND purpose LIKE ?", (job_id, f"read_image:{tag}%"))
    if not created and next_kind != "process":  # cover letter / resume for a job we already have
        await queue.put((next_kind, job_id, extra or {}))
        await reply(update, f"✍️ Job #{job_id} is already tracked — making the "
                            f"{'cover letter' if next_kind == 'cover' else 'tailored resume'} for it…")
        return job_id
    if not created:
        await reply(update, f"Already tracked:\n{jobs.job_card(job_id)}\n\n"
                            f"Use /retry {job_id} to apply again or /email {job_id} to cold-email.")
        return None
    source = "link" if not url.startswith("text:") else "details"
    await update.effective_message.reply_text(f"📥 Job #{job_id} received ({source}) — analyzing…")
    asyncio.create_task(intake(job_id, url, None if source == "link" else text, email, next_kind, extra))
    return job_id


_albums: dict[str, list] = {}  # media_group_id -> messages of a multi-photo send


@owner_only
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Screenshots of job posts (one or many at once): read each, then apply via link / cold-email via address."""
    msg = update.message
    gid = msg.media_group_id
    if not gid:
        await process_screenshots(update, [msg])
        return
    batch = _albums.setdefault(gid, [])
    batch.append(msg)
    if len(batch) == 1:  # first photo of the album waits for the rest, then handles them all together
        await asyncio.sleep(3)
        await process_screenshots(update, _albums.pop(gid))


async def _read_one(msg) -> dict:
    if msg.photo:
        tg_file, media_type = await msg.photo[-1].get_file(), "image/jpeg"
    else:
        tg_file, media_type = await msg.document.get_file(), msg.document.mime_type or "image/jpeg"
    data = bytes(await tg_file.download_as_bytearray())
    posts_dir = config.DATA_DIR / "posts"
    posts_dir.mkdir(exist_ok=True)
    (posts_dir / f"{tg_file.file_unique_id}.jpg").write_bytes(data)
    try:
        post = await jobs.read_image(data, media_type, tag=tg_file.file_unique_id)
    except Exception as e:
        return {"error": str(e)[:200], "tag": tg_file.file_unique_id}
    post["tag"] = tg_file.file_unique_id
    return post


async def process_screenshots(update: Update, msgs: list) -> None:
    n = len(msgs)
    await update.effective_message.reply_text(f"🔎 Reading {n} screenshot{'s' if n > 1 else ''}…")
    captions = " ".join(m.caption or "" for m in msgs)
    posts = await asyncio.gather(*(_read_one(m) for m in msgs))

    # Screenshots of the same post (same company + title) become one job
    groups: dict[tuple, dict] = {}
    skipped = []
    for i, p in enumerate(posts, 1):
        text = (p.get("text") or "").strip()
        if p.get("error") or not text:
            skipped.append(f"screenshot {i}: {p.get('error') or 'no text found'}")
            continue
        key = ((p.get("company") or "").lower().strip(), (p.get("title") or "").lower().strip())
        if not all(key):
            key = (f"#{i}", "")
        g = groups.setdefault(key, {"title": p.get("title"), "company": p.get("company"), "texts": [],
                                    "emails": [], "links": [], "tags": []})
        g["texts"].append(text)
        g["emails"] += p["emails"]
        g["links"] += p["links"]
        g["tags"].append(p["tag"])

    caption_emails = jobs.extract_emails(captions)
    caption_links = jobs.URL_RE.findall(captions) if n == 1 else []  # an album caption can't belong to one post
    lines = []
    for g in groups.values():
        emails = list(dict.fromkeys(caption_emails + g["emails"]))
        links = list(dict.fromkeys(caption_links + g["links"]))
        email_arg = ", ".join(emails) or None
        lines.append(f"• {g['title'] or '?'} @ {g['company'] or '?'} — "
                     f"{'link' if links else 'no link'}, {email_arg or 'no email'}"
                     + (f" ({len(g['texts'])} screenshots)" if len(g["texts"]) > 1 else ""))
        text = "\n\n".join(g["texts"])
        if links:
            await start_job(update, links[0].rstrip(").,>"), None, email_arg, g["tags"])
        else:
            await start_job(update, None, text, email_arg, g["tags"])
    summary = f"🖼 Found {len(groups)} job post{'s' if len(groups) != 1 else ''}:\n" + "\n".join(lines)
    if skipped:
        summary += "\n⚠️ Couldn't read " + "; ".join(skipped)
    await reply(update, summary)


@owner_only
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        pass  # button tapped while the bot was offline: too old to acknowledge, still handle it
    try:
        _, key, idx = query.data.split("|")
        choice = asker.answer_button(int(key), int(idx))
    except ValueError:
        choice = None
    original = query.message.text or ""
    if choice:
        await query.edit_message_text(f"{original}\n\n➡️ {choice}")
    else:
        await query.edit_message_text(f"{original}\n\n(expired)")


def _arg_id(ctx) -> int | None:
    try:
        return int(ctx.args[0])
    except (IndexError, ValueError):
        return None


@owner_only
async def cmd_help(update: Update, ctx) -> None:
    await reply(update, HELP)


@owner_only
async def cmd_jobs(update: Update, ctx) -> None:
    if ctx.args:
        rows = db.q("SELECT * FROM jobs WHERE status=? ORDER BY id DESC LIMIT 30", (ctx.args[0],))
    else:
        rows = db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 30")
    if not rows:
        await reply(update, "No jobs yet.")
        return
    icon = {"applied": "✅", "emailed": "📨", "failed": "❌", "skipped": "⏭", "interview": "🗓",
            "offer": "🎉", "rejected": "🚫", "applying": "🤖"}
    lines = [f"{icon.get(r['status'], '•')} #{r['id']} {r['title'] or '?'} — {r['company'] or '?'} "
             f"[{r['status']}{' +✉️' if r['emailed_at'] and r['status'] != 'emailed' else ''}]" for r in rows]
    await reply(update, "\n".join(lines))


@owner_only
async def cmd_job(update: Update, ctx) -> None:
    job_id = _arg_id(ctx)
    if not job_id or not db.get_job(job_id):
        await reply(update, "Usage: /job <id>")
        return
    events = db.q("SELECT kind, detail, created_at FROM events WHERE job_id=? ORDER BY id", (job_id,))
    mails = db.q("SELECT to_addr, subject, status, sent_at FROM emails WHERE job_id=? AND status='sent'", (job_id,))
    text = jobs.job_card(job_id) + "\n" + tracker.job_cost_text(job_id) + "\n\n🕒 Timeline:\n" + "\n".join(
        f"  {e['created_at'][5:16]} {e['kind']} {e['detail'][:80]}" for e in events)
    if mails:
        text += "\n\n📨 Emails:\n" + "\n".join(f"  {m['sent_at'][:16]} → {m['to_addr']}: {m['subject']}" for m in mails)
    await reply(update, text)


@owner_only
async def cmd_stats(update: Update, ctx) -> None:
    await reply(update, tracker.stats_text())


@owner_only
async def cmd_export(update: Update, ctx) -> None:
    path = tracker.export_excel()
    with open(path, "rb") as f:
        await update.message.reply_document(f, caption="📊 Your job tracker")


@owner_only
async def cmd_creds(update: Update, ctx) -> None:
    accounts = db.list_accounts()
    if ctx.args:
        accounts = [a for a in accounts if ctx.args[0].lower() in a["domain"]]
    if not accounts:
        await reply(update, "No saved accounts.")
        return
    await reply(update, "🔐 Saved logins:\n" + "\n".join(
        f"• {a['domain']}\n   {a['email']} / {a['password']}" for a in accounts))
    if not ctx.args:  # the full list as a sheet: company, website, login URL, email, password
        with open(tracker.update_accounts_sheet(), "rb") as f:
            await update.message.reply_document(f, caption="🔐 Site accounts sheet")


@owner_only
async def cmd_email(update: Update, ctx) -> None:
    job_id = _arg_id(ctx)
    if not job_id or not db.get_job(job_id):
        await reply(update, "Usage: /email <job id> [to@email]")
        return
    to = ctx.args[1] if len(ctx.args) > 1 else None
    await queue.put(("email", job_id, {"to": to}))
    await reply(update, f"✉️ Drafting cold email for job #{job_id}…")


@owner_only
async def cmd_mailed(update: Update, ctx) -> None:
    """The Mailed Contacts sheet: everyone the bot has emailed, with name, company, position, date."""
    path = tracker.update_mailed_sheet()
    rows = tracker.mailed_rows()
    recent = "\n".join(f"• {r['Date sent'][:10]}  {r['HR / Contact name'] or r['Email']} — {r['Company'] or '?'}"
                       f" ({r['Position'] or '?'})" for r in rows[:10])
    with open(path, "rb") as f:
        await update.message.reply_document(
            f, caption=f"📒 Mailed contacts: {len(rows)} sent" + (f"\nLatest:\n{recent}" if recent else "")[:1000])


@owner_only
async def cmd_remote(update: Update, ctx) -> None:
    """Link to see and control the bot's browser from this phone."""
    for _ in range(10):  # the tunnel needs a few seconds after startup
        if config.BROWSER_VIEW_URL:
            break
        await asyncio.sleep(2)
    if not config.BROWSER_VIEW_URL:
        await reply(update, "Phone control isn't available right now (tunnel not running). Try again in a minute.")
        return
    await reply(update, f"🖥 Control the bot's browser from your phone:\n{config.BROWSER_VIEW_URL}\n\n"
                        "Tap the picture to click · type in the box · Enter/Tab/⌫ · ▲▼ scroll · Back · Next tab.\n"
                        "Keep this link private - it controls the browser. Use /open <url> to open any page.")


@owner_only
async def cmd_open(update: Update, ctx) -> None:
    """/open <url>: open a page in the bot's browser (e.g. to log in to a site yourself from the phone)."""
    url = ctx.args[0] if ctx.args else ""
    if not url.startswith("http"):
        await reply(update, "Usage: /open https://site.com/login")
        return
    await browser.ensure()
    page = await browser.ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await cmd_remote(update, ctx)


@owner_only
async def cmd_screen(update: Update, ctx) -> None:
    """Screenshot of what the bot's browser shows right now (+ link to control it on a server)."""
    if not browser.ctx or not browser.ctx.pages:
        await reply(update, "The browser isn't open right now." + config.browser_hint())
        return
    page = browser.ctx.pages[-1]
    path = config.SCREENSHOT_DIR / "screen_now.png"
    await page.screenshot(path=str(path))
    with open(path, "rb") as f:
        await update.message.reply_photo(f, caption=f"{page.url[:200]}{config.browser_hint()}")


@owner_only
async def cmd_mail(update: Update, ctx) -> None:
    text = " ".join(ctx.args)
    if not jobs.extract_emails(text) or not await start_outreach(update, "mail " + text):
        await reply(update, "Usage: /mail hr@acme.com Acme Rahul Sharma SDE Intern\n"
                            "(or just write: Mail hr@acme.com at Acme for any suitable role)")


@owner_only
async def cmd_resume(update: Update, ctx) -> None:
    job_id = _arg_id(ctx)
    if not job_id or not db.get_job(job_id):
        await reply(update, "Usage: /resume <job id>")
        return
    await queue.put(("tailor", job_id, {}))
    await reply(update, "✍️ Queued resume drafting.")


@owner_only
async def cmd_retry(update: Update, ctx) -> None:
    job_id = _arg_id(ctx)
    job = db.get_job(job_id) if job_id else None
    if not job or job["url"].startswith("text:"):
        await reply(update, "Usage: /retry <job id> (only for jobs with a link)")
        return
    await queue.put(("apply", job_id, {}))
    await reply(update, f"🔁 Job #{job_id} queued for applying.")


@owner_only
async def cmd_status(update: Update, ctx) -> None:
    job_id = _arg_id(ctx)
    if not job_id or len(ctx.args) < 2 or ctx.args[1] not in STATUSES or not db.get_job(job_id):
        await reply(update, f"Usage: /status <id> <{'|'.join(sorted(STATUSES))}> [note]")
        return
    note = " ".join(ctx.args[2:])
    db.set_status(job_id, ctx.args[1], note)
    if note:
        db.update_job(job_id, notes=note)
    await reply(update, f"Job #{job_id} → {ctx.args[1]}")


@owner_only
async def cmd_memory(update: Update, ctx) -> None:
    rows = db.q("SELECT id, question, answer FROM qa_memory ORDER BY id")
    await reply(update, "🧠 Learned answers:\n" + "\n".join(
        f"#{r['id']} {r['question'][:80]} → {r['answer'][:80]}" for r in rows) if rows else "Nothing learned yet.")


@owner_only
async def cmd_teach(update: Update, ctx) -> None:
    text = " ".join(ctx.args)
    if "=" not in text:
        await reply(update, "Usage: /teach Expected CTC = 8 LPA")
        return
    question, answer = (s.strip() for s in text.split("=", 1))
    memory.save(question, answer)
    await reply(update, f"🧠 Saved: {question} → {answer}")


@owner_only
async def cmd_forget(update: Update, ctx) -> None:
    mem_id = _arg_id(ctx)
    db.run("DELETE FROM qa_memory WHERE id=?", (mem_id,))
    await reply(update, f"Forgot #{mem_id}.")


@owner_only
async def cmd_reload(update: Update, ctx) -> None:
    profile.reload()
    has_resume = bool(profile.base_resume_text())
    await reply(update, f"🔄 Reloaded profile. Base resume: {'found' if has_resume else 'MISSING (resumes/base_resume.pdf)'}")


async def post_init(app: Application) -> None:
    if not config.TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID not set (or invalid). Send /start to @%s in Telegram to get your chat id.",
                    app.bot.username)
        return
    try:
        await app.bot.send_chat_action(int(config.TELEGRAM_CHAT_ID), "typing")
    except TelegramError as e:
        log.warning("Can't message chat %s (%s). Send /start to @%s to get your real chat id.",
                    config.TELEGRAM_CHAT_ID, e, app.bot.username)
        config.TELEGRAM_CHAT_ID = ""
        return
    if config.TELEGRAM_CHAT_ID:
        asker.bind(app.bot, int(config.TELEGRAM_CHAT_ID))
        asyncio.get_running_loop().create_task(worker())
        # resume jobs that were interrupted by a restart
        for r in db.q("SELECT id FROM jobs WHERE status IN ('applying','analyzed') ORDER BY id"):
            await queue.put(("process", r["id"], {}))
        await asker.say("🤖 Job Applier is online. Send me a job link. /help")
        try:
            await remote.start()  # phone control of the bot's browser (/remote)
        except Exception as e:
            log.warning("Phone browser control not started: %s", e)


async def post_shutdown(app: Application) -> None:
    await remote.stop()
    await browser.close()


def build_app() -> Application:
    app = (Application.builder().token(config.TELEGRAM_BOT_TOKEN).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    for name, fn in [("start", cmd_help), ("help", cmd_help), ("jobs", cmd_jobs), ("job", cmd_job),
                     ("stats", cmd_stats), ("export", cmd_export), ("creds", cmd_creds), ("email", cmd_email),
                     ("resume", cmd_resume), ("retry", cmd_retry), ("status", cmd_status), ("mail", cmd_mail), ("screen", cmd_screen), ("mailed", cmd_mailed),
                     ("remote", cmd_remote), ("open", cmd_open),
                     ("memory", cmd_memory), ("teach", cmd_teach), ("forget", cmd_forget), ("reload", cmd_reload)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^ans\|"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
