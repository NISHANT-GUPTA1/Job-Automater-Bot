"""LinkedIn follow-up for companies you emailed: connect -> (accepted) short first message -> tell you on reply.

People come for free from what the email side already found: Hunter results (most have a LinkedIn URL) and the
founders listed on YC company pages. Nobody is searched for on LinkedIn.
"""
import html
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from jobapplier.jobs import UA
from jobapplier import emailedit
from jobapplier.llm import llm

from . import config, db, engine, linkedin

log = logging.getLogger(__name__)

NOTE_PROMPT = """Write a LinkedIn connection note from the candidate to {name} ({title}) at {company}.
They're hiring for: {role}. {emailed}
Max 180 characters. Warm, specific, human: mention the role and one real strength of the candidate.
No emojis, no hashtags, no "I hope you're well", no links. Return JSON: {{"note": str}}"""

DM_PROMPT = """{name} ({title}) at {company} just accepted the candidate's LinkedIn connection request.
Write the first message. They're hiring for: {role}. {emailed}
What the company does: {about}

Shape (same flow and warmth, your own words):
"Hi Sam, I'm <candidate>, pursuing <degree> at <college>. I went through Acme's page and what you're building with
<what Acme does>, and I'd really like to be part of it. I'm applying for the <role> role. <the highlight, told so it
matters to Acme>. Would love to discuss more about it."
1. Greet by first name; who the candidate is (name, degree, college) in one line.
2. One line showing they looked at the company: name what it actually builds (from "What the company does") and that
   this is why they want to join. If that's unknown, mention the company and the role only - never guess.
3. The role, then the ONE or TWO achievements from the resume closest to what this company builds or this role needs.
   Tell them so a founder gets it in a second and cares: the outcome and the scale in plain words, then why it
   matters for THEIR product specifically - name their domain (e.g. rebates, conferences, cargo ships) and what it
   would help with there. Each message must read as written only for {company}: no stock phrases such as "a product
   like yours" or "the kind of backbone". At most one tool name per achievement; no jargon (paginated, idempotent,
   hash-indexed, ingestion, throughput). Keep real numbers.
   Bad: "I scaled a paginated data-ingestion pipeline 46x in throughput and built FastAPI services feeding
   PostgreSQL pipelines." (jargon, says nothing about them)
4. {attach_rule}
5. Close: would love to discuss more / a quick chat.
55-90 words, warm and confident, correct grammar, no emojis, no links, no subject line. Never invent facts.
Return JSON: {{"message": str}}"""


def _today_count(field: str) -> int:
    return db.q1(f"SELECT COUNT(*) c FROM li_people WHERE {field} LIKE ?", (engine.today() + "%",))["c"]


def _week_invites() -> int:
    since = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    return db.q1("SELECT COUNT(*) c FROM li_people WHERE invited_at >= ?", (since,))["c"]


def _add(company_id: int, contact_id: int | None, name: str, title: str, url: str, source: str) -> bool:
    s = linkedin.slug(url)
    if not s or db.q1("SELECT 1 FROM li_people WHERE slug=?", (s,)):
        return False
    db.run("""INSERT INTO li_people(company_id, contact_id, name, title, slug, profile_url, source, status, created_at)
              VALUES(?,?,?,?,?,?,?,?,?)""",
           (company_id, contact_id, name, title, s, linkedin.profile_url(url), source, "queued", db.now()))
    return True


async def _yc_founders(company) -> list[dict]:
    """Founders (name, title, LinkedIn) from the company's YC page; cached."""
    roles = json.loads(company["roles_json"] or "[]")
    m = next((re.search(r"ycombinator\.com/companies/([^/]+)", r.get("url") or "") for r in roles
              if "ycombinator.com/companies/" in (r.get("url") or "")), None)
    if not m:
        return []
    key = f"yc_founders:{m.group(1)}"
    cached = db.get_setting(key)
    if cached:
        return json.loads(cached)
    founders = []
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as client:
            r = await client.get(f"https://www.ycombinator.com/companies/{m.group(1)}")
        page = re.search(r'data-page="([^"]+)"', r.text)
        data = json.loads(html.unescape(page.group(1)))["props"]["company"] if page else {}
        founders = [{"name": f.get("full_name"), "title": f.get("title") or "Founder", "linkedin": f.get("linkedin_url")}
                    for f in data.get("founders") or [] if f.get("linkedin_url")]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.info("YC founders for %s: %s", company["name"], e)
    db.set_setting(key, json.dumps(founders))
    return founders


async def collect() -> int:
    """Queue LinkedIn profiles of the people at companies you've emailed (who haven't replied by email)."""
    added = 0
    companies = db.q("""SELECT DISTINCT co.* FROM companies co JOIN contacts ct ON ct.company_id = co.id
                        WHERE ct.status IN ('sent', 'followed_up') AND co.status != 'replied'""")
    for co in companies:
        emailed = {r["email"].lower(): r for r in db.q(
            "SELECT * FROM contacts WHERE company_id=? AND status IN ('sent','followed_up')", (co["id"],))}
        for p in json.loads(db.get_setting(f"hunter:{co['domain']}") or "[]"):
            c = emailed.get((p.get("value") or "").lower())
            if c and p.get("linkedin"):
                added += _add(co["id"], c["id"], c["name"], c["title"] or p.get("position") or "", p["linkedin"], "hunter")
        for f in await _yc_founders(co):
            added += _add(co["id"], None, f["name"], f["title"], f["linkedin"], "yc")
    return added


def _context(p) -> dict:
    co = db.q1("SELECT * FROM companies WHERE id=?", (p["company_id"],))
    role = engine.target_role(co)[0] or "software / AI engineering roles"
    emailed = "The candidate also emailed them their resume recently." if p["contact_id"] else \
        "The candidate emailed their team recently."
    return {"name": p["name"], "title": p["title"] or "?", "company": co["name"], "role": role, "emailed": emailed,
            "about": (co["description"] or "unknown")[:400]}


def resume_for(p) -> str | None:
    """The resume already drafted for this company's email (this person's, else a colleague's) - no new cost."""
    row = None
    if p["contact_id"]:
        row = db.q1("""SELECT attachment FROM messages WHERE contact_id=? AND kind='initial' AND attachment IS NOT NULL
                       ORDER BY status='sent' DESC, id DESC LIMIT 1""", (p["contact_id"],))
    row = row or db.q1("""SELECT m.attachment FROM messages m JOIN contacts c ON c.id = m.contact_id
                          WHERE c.company_id=? AND m.kind='initial' AND m.attachment IS NOT NULL
                          ORDER BY m.status='sent' DESC, m.id DESC LIMIT 1""", (p["company_id"],))
    path = row["attachment"] if row else None
    return path if path and Path(path).exists() else None


async def draft_dm(person_id: int, instruction: str = "") -> str:
    p = db.q1("SELECT * FROM li_people WHERE id=?", (person_id,))
    attachment = p["attachment"] or resume_for(p)
    ctx = _context(p)
    if attachment and Path(attachment).name.endswith("_Resume.pdf"):  # older emails: give it the role name
        attachment = emailedit.rename_attachment(
            attachment, Path(engine.resume_file(ctx["role"])).name)
    ctx["attach_rule"] = (f"One short line saying their resume for the {ctx['role']} role is attached."
                          if attachment else "Don't mention a resume.")
    prompt = DM_PROMPT.format(**ctx)
    if instruction and p["message"]:
        prompt += f"\n\nPrevious draft:\n{p['message']}\nApply the candidate's change exactly: {instruction}"
    out = await llm.complete_json(prompt, purpose="linkedin_dm", max_tokens=400)
    msg = (out.get("message") or "").strip()
    db.update("li_people", person_id, message=msg, attachment=attachment)
    return msg


async def check_accepted(page, max_profile_checks: int = 10) -> int:
    """Mark accepted requests: in your recent connections = accepted; still in 'sent invitations' = waiting;
    anyone in neither list gets their profile opened to see (a few per round)."""
    connected = await linkedin.recent_connections(page)
    pending = await linkedin.pending_invites(page)
    accepted, unsure = 0, []
    for p in db.q("SELECT * FROM li_people WHERE status='invited' ORDER BY invited_at"):
        if p["slug"] in connected:
            db.update("li_people", p["id"], status="accepted", accepted_at=db.now())
            accepted += 1
        elif p["slug"] not in pending:
            unsure.append(p)
    for p in unsure[:max_profile_checks]:
        try:
            await linkedin.goto(page, p["profile_url"])
            state = await linkedin.profile_state(page)
        except linkedin.LinkedInStop:
            raise
        except Exception as e:  # one slow/odd profile shouldn't stop the round
            log.info("couldn't check %s: %s", p["profile_url"], e)
            continue
        if state == "connected":
            db.update("li_people", p["id"], status="accepted", accepted_at=db.now())
            accepted += 1
        elif state == "not_connected":  # withdrawn, declined or never went through: try again later
            db.update("li_people", p["id"], status="queued", invited_at=None)
    return accepted


def active_hours() -> bool:
    return config.LI_ACTIVE_HOURS[0] <= datetime.now().hour < config.LI_ACTIVE_HOURS[1]


async def cycle(notify) -> None:
    """One LinkedIn round: invites (within the daily/weekly caps), acceptances, approved messages, replies.
    notify(text, person_id=None) shows things in Telegram (person_id -> with Send/Edit/Drop buttons)."""
    if db.get_setting("li_paused") == "1" or not linkedin.has_session() or not active_hours():
        return
    await collect()
    async with linkedin.session() as page:
        if not await linkedin.logged_in(page):
            db.set_setting("li_session", "0")
            raise linkedin.LinkedInStop("LinkedIn logged you out. On the laptop run:  .venv\\Scripts\\python linkedin_login.py")

        # 1. invites
        room = min(config.LI_DAILY_INVITES - _today_count("invited_at"), config.LI_WEEKLY_INVITES - _week_invites())
        if db.get_setting("li_invites_paused_until", "") > db.now():
            room = 0
        sent = 0
        for p in db.q("SELECT * FROM li_people WHERE status='queued' ORDER BY contact_id IS NULL, id LIMIT ?",
                      (max(0, room),)):
            month = datetime.now().strftime("%Y-%m")
            allow_note = config.LI_SEND_NOTES and db.get_setting("li_notes_used_up") != month
            note = ""
            if allow_note:
                note = (await llm.complete_json(NOTE_PROMPT.format(**_context(p)), purpose="linkedin_note",
                                                max_tokens=200)).get("note", "")[:200]
            email = (db.q1("SELECT email FROM contacts WHERE id=?", (p["contact_id"],)) or {"email": ""})["email"] \
                if p["contact_id"] else ""
            try:
                result = await linkedin.invite(page, p["profile_url"], note, email or "", allow_note)
                if result == "notes_used_up":
                    db.set_setting("li_notes_used_up", month)
                    result = await linkedin.invite(page, p["profile_url"], "", email or "", False)
            except linkedin.LinkedInStop as e:
                if "weekly" in str(e):
                    db.set_setting("li_invites_paused_until",
                                   (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds"))
                raise
            except Exception as e:  # a page that didn't behave: skip this person, keep going
                log.warning("invite %s failed: %s", p["profile_url"], e)
                result = f"failed:{str(e)[:120]}"
            if result.startswith("invited") or result == "pending":
                db.update("li_people", p["id"], status="invited", note=note if result == "invited" else "",
                          invited_at=db.now())
                sent += result.startswith("invited")
            elif result == "connected":
                db.update("li_people", p["id"], status="accepted", accepted_at=db.now())
            else:
                db.update("li_people", p["id"], status="failed", error=result[7:])
            await linkedin.pause(*config.LI_GAP_SECONDS)
        if sent:
            await notify(f"🤝 LinkedIn: sent {sent} connection requests today "
                         f"({_week_invites()}/{config.LI_WEEKLY_INVITES} this week).")

        # 2. who accepted -> first message (for your approval unless LINKEDIN_AUTO_MESSAGE=true)
        if db.q1("SELECT 1 FROM li_people WHERE status='invited'"):
            await check_accepted(page)
        for p in db.q("SELECT * FROM li_people WHERE status='accepted'"):
            msg = await draft_dm(p["id"])
            if config.LI_AUTO_MESSAGE:
                db.update("li_people", p["id"], status="msg_approved")
            else:
                db.update("li_people", p["id"], status="msg_draft")
                await notify(dm_text(p["id"]), person_id=p["id"])

        # 3. send approved messages
        for p in db.q("SELECT * FROM li_people WHERE status='msg_approved' ORDER BY id"):
            try:
                result = await linkedin.send_message(page, p["profile_url"], p["message"], p["attachment"])
            except linkedin.LinkedInStop:
                raise
            except Exception as e:
                result = f"failed:{str(e)[:120]}"
            if result == "sent":
                db.update("li_people", p["id"], status="messaged", messaged_at=db.now())
                await notify(f"💬 LinkedIn message sent to {p['name']}.")
            else:
                db.update("li_people", p["id"], status="msg_failed", error=result[7:])
                await notify(f"⚠️ Couldn't message {p['name']} on LinkedIn: {result[7:]}\n{p['profile_url']}")
            await linkedin.pause(*config.LI_GAP_SECONDS)

        # 4. replies -> your turn
        waiting = db.q("SELECT * FROM li_people WHERE status='messaged'")
        if waiting:
            convs = await linkedin.conversations(page)
            for p in waiting:
                first = (p["name"] or "").split()[0].lower() if p["name"] else ""
                for c in convs:
                    if first and (p["name"] or "").lower() in c["text"].lower() and not c["from_me"]:
                        snippet = c["text"].split("\n")[-1][:300]
                        db.update("li_people", p["id"], status="replied", replied_at=db.now(), reply_snippet=snippet)
                        co = db.q1("SELECT name FROM companies WHERE id=?", (p["company_id"],))
                        await notify(f"🔔 {p['name']} ({p['title']}) @ {co['name']} replied on LinkedIn:\n“{snippet}”\n\n"
                                     f"Over to you: {p['profile_url']}")
                        break


def dm_text(person_id: int) -> str:
    p = db.q1("SELECT * FROM li_people WHERE id=?", (person_id,))
    co = db.q1("SELECT name FROM companies WHERE id=?", (p["company_id"],))
    return (f"✅ {p['name']} ({p['title'] or '?'}) @ {co['name']} accepted your LinkedIn request.\n"
            f"{p['profile_url']}\n" + (f"📎 {Path(p['attachment']).name}\n" if p["attachment"] else "")
            + f"\nFirst message:\n\n{p['message']}")


async def send_now(notify) -> None:
    """Send approved messages right away (used after you tap Send)."""
    if not linkedin.has_session():
        return
    async with linkedin.session() as page:
        for p in db.q("SELECT * FROM li_people WHERE status='msg_approved' ORDER BY id"):
            result = await linkedin.send_message(page, p["profile_url"], p["message"], p["attachment"])
            if result == "sent":
                db.update("li_people", p["id"], status="messaged", messaged_at=db.now())
                await notify(f"💬 LinkedIn message sent to {p['name']}.")
            else:
                db.update("li_people", p["id"], status="msg_failed", error=result[7:])
                await notify(f"⚠️ Couldn't message {p['name']} on LinkedIn: {result[7:]}\n{p['profile_url']}")
            await linkedin.pause(*config.LI_GAP_SECONDS)


def stats() -> str:
    rows = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM li_people GROUP BY status")}
    session = "signed in ✅" if linkedin.has_session() else "not signed in — run  .venv\\Scripts\\python linkedin_login.py"
    paused = " (paused — /linkedin resume)" if db.get_setting("li_paused") == "1" else ""
    return (f"🔗 LinkedIn{paused}: {session}\n"
            f"• waiting to connect: {rows.get('queued', 0)}\n"
            f"• requests sent, not accepted yet: {rows.get('invited', 0)}\n"
            f"• accepted, message waiting for you: {rows.get('msg_draft', 0)}\n"
            f"• messaged, waiting for a reply: {rows.get('messaged', 0)}\n"
            f"• replied: {rows.get('replied', 0)}\n"
            f"• couldn't connect/message: {rows.get('failed', 0) + rows.get('msg_failed', 0)}\n"
            f"Today {_today_count('invited_at')}/{config.LI_DAILY_INVITES} requests · "
            f"this week {_week_invites()}/{config.LI_WEEKLY_INVITES}")
