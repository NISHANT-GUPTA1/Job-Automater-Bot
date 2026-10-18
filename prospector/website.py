"""Email addresses a company publishes on its own website (careers@, hr@, founders@, hello@ ...). Free."""
import asyncio
import re

import httpx

from jobapplier.jobs import UA

PAGES = ("", "/careers", "/jobs", "/join-us", "/work-with-us", "/contact", "/contact-us", "/about", "/about-us",
         "/team", "/company")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HIRING = re.compile(r"^(careers?|jobs?|hr|hiring|talent|recruit\w*|people|join\w*|work|apply|internships?)$", re.I)
FOUNDER = re.compile(r"^(founders?|ceo|cto)$", re.I)
GENERAL = re.compile(r"^(hello|hi|contact|info|team|hey|connect|reach)$", re.I)
SKIP = re.compile(r"^(no-?reply|support|help|billing|sales|press|media|privacy|legal|abuse|security|admin|webmaster|"
                  r"partners?|marketing|accounts?|finance|invoices?)$", re.I)


async def published_emails(domain: str) -> list[dict]:
    """[{email, kind: hiring|founder|general}] found on the company's site, same domain only."""
    found: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": UA}) as client:
        async def fetch(path: str) -> str:
            try:
                r = await client.get(f"https://{domain}{path}")
                return r.text if r.status_code < 400 else ""
            except httpx.HTTPError:
                return ""
        pages = await asyncio.gather(*(fetch(p) for p in PAGES))
    base = domain.split(":")[0].lower()
    for html in pages:
        for e in EMAIL_RE.findall(html.replace("[at]", "@").replace("(at)", "@")):
            e = e.strip(".").lower()
            local, _, host = e.partition("@")
            if not (host == base or host.endswith("." + base)) or SKIP.match(local) or re.search(r"\.(png|jpg|svg|webp)$", e):
                continue
            kind = "hiring" if HIRING.match(local) else "founder" if FOUNDER.match(local) else \
                "general" if GENERAL.match(local) else "person"
            found.setdefault(e, kind)
    return [{"email": e, "kind": k} for e, k in found.items()]
