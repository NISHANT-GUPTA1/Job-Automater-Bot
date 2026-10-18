"""Per-job workflow: analyze -> (optional tailored resume) -> apply on site -> cold email."""
import asyncio
import hashlib
import json
import logging
import traceback
from pathlib import Path

from . import config, db, jobs, latex, mailer, memory, profile, tracker
from .asker import JobCancelled, asker, cancelled, current_job
from .browser import apply_to_job
from .llm import llm
from .resume import (ats_coverage, body_text, cover_letter, find_similar_resume, generate_tailored, resume_for_job,
                     telegram_html,
                     reuse_resume, review_caption, review_resume)

log = logging.getLogger(__name__)

queue: asyncio.Queue = asyncio.Queue()


async def worker() -> None:
    """Runs interactive steps one job at a time so questions never get mixed up."""
    while True:
        kind, job_id, extra = await queue.get()
        token = current_job.set(job_id)  # questions asked from here on get a "Skip job" button
        try:
            job = db.get_job(job_id)
            if job_id in cancelled or (kind == "process" and job and job["status"] == "skipped"):
                continue  # you skipped it - don't start it again
            if kind == "process":
                await process_job(job_id, extra)
            elif kind == "apply":
                await apply_step(job_id)
            elif kind == "email":
                await email_flow(job_id, extra.get("to"))
            elif kind == "tailor":
                await tailor_flow(job_id, notes=extra.get("notes", ""))
            elif kind == "cover":
                await cover_letter_flow(job_id, extra.get("notes", ""))
        except JobCancelled:
            db.set_status(job_id, "skipped", "skipped by you")
            await asker.say(f"⏭ Job #{job_id} skipped. (/retry {job_id} if you change your mind)")
        except Exception as e:
            log.error("job %s %s failed: %s", job_id, kind, traceback.format_exc())
            db.log(job_id, "error", f"{kind}: {e}")
            await asker.say(f"❌ Job #{job_id} ({kind}) failed: {str(e)[:500]}")
        finally:
            current_job.reset(token)
            queue.task_done()


async def analyze_and_report(job_id: int, url: str, text: str | None = None) -> dict:
    details = await jobs.analyze(job_id, url, text)
    await asker.say(jobs.job_card(job_id))
    return details


async def process_job(job_id: int, extra: dict) -> None:
    job = db.get_job(job_id)
    details = json.loads(job["details_json"] or "{}")
    if not details:
        details = await analyze_and_report(job_id, job["url"], extra.get("text"))
        job = db.get_job(job_id)

    already_decided = db.q1("SELECT 1 FROM events WHERE job_id=? AND kind IN "
                            "('eligible_override', 'resume_approved', 'status:applying')", (job_id,))
    if details.get("eligible") is False and not already_decided:  # never ask the same job twice
        choice = await asker.ask(f"Job #{job_id} may not fit: {details.get('eligibility_note')}\nApply anyway?",
                                 ["✅ Apply anyway", "⏭ Skip"])
        if not choice or choice.startswith("⏭"):
            db.set_status(job_id, "skipped", "not eligible")
            return
        db.log(job_id, "eligible_override", "you chose to apply anyway")

    approved = db.q1("SELECT 1 FROM resumes WHERE job_id=? AND status='approved'", (job_id,))
    if profile.base_resume_text() and not details.get("outreach") and not approved and \
            (config.ALWAYS_TAILOR or details.get("resume_tailoring_recommended")):
        # every job gets a recruiter-checked resume (a similar approved one is offered first - free)
        await tailor_flow(job_id, details.get("tailoring_reason") or "")

    if not job["url"].startswith("text:"):
        await apply_step(job_id)

    job = db.get_job(job_id)
    to = extra.get("email") or job["contact_email"]
    no_link = job["url"].startswith("text:")
    if (to or no_link) and not job["emailed_at"]:  # screenshot/text jobs: cold email is the way to apply
        await email_flow(job_id, to)
    if db.get_job(job_id)["status"] in ("new", "analyzed", "applying"):
        # finished without applying or emailing (e.g. you chose not to): close it so it never comes back
        db.set_status(job_id, "skipped", "finished without applying or emailing")
    await asker.say(f"🏁 Job #{job_id} done. Status: {db.get_job(job_id)['status']}  (/job {job_id})\n"
                    f"{tracker.job_cost_text(job_id)}")


async def apply_step(job_id: int) -> None:
    resume = resume_for_job(job_id)
    if not Path(resume).exists():
        await asker.say(f"⚠️ No resume found at {resume}. Add resumes/base_resume.pdf, then /retry {job_id}")
        return
    db.set_status(job_id, "applying")
    await asker.say(f"🤖 Applying to job #{job_id}… (tap to watch live)", browser=True)
    result = await apply_to_job(job_id, resume)
    status = {"applied": "applied", "skipped": "skipped"}.get(result["status"], "failed")
    db.set_status(job_id, status, result.get("note", ""))
    emoji = {"applied": "✅", "skipped": "⏭", "failed": "❌"}[status]
    await asker.say(f"{emoji} Job #{job_id}: {status} ({result.get('note', '')})\n{tracker.job_cost_text(job_id)}")


# ---- resume tailoring -------------------------------------------------------

async def tailor_flow(job_id: int, reason: str = "", notes: str = "") -> str:
    # A resume already made for THIS job (e.g. the job was skipped or interrupted after drafting): reuse it
    own = db.q1("""SELECT * FROM resumes WHERE job_id=? AND status IN ('approved', 'draft')
                   ORDER BY (status='approved') DESC, id DESC LIMIT 1""", (job_id,))
    if own and Path(own["path"]).exists() and not notes:
        if own["status"] == "approved":
            return own["path"]
        await asker.send_file(own["path"], f"Resume drafted earlier for job #{job_id}")
        choice = await asker.ask(f"Job #{job_id}: you already have this resume drafted for this job (no extra cost). "
                                 f"Use it?", ["✅ Use it", "✍️ Make a new one"])
        if choice and choice.startswith("✅"):
            db.run("UPDATE resumes SET status='approved' WHERE id=?", (own["id"],))
            db.update_job(job_id, resume_path=own["path"])
            db.log(job_id, "resume_approved", f"reused earlier draft {own['path']}")
            return own["path"]

    similar = find_similar_resume(job_id) if not notes else None  # your own points need a fresh resume
    if similar:
        row, other, score = similar
        choice = await asker.ask(
            f"Job #{job_id}: {reason + chr(10) if reason else ''}You already have an approved resume for a similar job:\n"
            f"#{other['id']} {other['title']} @ {other['company']} ({score:.0%} similar)\n"
            f"Reusing it costs nothing.",
            [f"♻️ Reuse resume from #{other['id']}", "✍️ Tailor a new one", "📄 Use base resume"])
        if choice and choice.startswith("♻️"):
            path = reuse_resume(job_id, row)
            await asker.send_file(path, f"♻️ Reused for job #{job_id} (from #{other['id']}) — $0 Claude cost")
            return path
        if not choice or choice.startswith("📄"):
            return str(config.BASE_RESUME_PATH)

    # 1) Read the base resume like a strict recruiter would for THIS job
    await asker.say(f"🧑‍💼 Job #{job_id}: reviewing your resume like a strict recruiter + ATS for this job…")
    review = await review_resume(job_id, body_text(latex.base_parts()[1]))
    hit, miss = ats_coverage(review["ats_keywords"], body_text(latex.base_parts()[1]))
    await asker.say(
        f"Current resume: {review.get('score', '?')}/100 — {'shortlist' if review.get('verdict') == 'shortlist' else 'would be rejected'}"
        + (f"; ATS keywords {len(hit)}/{len(hit) + len(miss)}" if hit or miss else "")
        + ("\nWhy: " + "; ".join(review["rejection_reasons"][:4]) if review["rejection_reasons"] else ""))

    # 2) Gaps only you can fill: ask once, remember the answer, never invent
    extra_facts = notes  # points you shared with the request are used as facts
    if review["missing_unknown"] and config.RESUME_KEYWORD_MODE == "strict":  # "related" mode adds them itself
        gaps = ", ".join(review["missing_unknown"])
        answer = await asker.ask(
            f"Job #{job_id} wants: {gaps}\nYour resume shows no evidence of these. Do you REALLY have experience "
            f"with any? Reply with what's true (e.g. \"Kafka: used it in my internship ETL pipeline\").",
            ["❌ None of these"])
        if answer and not answer.startswith("❌"):
            extra_facts = answer
            memory.save(f"Experience with {gaps}", answer)

    # 3) Rewrite -> re-review -> fix again if still risky; then your feedback loop
    content, feedback = None, ""
    while True:
        await asker.say(f"✍️ Writing the tailored resume for job #{job_id}…")
        path, content = await generate_tailored(job_id, feedback, content, None if content else review, extra_facts)
        await asker.send_file(path, f"Draft for job #{job_id}\n{review_caption(content)}\n\n{content.get('changes_made', '')}")
        choice = await asker.ask("Use this resume?", ["✅ Approve", "✏️ Change something", "❌ Use base resume"])
        row = db.q1("SELECT id FROM resumes WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,))
        if choice and choice.startswith("✅"):
            db.run("UPDATE resumes SET status='approved' WHERE id=?", (row["id"],))
            db.update_job(job_id, resume_path=path)
            db.log(job_id, "resume_approved", path)
            return path
        if choice and choice.startswith("✏️"):
            feedback = await asker.ask("What should I change?") or ""
            if feedback:
                continue
        db.run("UPDATE resumes SET status='rejected' WHERE id=?", (row["id"],))
        return str(config.BASE_RESUME_PATH)


# ---- cover letter on request ----------------------------------------------------

async def cover_letter_flow(job_id: int, notes: str = "") -> str | None:
    """Draft -> show text + PDF -> your changes -> approve. The approved letter is also used for upload fields."""
    job = db.get_job(job_id)
    text, feedback = "", ""
    while True:
        await asker.say(f"✍️ Writing the cover letter for job #{job_id} ({job['title'] or '?'} @ {job['company'] or '?'})…")
        path, text = await cover_letter(job_id, feedback, text, notes)
        await asker.say(f"📄 Cover letter — job #{job_id}\n\n{telegram_html(text)}", parse_mode="HTML")
        await asker.send_file(path, f"Cover letter for {job['title'] or 'the job'} @ {job['company'] or '?'}")
        choice = await asker.ask("Use this cover letter?", ["✅ Approve", "✏️ Change something", "❌ Cancel"])
        if choice and choice.startswith("✅"):
            db.log(job_id, "cover_letter_approved", path)
            await asker.say(f"✅ Saved. It'll be uploaded whenever an application for job #{job_id} asks for one.")
            return path
        if choice and choice.startswith("✏️"):
            feedback = await asker.ask("What should I change in the cover letter?") or ""
            if feedback:
                continue
        Path(path).unlink(missing_ok=True)  # cancelled: don't upload it later
        return None


# ---- outreach (no job post: email + company + person + role from your message) ----

def create_outreach_job(r: dict, note: str = "") -> int:
    """Track a cold-outreach target like a job, so it shows in /jobs, /stats and the Excel export."""
    role = (r.get("role") or "").strip()
    default_role = (profile.get("preferences.roles_wanted") or "Software Engineer").split(",")[0].strip()
    email = r["email"].strip().lower()
    key = f"text:outreach-{email}-{hashlib.sha1((role + db.now()).encode()).hexdigest()[:6]}"
    job_id, _ = db.add_job(key, "")
    person = (r.get("person_name") or "").strip()
    details = {"outreach": True, "person_name": person, "role": role, "note": note,
               "key_skills": [], "contact_email": email}
    db.update_job(job_id, title=role or default_role, company=(r.get("company") or "").strip(),
                  contact_email=email, details_json=json.dumps(details, ensure_ascii=False),
                  summary=f"Cold outreach to {person or email} at {r.get('company') or '?'}"
                          f" — {role or 'any suitable role'}", status="new")
    db.log(job_id, "outreach_created", f"{person} <{email}> {role}")
    return job_id


# ---- cold email -------------------------------------------------------------

EMAIL_PROMPT = """Write a cold email from the candidate to the recruiter/hiring manager that gets a reply for THIS job.
<job>
Title: {title} at {company}
Contact person: {contact}
{jd}
</job>
How the post asks applicants to apply (follow it exactly, e.g. subject line format): {instructions}
{feedback}
Think first (don't output this): pick the 3 requirements this job cares about most, and for each find the
strongest REAL evidence in the candidate's resume/profile (project, internship result, number). Then write:
- Subject: the post's required format if given, else "Application: <exact job title> – <candidate name>".
- Greeting: the contact's first name if known, else "Dear Hiring Team".
- 1 line: who the candidate is and the exact role applied for at {company}.
- 2-3 short lines/bullets: requirement -> concrete evidence (use the JD's own words; keep real numbers).
- 1 line: why this role/company specifically (from the job text, not generic praise).
- Close: resume attached, available for a call; then name, phone, LinkedIn.
120-170 words, confident and specific, no clichés ("I am writing to express"), no placeholders like [Name].
Never claim skills the candidate doesn't have.
Return JSON: {{"subject": str, "body": str}}"""


OUTREACH_PROMPT = """Write a short cold outreach email from the candidate to a person at a company (no job post).
Recipient: {person} at {company}
Role: {role}
Extra context from the candidate: {note}
{feedback}
Requirements: 110-150 words; greet the recipient by first name if known (else "Dear Hiring Team");
{role_rule} Give 2 real, relevant highlights from the resume; polite, confident, no fluff or clichés;
mention the resume is attached; end with the candidate's name, phone and LinkedIn. No placeholders like [Name].
Return JSON: {{"subject": str, "body": str}}"""


async def draft_email(job_id: int, feedback: str = "", previous: dict | None = None) -> dict:
    job = db.get_job(job_id)
    details = json.loads(job["details_json"] or "{}")
    fb = ""
    if previous:
        fb = f"Previous draft:\n{json.dumps(previous, ensure_ascii=False)}\nUser feedback to apply: {feedback}"
    if details.get("outreach"):
        role = details.get("role") or ""
        return await llm.complete_json(OUTREACH_PROMPT.format(
            person=details.get("person_name") or "unknown name", company=job["company"] or "the company",
            role=role or "not specified", note=details.get("note") or "none", feedback=fb,
            role_rule=(f"ask to be considered for the {role} role;" if role else
                       "ask about any suitable open roles that match the candidate's profile (software/backend engineering);")),
            purpose="cold_email", job_id=job_id, max_tokens=800)
    return await llm.complete_json(
        EMAIL_PROMPT.format(title=job["title"], company=job["company"] or "the company",
                            contact=details.get("contact_name") or "unknown",
                            jd=(job["description"] or "")[:5000] or f"Summary: {job['summary']}\n"
                               f"Key skills: {', '.join(details.get('key_skills', []))}",
                            feedback=fb, instructions=details.get("application_instructions") or "none given"),
        purpose="cold_email", job_id=job_id, max_tokens=800)


async def email_flow(job_id: int, to: str | None = None) -> None:
    job = db.get_job(job_id)
    to = to or job["contact_email"]
    if not to:
        answer = await asker.ask(f"Job #{job_id} ({job['title'] or '?'} @ {job['company'] or '?'}) has no email.\n"
                                 f"Which email should I send the cold email + resume to?", ["⏭ Skip email"])
        to = ", ".join(jobs.extract_emails(answer or ""))
        if not to:
            return
    addresses = [a.strip() for a in to.split(",") if a.strip()]
    if len(addresses) > 1:
        choice = await asker.ask(f"Job #{job_id}: found {len(addresses)} emails. Send to which?",
                                 addresses[:6] + ["📨 All of them", "⏭ Skip email"])
        if not choice or choice.startswith("⏭"):
            return
        to = ", ".join(addresses[:6]) if choice.startswith("📨") else choice
    # don't email the same person twice by accident
    for addr in [a.strip() for a in to.split(",")]:
        prev = db.q1("SELECT e.sent_at, e.job_id, j.title, j.company FROM emails e LEFT JOIN jobs j ON j.id=e.job_id "
                     "WHERE e.status='sent' AND lower(e.to_addr) LIKE ? ORDER BY e.id DESC LIMIT 1",
                     (f"%{addr.lower()}%",))
        if prev:
            choice = await asker.ask(
                f"⚠️ You already emailed {addr} on {prev['sent_at'][:10]} "
                f"(job #{prev['job_id']}: {prev['title'] or '?'} @ {prev['company'] or '?'}). Email again?",
                ["✉️ Yes, draft a new one", "⏭ Skip"])
            if not choice or choice.startswith("⏭"):
                db.log(job_id, "email_skipped", f"already emailed {addr}")
                return
    draft = await draft_email(job_id)
    while True:
        attachment = resume_for_job(job_id)
        if not Path(attachment).exists():
            await asker.say(f"⚠️ No resume at {attachment}; add resumes/base_resume.pdf first.")
            return
        email_id = db.add_email(job_id, to, draft["subject"], draft["body"], attachment)
        await asker.say(f"✉️ Cold email preview (job #{job_id})\nTo: {to}\nSubject: {draft['subject']}\n"
                        f"Attachment: {Path(attachment).name}\n\n{draft['body']}")
        choice = await asker.ask("Send this email?", ["✅ Send", "✏️ Change something", "❌ Cancel"])
        if choice and choice.startswith("✅"):
            try:
                await mailer.send_email(to, draft["subject"], draft["body"], [attachment])
            except Exception as e:
                db.update_email(email_id, status="failed", error=str(e))
                await asker.say(f"❌ Email failed: {e}")
                return
            db.update_email(email_id, status="sent", sent_at=db.now())
            db.update_job(job_id, contact_email=to)
            if db.get_job(job_id)["status"] in ("new", "analyzed", "failed", "skipped"):
                db.set_status(job_id, "emailed", to)
            else:
                db.update_job(job_id, emailed_at=db.now())
                db.log(job_id, "emailed", to)
            tracker.update_mailed_sheet()
            total = db.q1("SELECT COUNT(*) c FROM emails WHERE status='sent'")["c"]
            await asker.say(f"📨 Sent to {to}\n📒 Added to your Mailed Contacts sheet ({total} sent so far — /mailed)")
            return
        if choice and choice.startswith("✏️"):
            feedback = await asker.ask("What should I change in the email?")
            db.update_email(email_id, status="cancelled")
            if feedback:
                draft = await draft_email(job_id, feedback, draft)
                continue
        db.update_email(email_id, status="cancelled")
        return
