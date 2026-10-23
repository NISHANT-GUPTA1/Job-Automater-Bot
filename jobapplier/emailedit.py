"""Apply what you type after "Edit" to an email draft: subject, body, attachment file name - exactly as asked."""
import re
import shutil
from pathlib import Path

from .llm import llm
from .mailer import FORMAT_RULE

EDIT_PROMPT = """You are editing an email draft for the candidate. Apply their instruction exactly and change nothing
they didn't ask for. If they give new wording (e.g. a subject line), use it word for word.
<draft>
Subject: {subject}
Attachment file name: {attachment}
Body:
{body}
</draft>
<instruction>{instruction}</instruction>
Rules: never invent facts that aren't in the candidate's resume/profile; keep the sign-off; {format_rule}
Return JSON: {{"subject": str, "body": str,
"attachment_name": str (the file name to attach; keep the current one unless the instruction asks to rename the
resume/attachment; must end in .pdf),
"apply_to_all": bool (true only if the instruction says to do this for all / every / the other emails too),
"summary": str (what you changed, max 8 words)}}"""


def _clean_name(name: str, fallback: str) -> str:
    name = re.sub(r"[^\w.\- ]", "", Path(name or "").name).strip().replace(" ", "_")
    if not name:
        return fallback
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def rename_attachment(path: str, new_name: str) -> str:
    """A copy of the attachment under the new file name (the original stays for other emails)."""
    src = Path(path)
    if not new_name or new_name == src.name or not src.exists():
        return path
    target = src.with_name(new_name)
    shutil.copyfile(src, target)
    return str(target)


async def edit_email(subject: str, body: str, attachment: str | None, instruction: str,
                     job_id: int | None = None) -> dict:
    """-> {subject, body, attachment, apply_to_all, summary}"""
    current = Path(attachment).name if attachment else "(no attachment)"
    out = await llm.complete_json(EDIT_PROMPT.format(subject=subject, attachment=current, body=body,
                                                     instruction=instruction, format_rule=FORMAT_RULE),
                                  purpose="email_edit", job_id=job_id, max_tokens=1500)
    new_attachment = attachment
    if attachment:
        new_attachment = rename_attachment(attachment, _clean_name(out.get("attachment_name") or "", current))
    return {"subject": (out.get("subject") or subject).strip(), "body": (out.get("body") or body).strip(),
            "attachment": new_attachment, "apply_to_all": bool(out.get("apply_to_all")),
            "summary": (out.get("summary") or "").strip()}
