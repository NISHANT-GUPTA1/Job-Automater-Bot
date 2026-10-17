"""Tailored resumes from the Overleaf LaTeX template, plus cover letters.

The template's preamble and header (name + contact line) are copied verbatim every time;
the LLM only rewrites the body (Education ... Interests) and the PDF title/keywords metadata.
"""
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from . import config, db, latex, profile
from .llm import llm

log = logging.getLogger(__name__)

_REPLACE = {"•": "-", "–": "-", "—": "-", "‘": "'", "’": "'",
            "“": '"', "”": '"', "…": "...", " ": " ", "→": "->"}


def _safe(text) -> str:
    text = str(text or "")
    for k, v in _REPLACE.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "job").strip("_")[:40]


def job_folder(job) -> Path:
    """One folder per job so two jobs with the same role title never overwrite each other."""
    folder = config.GENERATED_DIR / f"{job['id']}_{_slug(job['company'])}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def resume_filename(job) -> str:
    """Always <Full_Name>_<Job_Role>.pdf, e.g. Jane_Doe_Backend_SDE_Intern.pdf"""
    name = _slug(profile.get("personal.full_name") or "Resume")
    role = re.sub(r"\(.*?\)", "", job["title"] or "Resume")  # drop "(Remote)", "(2026 batch)"...
    return f"{name}_{_slug(role)}.pdf"


def ensure_base_pdf() -> Path:
    """(Re)build resumes/base_resume.pdf from base_resume.tex when the .tex is newer."""
    pdf = config.BASE_RESUME_PATH
    if latex.BASE_TEX.exists() and (not pdf.exists() or pdf.stat().st_mtime < latex.BASE_TEX.stat().st_mtime):
        latex.compile_pdf(latex.BASE_TEX.read_text(encoding="utf-8"), pdf)
    return pdf


REVIEW_PROMPT = r"""You are the strictest technical recruiter AND the ATS screening resumes for this exact job.
Candidate facts beyond the resume (profile, learned answers) are in your context.

<job_description>
Title: {title} at {company}
{jd}
</job_description>

<resume>
{resume}
</resume>
{source}
Judge honestly, as if you had 200 applicants and 5 interview slots:
1. Would you reject this resume for THIS job? Give the concrete reasons.
2. List the job's most important ATS keywords (15-25) in the JD's exact wording: hard skills, tools, technologies,
   technical concepts and key responsibilities. Exclude the job title, the company name and generic soft skills
   ("problem-solving", "communication", "analytical thinking").
3. Truth check: {truth_rule} Also flag awkward keyword stuffing.

Return JSON: {{"verdict": "reject|shortlist", "score": 0-100,
 "unsupported_claims": [str] (exact phrases that overclaim or are invented; empty if none),
 "rejection_reasons": [str] (max 6, one short sentence each),
 "ats_keywords": [str],
 "missing_but_supported": [str] (JD keywords the candidate genuinely has per resume/profile/learned answers, but the resume does not show clearly),
 "missing_unknown": [str] (important JD requirements with NO evidence anywhere - only the candidate can say if true; max 5, empty if none),
 "fixes": [str] (max 8, one short sentence each: which bullet to rewrite and how, what to move up, what to cut)}}
Keep every list item short - no long explanations."""


# How far tailoring may go beyond what the base resume literally says (RESUME_KEYWORD_MODE in .env).
# "related": the candidate confirmed they've worked with more than fits on one page, so JD skills related to
#            their real background may be added; hard facts can never be invented.
# "strict":  only what the resume/profile/learned answers literally state.
HARD_FACTS = ("employers, job titles, dates, degrees, grades/CGPA, certifications, metrics/numbers, projects, "
              "years of experience")
TRUTH_RULE_REVIEW = {
    "related": f"list statements in <resume> that invent or change HARD FACTS ({HARD_FACTS}) versus the original, "
               "or claim a skill from an unrelated field. Skills/tools/concepts related to the candidate's real "
               "background are ALLOWED (the candidate confirmed this experience) - do not flag those.",
    "strict": "list every statement in <resume> that is NOT supported by the original resume, the candidate profile, "
              "learned answers or confirmed facts (e.g. a tool, integration type, auth method or responsibility that "
              "appears nowhere in those).",
}
TRUTH_RULE_WRITE = {
    "related": f"- You MAY add the job's skills, tools and concepts that are RELATED to or a natural extension of the "
               f"candidate's real experience (the candidate confirmed hands-on exposure beyond what fits on one page): "
               f"put them in Technical Skills and weave them into the most relevant existing bullets.\n"
               f"- NEVER invent or change hard facts: {HARD_FACTS}. Never add skills from an unrelated field.",
    "strict": "- NEVER claim a tool, integration type, auth method or responsibility the sources don't state - even if "
              "the job wants it. Missing requirements are the candidate's to confirm, not yours to add.",
}


def _mode() -> str:
    return "strict" if config.RESUME_KEYWORD_MODE == "strict" else "related"


async def review_resume(job_id: int, resume_text: str, source_text: str = "") -> dict:
    """Recruiter + ATS review of a resume (plain text) against the job, plus a truth check against
    source_text (the original resume + confirmed facts). One cheap LLM call."""
    job = db.get_job(job_id)
    jd = (job["description"] or job["summary"] or "")[:6000]
    source = (f"\n<original_resume_and_confirmed_facts>\n{source_text[:9000]}\n</original_resume_and_confirmed_facts>\n"
              if source_text else "")
    out = await llm.complete_json(REVIEW_PROMPT.format(title=job["title"], company=job["company"], jd=jd,
                                                       resume=resume_text[:8000], source=source,
                                                       truth_rule=TRUTH_RULE_REVIEW[_mode()]),
                                  purpose="resume_review", job_id=job_id, max_tokens=3500)
    out.setdefault("ats_keywords", [])
    for k in ("rejection_reasons", "missing_but_supported", "missing_unknown", "fixes", "unsupported_claims"):
        out[k] = [str(x) for x in out.get(k) or []]
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#]", "", s.lower())


def ats_coverage(keywords: list[str], resume_text: str) -> tuple[list[str], list[str]]:
    """Exact keyword check, like a simple ATS: which JD keywords literally appear in the resume."""
    text = _norm(resume_text)
    hit, miss = [], []
    for k in dict.fromkeys(k.strip() for k in keywords if k and k.strip()):
        (hit if _norm(k) and _norm(k) in text else miss).append(k)
    return hit, miss


def body_text(body: str) -> str:
    return latex.to_plain_text(body, body_only=True)


TAILOR_PROMPT = r"""Rewrite the candidate's LaTeX resume body so it gets SHORTLISTED for this job by a strict recruiter
and scores ~100 on ATS keyword match - truthfully.

<job_description>
Title: {title} at {company}
{jd}
</job_description>

<current_resume_body>
{base_body}
</current_resume_body>

<recruiter_review>
Verdict: {verdict} ({score}/100)
Why it would be rejected: {reasons}
Fixes to make: {fixes}
JD keywords the candidate genuinely has but the resume doesn't show - work them in: {supported}
All ATS keywords for this job: {keywords}
</recruiter_review>
{extra}{feedback}
How to rewrite:
- Rewrite bullets (not just reorder skills) so they mirror this job's responsibilities, using the JD's exact
  keyword wording wherever it is TRUE for the candidate. Strong action verb + what + tech + measurable result.
- Keep the same section order as the current body (Education first). Inside sections, put the most relevant
  experience bullets/projects/skills first; trim or drop what doesn't help this job.
- Keep every existing number exactly as it is; never invent skills, numbers, tools, roles or projects. Only use
  facts from the resume, profile, learned answers and the extra facts above.
- Output ONLY the body: from \section{{Education}} through the last section. The preamble and header (name,
  email, phone, links) are fixed and added automatically.
- Same LaTeX style/macros as the current body (\section, \textbf, \hfill, itemize, \href, \vspace). Escape
  special characters (\%, \&, \#, \_). No new packages or custom commands.
- Do NOT add joining dates, availability, notice period, "immediate joiner" or similar lines.
- NEVER write placeholders ("[to be added]", "TBD", "XX%", "<...>"). If a fact is unknown, leave it out entirely.
{truth_rule}
- No keyword stuffing: each keyword used naturally, no parenthetical keyword lists, no repeated phrases.
- Must fit on ONE A4 page: about the same length as the current body.

Reply in exactly this format:
===CHANGES===
3-5 short lines: what you changed and which rejection reason each change fixes
===TITLE===
Candidate Name - <target role>
===KEYWORDS===
comma-separated ATS keywords from the job that the resume now genuinely covers
===BODY===
<LaTeX body>"""

FIX_PROMPT = r"""This tailored LaTeX resume body needs a fix.
Problem: {problem}
Fix it while keeping the same content rules and style: real facts only (the original below is the source of truth),
same section order, no placeholders, no keyword stuffing. Reply in the same format
(===CHANGES=== / ===TITLE=== / ===KEYWORDS=== / ===BODY===).
<original_resume_body>
{original}
</original_resume_body>
<body>
{body}
</body>"""


def _parse_sections(text: str) -> dict:
    parts = dict(re.findall(r"===(CHANGES|TITLE|KEYWORDS|BODY)===\s*(.*?)(?=\n===[A-Z]+===|\Z)", text, re.S))
    body = parts.get("BODY", "").strip()
    body = re.sub(r"^```(?:latex|tex)?\s*|\s*```$", "", body)
    body = body.split(r"\end{document}")[0].strip()
    if r"\section{Education}" not in body:
        raise ValueError("LLM reply did not contain a resume body")
    return {"changes_made": parts.get("CHANGES", "").strip(), "title": parts.get("TITLE", "").strip(),
            "keywords": parts.get("KEYWORDS", "").strip().replace("\n", " "), "body": body}


async def _write(job, body: str, review: dict, extra_facts: str = "", feedback: str = "") -> dict:
    details = json.loads(job["details_json"] or "{}")
    prompt = TAILOR_PROMPT.format(
        title=job["title"], company=job["company"], jd=(job["description"] or job["summary"] or "")[:6000],
        base_body=body, verdict=review.get("verdict", "?"), score=review.get("score", "?"),
        reasons="; ".join(review["rejection_reasons"]) or "none", fixes="; ".join(review["fixes"]) or "none",
        supported=", ".join(review["missing_but_supported"]
                            + (review["missing_unknown"] if _mode() == "related" else [])) or "none",
        keywords=", ".join(review["ats_keywords"] or details.get("key_skills", [])),
        truth_rule=TRUTH_RULE_WRITE[_mode()],
        extra=f"\n<extra_candidate_facts>\n{extra_facts}\n</extra_candidate_facts>\n" if extra_facts else "",
        feedback=(f"\n<user_feedback>\n{feedback}\n</user_feedback>\nApply this feedback; it overrides the review.\n"
                  if feedback else ""))
    return _parse_sections(await llm.complete(prompt, purpose="tailor_resume", job_id=job["id"], max_tokens=6000))


PLACEHOLDER_RE = re.compile(r"\[(?:to be |tbd|add|insert|your|placeholder|fill|update|n/?a)[^\]]*\]|\bTBD\b|\bTBA\b|"
                            r"\bXX+\b|\bX{2,}%|<[A-Za-z ]+>|\?\?\?", re.I)


def section_order(body: str) -> list[str]:
    return re.findall(r"\\section\{([^}]+)\}", body)


def reorder_sections(body: str) -> str:
    """Put sections back into the base template's order (the LLM sometimes moves them)."""
    body = re.sub(r"(?m)^[ \t]*%.*\n", "", body)  # decorative comment lines would end up in the wrong place
    marks = list(re.finditer(r"\\section\{([^}]+)\}", body))
    if not marks:
        return body
    pre = body[:marks[0].start()]
    chunks = [(m.group(1), body[m.start():(marks[i + 1].start() if i + 1 < len(marks) else len(body))].rstrip() + "\n\n")
              for i, m in enumerate(marks)]
    base = [s.lower() for s in section_order(latex.base_parts()[1])]
    rank = lambda name: base.index(name.lower()) if name.lower() in base else len(base)
    chunks.sort(key=lambda c: rank(c[0]))  # stable: unknown sections keep their relative order at the end
    return (pre + "".join(c for _, c in chunks)).strip()


async def _compile_fit(job_id: int, content: dict, path: Path) -> dict:
    """Compile to PDF; fix LaTeX errors, page overflow and placeholders with the LLM (max 2 tries)."""
    for attempt in range(3):
        content["body"] = reorder_sections(content["body"])
        bad = PLACEHOLDER_RE.findall(content["body"])
        if bad and attempt < 2:
            problem = (f"It contains placeholders {bad[:5]}. A resume must never contain placeholders: remove those "
                       f"fragments entirely (if a fact is unknown, leave it out).")
            fixed = _parse_sections(await llm.complete(FIX_PROMPT.format(problem=problem, body=content["body"], original=latex.base_parts()[1]),
                                                       purpose="tailor_resume:fix", job_id=job_id, max_tokens=6000))
            fixed["changes_made"] = fixed["changes_made"] or content["changes_made"]
            content = fixed
            continue
        tex = latex.build(content["body"], content["title"] or None, content["keywords"] or None)
        try:
            latex.compile_pdf(tex, path)
            pages = latex.page_count(path)
            short = len(body_text(content["body"])) < 0.85 * len(body_text(latex.base_parts()[1]))
            if (pages == 1 and not short) or attempt == 2:
                return content
            problem = (f"It renders to {pages} pages; it must fit on one page - tighten wording, drop least relevant bullets."
                       if pages > 1 else
                       "It leaves empty space at the bottom of the page: restore the most job-relevant bullets/details "
                       "from the original resume (real facts only, same section order) so it fills one page.")
        except RuntimeError as e:
            if attempt == 2:
                raise
            problem = f"LaTeX error:\n{e}"
        fixed = _parse_sections(await llm.complete(FIX_PROMPT.format(problem=problem, body=content["body"], original=latex.base_parts()[1]),
                                                   purpose="tailor_resume:fix", job_id=job_id, max_tokens=6000))
        fixed["changes_made"] = fixed["changes_made"] or content["changes_made"]
        content = fixed
    return content


async def generate_tailored(job_id: int, feedback: str = "", previous: dict | None = None,
                            review: dict | None = None, extra_facts: str = "") -> tuple[str, dict]:
    """Review as a strict recruiter -> rewrite to fix every rejection reason -> re-review -> fix once more if
    it would still be rejected. Returns (pdf path, content incl. final review + ATS keyword coverage)."""
    if not latex.BASE_TEX.exists():
        raise RuntimeError(f"Put your LaTeX resume at {latex.BASE_TEX} first.")
    job = db.get_job(job_id)
    start_body = previous["body"] if previous else latex.base_parts()[1]
    if review is None:
        review = await review_resume(job_id, body_text(start_body))

    path = job_folder(job) / resume_filename(job)  # each new draft replaces the previous one
    content = await _compile_fit(job_id, await _write(job, start_body, review, extra_facts, feedback), path)

    # Truth check + recruiter re-review; fix overclaims / weak spots (max 2 rounds keeps cost bounded)
    source = "\n".join(x for x in (body_text(latex.base_parts()[1]), extra_facts, feedback) if x)
    final = await review_resume(job_id, body_text(content["body"]), source)
    for _ in range(2):
        unsupported = final["unsupported_claims"]
        weak = not feedback and (final.get("verdict") == "reject" or int(final.get("score") or 0) < 80)
        if not unsupported and not weak:
            break
        final["fixes"] = [f"REMOVE or rewrite truthfully (not backed by real experience): {u}" for u in unsupported] \
            + final["fixes"]
        revised = await _compile_fit(job_id, await _write(job, content["body"], final, extra_facts), path)
        revised["changes_made"] = content["changes_made"] + "\n" + revised["changes_made"]
        content = revised
        final = await review_resume(job_id, body_text(content["body"]), source)

    # ATS floor: keep working the job's missing keywords in until coverage >= RESUME_MIN_ATS (max 3 passes)
    keywords = review["ats_keywords"] or final["ats_keywords"]
    base_hit, _ = ats_coverage(keywords, body_text(latex.base_parts()[1]))
    for _ in range(3):
        hit, miss = ats_coverage(keywords, body_text(content["body"]))
        if not keywords or 100 * len(hit) / len(keywords) >= config.RESUME_MIN_ATS:
            break
        prompt = KEYWORD_PROMPT.format(target=config.RESUME_MIN_ATS, missing=", ".join(miss),
                                       truth_rule=TRUTH_RULE_WRITE[_mode()], body=content["body"],
                                       jd=(job["description"] or job["summary"] or "")[:4000])
        more = _parse_sections(await llm.complete(prompt, purpose="tailor_resume:keywords", job_id=job_id,
                                                  max_tokens=6000))
        more["changes_made"] = content["changes_made"] + "\n" + more["changes_made"]
        content = await _compile_fit(job_id, more, path)
    hit, miss = ats_coverage(keywords, body_text(content["body"]))

    content["review"] = {"verdict": final.get("verdict"), "score": final.get("score"),
                         "remaining": final["rejection_reasons"], "missing_unknown": final["missing_unknown"]}
    content["ats"] = {"matched": len(hit), "total": len(keywords), "missing": miss,
                      "added": [k for k in hit if k not in base_hit]}
    db.add_resume(job_id, str(path), json.dumps(content, ensure_ascii=False), "draft", feedback)
    return str(path), content


KEYWORD_PROMPT = r"""This tailored LaTeX resume body must reach at least {target}% ATS keyword match for the job below.
Still missing (use these EXACT words): {missing}

Work each missing keyword in naturally: add it to the right Technical Skills line and/or weave it into the most
relevant existing bullet (one mention is enough).
{truth_rule}
Keep ONE page (tighten other wording if needed), the same section order and LaTeX style, no placeholders,
no comma-dumps of keywords in bullets.
<job_description>
{jd}
</job_description>
<body>
{body}
</body>
Reply in the same format (===CHANGES=== / ===TITLE=== / ===KEYWORDS=== / ===BODY===)."""


def review_caption(content: dict) -> str:
    """Short scorecard shown with each resume draft."""
    r, a = content.get("review") or {}, content.get("ats") or {}
    lines = []
    if a.get("total"):
        lines.append(f"🎯 ATS keywords: {a['matched']}/{a['total']} ({100 * a['matched'] // a['total']}%)")
        if a.get("added"):
            lines.append("➕ Added from the job description (be ready to discuss): " + ", ".join(a["added"][:12]))
    if r.get("score") is not None:
        lines.append(f"🧑‍💼 Recruiter check: {r.get('score')}/100 — {'✅ shortlist' if r.get('verdict') == 'shortlist' else '⚠️ still risky'}")
    if a.get("missing"):
        lines.append("Not in resume: " + ", ".join(a["missing"][:8]))
    if r.get("remaining") and r.get("verdict") != "shortlist":
        lines.append("Remaining concerns: " + "; ".join(r["remaining"][:3]))
    return "\n".join(lines)


# ---- reuse: avoid paying to tailor again for a similar job -------------------

_TITLE_STOP = {"and", "of", "the", "a", "an", "for", "in", "at", "with", "to", "i", "ii", "iii", "level"}


_ABBREV = {"sde": "software development engineer", "swe": "software engineer", "dev": "developer",
           "ml": "machine learning", "ai": "artificial intelligence", "qa": "quality assurance test",
           "sdet": "software development engineer test", "fe": "frontend", "be": "backend",
           "fullstack": "full stack", "devops": "devops", "intern": "intern", "internship": "intern",
           "apis": "api", "js": "javascript", "ts": "typescript", "dsa": "data structures algorithms"}


def _words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9+#]+", (text or "").lower())
    expanded = " ".join(_ABBREV.get(w, w) for w in words).split()
    return {w for w in expanded if w not in _TITLE_STOP}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def job_similarity(job_a, job_b) -> float:
    """0..1 from role-title words (40%) and required skills (60%). Local, no LLM cost."""
    skills = lambda j: _words(" ".join(json.loads(j["details_json"] or "{}").get("key_skills", [])))
    return 0.4 * _jaccard(_words(job_a["title"]), _words(job_b["title"])) + 0.6 * _jaccard(skills(job_a), skills(job_b))


def find_similar_resume(job_id: int, threshold: float = 0.4):
    """Best approved tailored resume from another job that fits this one. Returns (row, job, score) or None."""
    job = db.get_job(job_id)
    best = None
    for r in db.q("""SELECT r.*, j.id jid FROM resumes r JOIN jobs j ON j.id = r.job_id
                     WHERE r.status='approved' AND r.job_id != ? AND COALESCE(r.feedback, '') NOT LIKE 'reused%'
                     ORDER BY r.id DESC""", (job_id,)):
        if not Path(r["path"]).exists():
            continue
        other = db.get_job(r["jid"])
        score = job_similarity(job, other)
        if score >= threshold and (not best or score > best[2]):
            best = (r, other, score)
    return best


def reuse_resume(job_id: int, src_row) -> str:
    """Copy an approved resume to this job as <Name>_<New_Role>.pdf (rebuilt locally with the new
    PDF title when the .tex is available - no LLM call) and mark it approved for this job."""
    job = db.get_job(job_id)
    target = job_folder(job) / resume_filename(job)
    src_tex = Path(src_row["path"]).with_suffix(".tex")
    content = json.loads(src_row["content_json"] or "{}")
    try:
        if not content.get("body"):
            raise ValueError("no body")
        name = profile.get("personal.full_name") or "Candidate"
        latex.compile_pdf(latex.build(content["body"], f"{name} - {job['title']}", content.get("keywords")), target)
    except Exception:
        shutil.copyfile(src_row["path"], target)
        if src_tex.exists():
            shutil.copyfile(src_tex, target.with_suffix(".tex"))
    db.add_resume(job_id, str(target), src_row["content_json"], "approved", f"reused from job #{src_row['job_id']}")
    db.update_job(job_id, resume_path=str(target))
    db.log(job_id, "resume_reused", f"from job #{src_row['job_id']}")
    return str(target)


COVER_PROMPT = """Write a cover letter that makes a strict hiring manager want to interview the candidate for THIS job.
<job>
Title: {title} at {company}
Contact person: {contact}
{jd}
</job>
{notes}{previous}{feedback}
Think first (don't output this): pick the 3 requirements this job cares about most and the strongest real evidence
for each - first from the candidate's own points above (if any), then their resume/profile/learned answers
(projects, internship results, numbers).
Write:
- Greeting: "Dear <contact first name>," if known, else "Dear Hiring Manager,".
- Para 1 (2-3 lines): the exact role at {company} and a specific reason this role/company fits (from the job text).
- Para 2-3: requirement -> concrete evidence, using the job's own keywords; keep real numbers exactly.
  Skills related to the candidate's real background may be mentioned; never invent employers, titles, dates,
  grades, numbers, certifications or projects.
- Closing para: what they'd bring + ask for a conversation; resume attached.
- Sign-off: "Sincerely," then the candidate's full name on the next line.
250-350 words. Confident, specific, no cliches ("I am writing to express my interest"), no placeholders like [Name],
no availability/joining-date lines. Plain text, except: mark the 5-8 most important phrases (key achievements with
numbers, skills that match the job, the candidate's own highlighted points) with **double asterisks** - never more than 8."""

_TEX_ESCAPES = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
                "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def _tex_escape(text: str) -> str:
    return "".join(_TEX_ESCAPES.get(ch, ch) for ch in text)


def _bold(tex: str) -> str:
    """**phrase** -> \\textbf{phrase} (highlights in the PDF)."""
    return re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", tex)


def plain(text: str) -> str:
    """Letter text without the ** markers (for places that can't show bold)."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


def telegram_html(text: str) -> str:
    """Letter text for a Telegram HTML message: **phrase** -> <b>phrase</b>."""
    import html
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text))


def _cover_pdf(job, text: str, path: Path) -> None:
    """Same header as the resume (from the Overleaf template), then the letter. Falls back to a plain PDF."""
    try:
        head, _, end = latex.base_parts()
        paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
        body = (r"\vspace{14pt}" + "\n" + _tex_escape(datetime.now().strftime("%d %B %Y")) + r"\\[10pt]" + "\n"
                + "\n\n".join(_bold(_tex_escape(p)).replace("\n", r"\\" + "\n") for p in paras))
        body = r"{\fontsize{11}{15}\selectfont" + "\n" + r"\setlength{\parskip}{9pt}" + "\n" + body + "\n}"
        latex.compile_pdf(f"{latex.set_pdf_meta(head, f'{job_name()} - Cover Letter', None)}{body}\n\n{end}", path)
    except Exception as e:  # LaTeX unavailable or failed: simple but clean PDF
        log.info("cover letter LaTeX failed (%s) - using plain PDF", e)
        pdf = FPDF(format="A4")
        pdf.set_margins(20, 20, 20)
        pdf.add_page()
        pdf.set_font("Helvetica", "", 11)
        pdf.multi_cell(pdf.epw, 6, _safe(f"{datetime.now():%d %B %Y}\n\n{plain(text).strip()}"))
        pdf.output(str(path))


def job_name() -> str:
    return profile.get("personal.full_name") or "Candidate"


def cover_letter_path(job) -> Path:
    return job_folder(job) / f"{_slug(job_name())}_Cover_Letter.pdf"


async def cover_letter(job_id: int, feedback: str = "", previous: str = "", notes: str = "") -> tuple[str, str]:
    """Write (or rewrite with feedback) the cover letter for a job. Returns (pdf path, letter text with **bold**).
    notes = the candidate's own points to include/highlight (true facts they shared with the request)."""
    job = db.get_job(job_id)
    details = json.loads(job["details_json"] or "{}")
    text = await llm.complete(COVER_PROMPT.format(
        notes=(f"\n<candidate_points>\nThe candidate asked to include and emphasize these (they are true):\n{notes}\n"
               f"</candidate_points>\n") if notes else "",
        title=job["title"], company=job["company"] or "the company",
        contact=details.get("contact_name") or details.get("person_name") or "unknown",
        jd=(job["description"] or "")[:5000] or f"Summary: {job['summary']}",
        previous=f"\n<previous_draft>\n{previous}\n</previous_draft>\n" if previous else "",
        feedback=f"<candidate_feedback>\n{feedback}\n</candidate_feedback>\nApply this feedback to the previous draft.\n"
                 if feedback else ""),
        purpose="cover_letter", job_id=job_id, max_tokens=1500)
    text = text.strip()
    path = cover_letter_path(job)
    _cover_pdf(job, text, path)
    return str(path), text


async def cover_letter_pdf(job_id: int) -> str:
    """For upload fields during applying: the approved/existing cover letter, else a fresh one."""
    path = cover_letter_path(db.get_job(job_id))
    if path.exists():
        return str(path)
    return (await cover_letter(job_id))[0]


def resume_for_job(job_id: int) -> str:
    """Approved tailored resume if there is one, else a copy of the base resume - both named
    <Full_Name>_<Job_Role>.pdf, since this is the file name recruiters and portals see."""
    row = db.q1("SELECT path FROM resumes WHERE job_id=? AND status='approved' ORDER BY id DESC LIMIT 1", (job_id,))
    if row and Path(row["path"]).exists():
        return row["path"]
    base = ensure_base_pdf()
    if not base.exists():
        return str(base)
    target = job_folder(db.get_job(job_id)) / resume_filename(db.get_job(job_id))
    shutil.copyfile(base, target)
    return str(target)
