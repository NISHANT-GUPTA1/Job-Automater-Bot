"""Open roles from a company's public job board (Greenhouse, Lever, Ashby) - free, no Apollo credits."""
import asyncio
import re

import httpx

from . import config

RELEVANT = re.compile(r"software|backend|back-end|back end|python|full ?stack|react|frontend|front-end|\bai\b|"
                      r"machine learning|\bml\b|\bsde\b|developer|engineering intern", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|director|head|manager|vp|architect)\b", re.I)


def slugs(name: str, domain: str) -> list[str]:
    base = (domain or "").split(".")[0]
    clean = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    dashed = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return [s for s in dict.fromkeys([base, clean, dashed]) if s]


async def _get(client: httpx.AsyncClient, url: str):
    try:
        r = await client.get(url)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def open_roles(name: str, domain: str) -> list[dict]:
    """Relevant, non-senior open roles: [{title, url, source}]. Empty if no public board was found."""
    roles: list[dict] = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for slug in slugs(name, domain):
            gh, lv, ab = await asyncio.gather(
                _get(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
                _get(client, f"https://api.lever.co/v0/postings/{slug}?mode=json"),
                _get(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}"))
            for j in (gh or {}).get("jobs", []) if isinstance(gh, dict) else []:
                roles.append({"title": j.get("title", ""), "url": j.get("absolute_url", ""), "source": "greenhouse"})
            for j in lv if isinstance(lv, list) else []:
                roles.append({"title": j.get("text", ""), "url": j.get("hostedUrl", ""), "source": "lever"})
            for j in (ab or {}).get("jobs", []) if isinstance(ab, dict) else []:
                roles.append({"title": j.get("title", ""), "url": j.get("jobUrl", ""), "source": "ashby"})
            if roles:
                break
    return filter_roles(roles)


def filter_roles(roles: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in roles:
        t = (r.get("title") or "").strip()
        if t and RELEVANT.search(t) and not SENIOR.search(t) and t.lower() not in seen:
            seen.add(t.lower())
            out.append(r)
    return out[:8]
