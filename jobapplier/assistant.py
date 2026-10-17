"""Understands free-form Telegram messages: answers, requests, instructions, questions - one cheap LLM call."""
import re

from . import db, tracker
from .llm import llm

ROUTER_PROMPT = """You are the candidate's personal job-search assistant, chatting with them on Telegram. The bot
also applies to jobs, writes resumes/cover letters/cold emails and tracks everything.

Question the bot is currently waiting on (may be empty): {pending}
Recent jobs (id | title | company | status):
{jobs}
Tracker: {stats}

The candidate's message:
<<<
{text}
>>>

Decide the intent:
- "answer": the message answers the waiting question above (only if it clearly does).
- "job_post": they share a job (link or full posting) to apply to.
- "outreach": they ask to cold-email specific people / email addresses.
- "cover_letter": they want a cover letter (for a pasted job/requirements, a link, or an existing job #id).
- "tailor_resume": they want a resume made/tailored for given requirements, a link or job #id (just the resume).
- "action": retry / skip / change status / email / resume for an existing job id.
- "remember": a standing instruction or preference for all future work (e.g. "always pick LinkedIn as source",
  "don't apply to jobs in Pune", "keep cover letters under 250 words").
- "chat": anything else - a question or request you can answer yourself right now (e.g. write skills keywords,
  advice, interview prep, answers about their jobs from the tracker above).

Return JSON: {{"intent": str, "job_id": int or null, "action": "retry|skip|status|email|resume" or null,
"status": "interview|offer|rejected|applied|skipped|ghosted" or null,
"job_text": "the job description/requirements text from the message, verbatim, if any (else empty)",
"notes": "the candidate's own points/instructions for the document - what to include, mention or highlight (e.g. achievements, skills, tone) - verbatim, else empty",
"url": "job link from the message if any (else empty)",
"reply": "chat: your full helpful answer (plain text, concise, use only real facts about the candidate);
          remember: a short confirmation of what you'll do from now on; otherwise: one short line"}}"""


def _context() -> tuple[str, str]:
    jobs = "\n".join(f"{r['id']} | {r['title'] or '?'} | {r['company'] or '?'} | {r['status']}"
                     for r in db.q("SELECT id, title, company, status FROM jobs ORDER BY id DESC LIMIT 12")) or "none"
    counts = ", ".join(f"{r['status']}: {r['c']}" for r in db.q("SELECT status, COUNT(*) c FROM jobs GROUP BY status"))
    sent = db.q1("SELECT COUNT(*) c FROM emails WHERE status='sent'")["c"]
    return jobs, f"{counts}; cold emails sent: {sent}"


async def route(text: str, pending: str = "") -> dict:
    jobs, stats = _context()
    out = await llm.complete_json(ROUTER_PROMPT.format(pending=pending or "none", jobs=jobs, stats=stats,
                                                       text=text[:6000]),
                                  purpose="chat_router", max_tokens=2500)
    out["intent"] = (out.get("intent") or "chat").strip().lower()
    try:
        out["job_id"] = int(out["job_id"]) if out.get("job_id") not in (None, "") else None
    except (TypeError, ValueError):
        out["job_id"] = None
    return out


def quick_answer(text: str, pending: str) -> bool:
    """Obvious answers to the waiting question, recognised without an LLM call."""
    t = text.strip()
    if re.search(r"verification code|security code|the code", pending, re.I) and re.fullmatch(r"[A-Za-z0-9-]{4,12}", t):
        return True
    if re.search(r"tile numbers", pending, re.I) and re.fullmatch(r"[\d ,]+", t):
        return True
    if re.search(r"which email|email should i send", pending, re.I) and "@" in t and len(t) < 200:
        return True
    return False
