"""Daily cycle: companies -> people -> emails -> drafts for your review -> send -> follow-ups -> replies."""
import json
import logging
import re
import shutil
from datetime import datetime, timedelta

from pathlib import Path

from jobapplier import config as base_config, db as jobdb, jobs, mailer, profile
from jobapplier.llm import llm
from jobapplier.resume import _jaccard, _slug, _words, ensure_base_pdf, generate_tailored

import httpx

from jobapplier.jobs import UA

from . import apollo, careers, config, db, hunter, jobsources, website

log = logging.getLogger(__name__)
ALLOWED_EMAIL_STATUS = {"verified", "likely to engage", "likely_to_engage"}  # protects your Gmail from bounces


def stage() -> int:
    return int(db.get_setting("stage", "1"))


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def domain_of(value: str) -> str:
    host = re.sub(r"^https?://", "", (value or "").strip().lower()).split("/")[0]
    return host[4:] if host.startswith("www.") else host


def already_emailed(email: str) -> bool:
    """True if this address got an email from either bot before - nobody is contacted twice."""
    e = (email or "").lower()
    if db.q1("SELECT 1 FROM contacts WHERE lower(email)=? AND status IN ('sent','followed_up','replied','bounced')",
             (e,)):
        return True
    return bool(jobdb.q1("SELECT 1 FROM emails WHERE status='sent' AND lower(to_addr) LIKE ?", (f"%{e}%",)))


def add_company(name: str, domain: str, apollo_id: str | None = None, website: str = "", linkedin: str = "",
                source: str = "you") -> tuple[int, bool]:
    domain = domain_of(domain)
    row = (db.q1("SELECT id FROM companies WHERE apollo_id=?", (apollo_id,)) if apollo_id else None) or \
          (db.q1("SELECT id FROM companies WHERE domain=?", (domain,)) if domain else None)
    if row:
        return row["id"], False
    cid = db.run("""INSERT INTO companies(apollo_id, name, domain, website, linkedin, size_stage, source, status,
                    created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 (apollo_id, name, domain or None, website or (f"https://{domain}" if domain else ""), linkedin,
                  stage(), source, "new", db.now(), db.now()))
    return cid, True


# ---- finding companies and people -----------------------------------------------

async def resolve_domain(name: str) -> str | None:
    """A company's website from its name: try the usual domains and check the page mentions the name."""
    base = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not base:
        return None
    word = re.sub(r"[^a-z0-9 ]", "", name.lower()).split()[0]
    async with httpx.AsyncClient(timeout=8, follow_redirects=True, headers={"User-Agent": UA}) as client:
        for tld in (".com", ".ai", ".io", ".in", ".co", ".tech", ".app", ".dev"):
            try:
                r = await client.get(f"https://{base}{tld}")
            except httpx.HTTPError:
                continue
            if r.status_code < 400 and word in r.text[:20000].lower():
                return domain_of(str(r.url))
    return None


async def discover() -> int:
    """New companies for the current size stage: Apollo search if your plan has it, else free job APIs."""
    new = 0
    if db.get_setting("apollo_search_blocked") != "1":
        key = f"apollo_page_stage{stage()}"
        page = int(db.get_setting(key, "1"))
        try:
            orgs = await apollo.search_companies(stage(), page)
            db.set_setting(key, str(page + 1))
            for o in orgs:
                domain = o.get("primary_domain") or domain_of(o.get("website_url") or "")
                if domain:
                    _, created = add_company(o.get("name") or domain, domain, o.get("id"), o.get("website_url") or "",
                                             o.get("linkedin_url") or "", "apollo")
                    new += created
            return new
        except apollo.ApolloBlocked:
            db.set_setting("apollo_search_blocked", "1")  # free plan: don't try again
    seen = {r["name"].lower() for r in db.q("SELECT name FROM companies")}
    sources = []
    if db.get_setting("hn_thread_done") != datetime.now().strftime("%Y-%m"):  # the HN thread is monthly
        sources += await jobsources.hn_who_is_hiring()
        db.set_setting("hn_thread_done", datetime.now().strftime("%Y-%m"))
    sources += await jobsources.hiring_companies()
    for job in sources:
        if job["company"].lower() in seen or new >= 25:
            continue
        seen.add(job["company"].lower())
        domain = domain_of(job.get("website") or "") or (job["email"].split("@")[1] if job.get("email") and
                 not re.search(r"@(gmail|yahoo|outlook|hotmail|proton)", job["email"]) else "")             or await resolve_domain(job["company"])
        if not domain:
            continue
        cid, created = add_company(job["company"], domain, source=job["source"])
        if not created:
            continue
        db.update("companies", cid, roles_json=json.dumps([{"title": job["title"], "url": job["url"],
                                                            "source": job["source"]}]))
        if job.get("email"):  # written in the post itself: usually the founder / hiring manager
            _add_contact(cid, f"post:{job['email']}", f"{job['company']} Hiring Contact",
                         "Hiring contact (from their job post)", 1, job["email"], "published")
            db.update("companies", cid, status="people_found")
        new += 1
    return new


async def check_size(company_id: int) -> bool:
    """True if the company fits the current size stage. Unknown size is looked up once (1 Apollo credit)."""
    c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    if c["employees"] is None and c["source"] != "apollo":
        try:
            org = await apollo.enrich_company(c["domain"])
            db.update("companies", company_id, employees=org.get("estimated_num_employees"),
                      description=(org.get("short_description") or "")[:500], linkedin=org.get("linkedin_url") or "")
            c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
        except (apollo.ApolloBlocked, apollo.ApolloOutOfCredits, httpx.HTTPError) as e:
            log.info("size check skipped for %s: %s", c["name"], e)
    if c["employees"] and c["employees"] > config.STAGE_MAX_EMPLOYEES[stage()]:
        db.update("companies", company_id, status="too_big", updated_at=db.now())
        return False
    return True


def _tier_for(position: str, department: str) -> int | None:
    p = f"{position or ''} {department or ''}".lower()
    if re.search(r"recruit|talent|\bhr\b|human resources|people (ops|operations|partner)|hiring", p):
        return 1
    if re.search(r"engineering manager|head of (engineering|technology|product)|\bcto\b|chief technology|"
                 r"vp,? engineering|director of engineering|tech(nical)? lead", p):
        return 2
    if re.search(r"founder|\bceo\b|chief executive|owner|managing director", p):
        return 3
    return None


def _add_contact(company_id: int, key: str, name: str, title: str, tier: int, email: str, status: str) -> bool:
    if db.q1("SELECT 1 FROM contacts WHERE apollo_person_id=? OR lower(email)=?", (key, email.lower())):
        return False
    if already_emailed(email):
        return False
    db.run("""INSERT INTO contacts(company_id, apollo_person_id, name, title, tier, email, email_status, status,
              created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
           (company_id, key, name, title, tier, email, status, "revealed", db.now()))
    return True


async def research(company_id: int) -> int:
    """Open roles + the best person to email, cheapest source first:
    the company's own website (free) -> Hunter.io (50 free/month) -> Apollo people search (paid plans)."""
    c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    if not await check_size(company_id):
        return 0
    roles = await careers.open_roles(c["name"], c["domain"] or "")
    if roles or not c["roles_json"]:
        db.update("companies", company_id, roles_json=json.dumps(roles, ensure_ascii=False), updated_at=db.now())
    has_roles = bool(roles or json.loads(c["roles_json"] or "[]"))

    candidates: list[tuple[int, int, str, str, str, str]] = []  # (tier, -confidence, email, name, title, status)
    for e in await website.published_emails(c["domain"]):
        tier = {"hiring": 1, "founder": 3, "general": 4}.get(e["kind"])
        if tier:
            label = {1: f"{c['name']} Hiring Team", 3: f"{c['name']} Founders", 4: f"{c['name']} Team"}[tier]
            candidates.append((tier, -100, e["email"], label, "Team inbox (published on their website)", "published"))

    if not any(t == 1 for t, *_ in candidates) and has_roles:  # worth a Hunter search
        cache_key = f"hunter:{c['domain']}"
        try:
            people = json.loads(db.get_setting(cache_key) or "null")
            if people is None:  # one search per domain, ever
                people = await hunter.domain_search(c["domain"])
                db.set_setting(cache_key, json.dumps(people))
            usable = []
            for p in people:
                status = ((p.get("verification") or {}).get("status") or "unknown").lower()
                conf = int(p.get("confidence") or 0)
                on_own_site = any(s.get("still_on_page") and s.get("domain") == c["domain"]
                                  for s in p.get("sources") or [])
                if p.get("value") and (status == "valid" or conf >= 90 or (on_own_site and status != "invalid")):
                    name = (" ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
                            or p["value"].split("@")[0].replace(".", " ").title())
                    usable.append((_tier_for(p.get("position"), p.get("department")), -conf, p["value"], name,
                                   p.get("position") or "", status))
            candidates += [u for u in usable if u[0]]
            # tiny team where nobody has a title: the few people Hunter knows are almost always founders/early staff
            if not any(u[0] for u in usable) and 0 < len(people) <= 3 and (c["employees"] or 0) <= 15:
                candidates += [(3, conf, email, name, "Early team member (title not listed)", status)
                               for _, conf, email, name, _, status in usable]
        except hunter.HunterUnavailable as e:
            log.info("Hunter skipped for %s: %s", c["name"], e)

    picked = 0
    for tier, _, email, name, title, status in sorted(candidates):
        if _add_contact(company_id, f"{'web' if status == 'published' else 'hunter'}:{email}", name, title, tier,
                        email, status):
            picked += 1
            if picked >= config.CONTACTS_PER_COMPANY:
                break
    db.update("companies", company_id, status="people_found" if picked else "no_people", updated_at=db.now())
    return picked


async def reveal(contact_id: int) -> bool:
    """Get the contact's work email from Apollo (1 credit when found). True if usable."""
    c = db.q1("SELECT * FROM contacts WHERE id=?", (contact_id,))
    person = await apollo.reveal_email(c["apollo_person_id"])
    email = (person.get("email") or "").strip()
    status = (person.get("email_status") or "").lower()
    fields = {"email": email or None, "email_status": status, "name": person.get("name") or c["name"],
              "title": person.get("title") or c["title"]}
    if not email or (status and status not in ALLOWED_EMAIL_STATUS):
        db.update("contacts", contact_id, status="no_email", **fields)
        return False
    if already_emailed(email):
        db.update("contacts", contact_id, status="skipped", **fields)
        return False
    db.update("contacts", contact_id, status="revealed", **fields)
    return True


# ---- drafting ----------------------------------------------------------------------

INITIAL_PROMPT = """Write a cold email from the candidate to {name} ({title}) at {company} ({website}).
Recipient type: {tier_label}.
Open roles at this company that fit the candidate: {roles}

Write it for this person:
- HR / recruiter: ask to be considered for the most fitting role (or software/AI engineering roles / internships
  if none are listed); show fit with 2 real, relevant highlights.
- Engineering manager / CTO: lead with the most relevant technical work; ask to be considered or for a short chat.
- Founder: show genuine interest in what the company builds and the impact the candidate can have; ask for a short
  chat or to be pointed to the right person.
If the recipient is a team inbox (e.g. "Acme Hiring Team"), greet the team ("Hi Acme team,").
Rules: 90-140 words; greet by first name; one line on who the candidate is; 2 concrete, real highlights from the
resume; say the resume is attached; end with the candidate's name, phone and LinkedIn. Never invent facts, no
placeholders, no cliches ("I hope this email finds you well", "I am writing to express"). Subject: short and specific
(the role and the candidate's name).
Return JSON: {{"subject": str, "body": str}}"""

FOLLOWUP_PROMPT = """Write a short, polite follow-up to this cold email that got no reply.
Recipient: {name} ({title}) at {company}.
<previous_email>
{previous}
</previous_email>
Rules: 40-70 words, friendly, no guilt-tripping, add one new short reason to talk (a different real highlight),
ask one easy question. Same sign-off. Return JSON: {{"body": str}}"""


def _roles_text(company) -> str:
    roles = json.loads(company["roles_json"] or "[]")
    return ", ".join(r["title"] for r in roles[:5]) or "none listed"


async def draft_initial(contact_id: int, instruction: str = "", previous: dict | None = None) -> dict:
    c = db.q1("SELECT * FROM contacts WHERE id=?", (contact_id,))
    co = db.q1("SELECT * FROM companies WHERE id=?", (c["company_id"],))
    prompt = INITIAL_PROMPT.format(name=c["name"], title=c["title"] or "?", company=co["name"],
                                   website=co["website"] or co["domain"] or "", tier_label=config.TIERS[c["tier"]][0],
                                   roles=_roles_text(co))
    if previous and instruction:
        prompt += (f"\n\nPrevious draft:\n{json.dumps(previous, ensure_ascii=False)}\n"
                   f"Change it as the candidate asks: {instruction}")
    out = await llm.complete_json(prompt, purpose="prospect_email", max_tokens=800)
    return {"subject": out.get("subject", "").strip(), "body": out.get("body", "").strip()}


async def draft_followup(contact_id: int, instruction: str = "", previous: dict | None = None) -> dict:
    c = db.q1("SELECT * FROM contacts WHERE id=?", (contact_id,))
    co = db.q1("SELECT * FROM companies WHERE id=?", (c["company_id"],))
    first = db.q1("SELECT body FROM messages WHERE contact_id=? AND kind='initial' AND status='sent'", (contact_id,))
    prompt = FOLLOWUP_PROMPT.format(name=c["name"], title=c["title"] or "?", company=co["name"],
                                    previous=first["body"] if first else "")
    if previous and instruction:
        prompt += f"\n\nPrevious draft:\n{previous.get('body')}\nChange it as the candidate asks: {instruction}"
    out = await llm.complete_json(prompt, purpose="prospect_followup", max_tokens=500)
    subject = c["subject"] or "Following up"
    return {"subject": subject if subject.lower().startswith("re:") else f"Re: {subject}",
            "body": out.get("body", "").strip()}


async def redraft(message_id: int, instruction: str) -> dict:
    m = db.q1("SELECT * FROM messages WHERE id=?", (message_id,))
    prev = {"subject": m["subject"], "body": m["body"]}
    fn = draft_followup if m["kind"] == "followup" else draft_initial
    new = await fn(m["contact_id"], instruction, prev)
    db.update("messages", message_id, subject=new["subject"], body=new["body"])
    return new


def sent_today() -> int:
    return db.q1("SELECT COUNT(*) c FROM messages WHERE status='sent' AND sent_at LIKE ?", (today() + "%",))["c"]


async def prepare_batch(progress=None) -> list[int]:
    """Today's drafts: due follow-ups first, then new emails, up to the daily limit. Safe to call twice."""
    say = progress or (lambda *_: None)
    existing = db.q("SELECT id FROM messages WHERE batch_date=? AND status IN ('draft','approved')", (today(),))
    if existing:
        return [r["id"] for r in existing]
    room = max(0, config.DAILY_LIMIT - sent_today())
    ids: list[int] = []

    cutoff = (datetime.now() - timedelta(days=config.FOLLOWUP_DAYS)).isoformat(timespec="seconds")
    for c in db.q("""SELECT id FROM contacts WHERE status='sent' AND followup_at IS NULL AND sent_at <= ?
                     ORDER BY sent_at LIMIT ?""", (cutoff, room)):
        d = await draft_followup(c["id"])
        ids.append(db.run("""INSERT INTO messages(contact_id, kind, subject, body, status, batch_date, created_at)
                             VALUES(?,?,?,?,?,?,?)""", (c["id"], "followup", d["subject"], d["body"], "draft", today(), db.now())))
    room -= len(ids)

    notes = []
    for _ in range(6):  # find enough ready contacts: reveal -> research -> discover, each only if needed
        ready = db.q("""SELECT ct.id FROM contacts ct JOIN companies co ON co.id = ct.company_id
                        WHERE ct.status='revealed' AND co.status != 'skipped'
                        ORDER BY (co.roles_json IS NOT NULL AND co.roles_json != '[]') DESC, ct.tier, ct.id""")
        if len(ready) >= room or room <= 0:
            break
        found = db.q("""SELECT ct.id FROM contacts ct JOIN companies co ON co.id = ct.company_id
                        WHERE ct.status='found' AND co.status != 'skipped' ORDER BY ct.tier, ct.id LIMIT ?""",
                     (room - len(ready),))
        if found:
            for c in found:
                try:
                    await reveal(c["id"])
                except apollo.ApolloOutOfCredits as e:
                    notes.append(f"⚠️ {e}")
                    break
            continue
        fresh = db.q("SELECT id FROM companies WHERE status='new' ORDER BY id LIMIT ?", (max(4, room * 2),))
        if fresh:
            say(f"🔎 Looking for the right people at {len(fresh)} companies…")
            try:
                for co in fresh:
                    await research(co["id"])
            except apollo.ApolloBlocked as e:
                notes.append("⚠️ Apollo people search is blocked for this account. Free accounts need an Apollo login "
                             f"registered with a work/college email. ({str(e)[:100]})")
                break
            continue
        if config.AUTO_DISCOVER and db.get_setting("paused") != "1":
            try:
                new = await discover()
                say(f"🏢 Found {new} new companies (stage {stage()}).")
                if not new:
                    break
            except apollo.ApolloBlocked as e:
                notes.append("Apollo company search isn't available on this plan - send me companies "
                             f"(links, names, screenshots) and I'll do the rest. ({str(e)[:80]})")
                break
            except apollo.ApolloOutOfCredits as e:
                notes.append(f"⚠️ {e}")
                break
        else:
            break
    for n in notes:
        say(n)

    drafted = 0
    for c in db.q("""SELECT ct.id, ct.company_id FROM contacts ct JOIN companies co ON co.id = ct.company_id
                     WHERE ct.status='revealed' AND co.status NOT IN ('skipped', 'too_big')
                     ORDER BY (co.roles_json IS NOT NULL AND co.roles_json != '[]') DESC, ct.tier, ct.id"""):
        if drafted >= room:
            break
        if not await check_size(c["company_id"]):  # e.g. found via a job post: confirm it's small enough
            continue
        drafted += 1
        d = await draft_initial(c["id"])
        try:
            attachment, note, _ = await resume_for(c["id"])
        except Exception as e:  # never block the email on the resume: fall back to the base resume
            log.warning("tailored resume failed: %s", e)
            attachment, note = resume_file(), f"your base resume (tailoring failed: {str(e)[:60]})"
        ids.append(db.run("""INSERT INTO messages(contact_id, kind, subject, body, status, batch_date, created_at,
                             attachment, attach_note) VALUES(?,?,?,?,?,?,?,?,?)""",
                          (c["id"], "initial", d["subject"], d["body"], "draft", today(), db.now(), attachment, note)))
    return ids


# ---- sending, follow-ups, replies -------------------------------------------------------

def resume_file() -> str:
    """Your base resume as <Name>_Resume.pdf (the file name recruiters see)."""
    src = ensure_base_pdf()
    target = config.DATA_DIR / "prospector" / f"{_slug(profile.get('personal.full_name') or 'Candidate')}_Resume.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    if src.exists() and (not target.exists() or target.stat().st_mtime < src.stat().st_mtime):
        shutil.copyfile(src, target)
    return str(target)


def target_role(company) -> tuple[str, str]:
    """The role to pitch at this company (first fitting open role) and its posting URL."""
    roles = json.loads(company["roles_json"] or "[]")
    if not roles:
        return "", ""
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", roles[0]["title"]).split(",")[0].strip()  # "Backend Engineer (Remote)"
    return title, roles[0].get("url") or ""


def role_similarity(target: str, existing: str) -> float:
    """How well a resume made for `existing` fits `target`: mostly 'does it cover every word of the target role',
    plus overall overlap. 'Full-Stack Developer' vs 'Full Stack Developer Intern (AI-Native...)' -> high."""
    t, e = _words(target), _words(existing)
    if not t or not e:
        return 0.0
    return 0.6 * len(t & e) / len(t) + 0.4 * _jaccard(t, e)


async def resume_for(contact_id: int) -> tuple[str, str, bool]:
    """Resume to attach for this contact's company: (pdf path, note, newly made?).
    1) reuse a resume already tailored for a similar role (by either bot) - free
    2) else tailor one for this role with the recruiter/ATS check and keep it for next time
    3) no specific role listed: your base resume"""
    c = db.q1("SELECT * FROM contacts WHERE id=?", (contact_id,))
    co = db.q1("SELECT * FROM companies WHERE id=?", (c["company_id"],))
    role, url = target_role(co)
    if not role:
        return resume_file(), "your base resume (no specific role listed)", False

    best = None
    for r in jobdb.q("""SELECT r.path, r.job_id, j.title FROM resumes r JOIN jobs j ON j.id = r.job_id
                        WHERE r.status='approved' ORDER BY r.id DESC"""):
        score = role_similarity(role, r["title"] or "")
        if score >= 0.7 and Path(r["path"]).exists() and (not best or score > best[0]):
            best = (score, r)
    if best:
        score, r = best
        name = _slug(profile.get("personal.full_name") or "Candidate")
        target = config.DATA_DIR / "prospector" / f"{name}_{_slug(role)}.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(r["path"], target)
        return str(target), f"reused the resume made for '{r['title']}' (job #{r['job_id']}, {score:.0%} match) — ₹0", False

    jd = ""
    if url:  # the real job description makes the tailoring much better
        try:
            _, jd = await jobs.fetch_page_text(url)
        except Exception as e:
            log.info("couldn't read %s: %s", url, e)
    jid, _ = jobdb.add_job(f"text:prospect-role:{_slug(role).lower()}", jd[:15000])
    jobdb.update_job(jid, title=role, company=co["name"], status="saved",
                     description=(jd or f"{role} at {co['name']}. {co['description'] or ''}")[:15000],
                     summary=f"{role} — resume made for cold outreach")
    before = jobdb.q1("SELECT COALESCE(MAX(id),0) m FROM llm_usage")["m"]
    path, content = await generate_tailored(jid)
    jobdb.run("UPDATE resumes SET status='approved' WHERE job_id=? AND path=?", (jid, path))
    cost = jobdb.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM llm_usage WHERE id>?", (before,))["c"]
    ats = content.get("ats") or {}
    score = f", ATS {ats['matched']}/{ats['total']}" if ats.get("total") else ""
    return path, f"new resume tailored for '{role}'{score} — {money(cost)}", True


def money(usd: float) -> str:
    return f"${usd:.4f} (≈ ₹{usd * float(base_config.env('USD_TO_INR', '88')):.2f})"


async def send(message_id: int) -> str:
    m = db.q1("SELECT * FROM messages WHERE id=?", (message_id,))
    c = db.q1("SELECT * FROM contacts WHERE id=?", (m["contact_id"],))
    if m["kind"] == "initial" and already_emailed(c["email"]):
        db.update("messages", message_id, status="dropped", error="already emailed")
        return "skipped (already emailed)"
    try:
        mid = await mailer.send_email(c["email"], m["subject"], m["body"],
                                      [m["attachment"] or resume_file()] if m["kind"] == "initial" else [],
                                      in_reply_to=c["thread_id"] if m["kind"] == "followup" else None,
                                      from_name=profile.get("personal.full_name") or None)
    except Exception as e:
        db.update("messages", message_id, status="failed", error=str(e)[:300])
        return f"failed: {e}"
    db.update("messages", message_id, status="sent", sent_at=db.now(), message_id=mid)
    if m["kind"] == "initial":
        db.update("contacts", c["id"], status="sent", thread_id=mid, subject=m["subject"], sent_at=db.now())
        db.update("companies", c["company_id"], status="contacted", updated_at=db.now())
    else:
        db.update("contacts", c["id"], status="followed_up", followup_at=db.now())
    return "sent"


async def check_replies() -> list[dict]:
    """Replies and bounces for everyone we emailed. Returns new replies for notification."""
    rows = db.q("SELECT * FROM contacts WHERE status IN ('sent','followed_up') AND email IS NOT NULL")
    if not rows:
        return []
    since = min(datetime.fromisoformat(r["sent_at"]).timestamp() for r in rows if r["sent_at"]) - 3600
    addresses = [r["email"] for r in rows]
    replies = await mailer.replies_from(addresses, since)
    bounced = await mailer.bounces_for(addresses, since)
    new = []
    for r in rows:
        e = r["email"].lower()
        if e in replies:
            db.update("contacts", r["id"], status="replied", replied_at=db.now(), reply_snippet=replies[e])
            db.update("companies", r["company_id"], status="replied", updated_at=db.now())
            db.run("UPDATE messages SET status='dropped' WHERE contact_id=? AND status IN ('draft','approved')", (r["id"],))
            co = db.q1("SELECT name FROM companies WHERE id=?", (r["company_id"],))
            new.append({"name": r["name"], "title": r["title"], "company": co["name"], "email": r["email"],
                        "snippet": replies[e]})
        elif e in bounced:
            db.update("contacts", r["id"], status="bounced")
    return new
