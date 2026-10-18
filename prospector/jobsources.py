"""Companies hiring developers, from free public sources: Hacker News "Who is hiring?", Remotive, Himalayas."""
import html
import re

import httpx

from jobapplier.jobs import UA

from .careers import RELEVANT, SENIOR

OK_LOCATIONS = ("worldwide", "anywhere", "india", "asia", "apac", "global", "remote")


def _location_ok(text: str) -> bool:
    t = (text or "").lower()
    return not t or any(k in t for k in OK_LOCATIONS)


RESTRICTED = re.compile(r"\b(us|usa|u\.s\.|united states|canada|eu|europe|uk|germany|us[- ]only|us timezones?|"
                        r"onsite|on-site|in[- ]office|hybrid)\b", re.I)
OPEN_LOC = re.compile(r"worldwide|anywhere|global|\bindia\b|bangalore|bengaluru|apac|asia|remote ?\(?(global|world)", re.I)
JUNIOR_OK = re.compile(r"\bintern|junior|new grad|graduate|entry[- ]level|early[- ]career|\bsde ?(i|1)\b", re.I)

HN_PROMPT = """These are job posts from Hacker News "Who is hiring?". For each, extract the details.
<posts>
{posts}
</posts>
Return JSON: {{"jobs": [{{"n": int (post number), "company": str, "website": "company website or empty",
"roles": [str] (software/AI/backend/full-stack/frontend roles only), "email": "contact email in the post or empty
(rewrite 'name [at] x [dot] com' as name@x.com)", "open_to_india": bool (remote worldwide/anywhere/APAC/India, or
an office in India; false if restricted to US/EU/other countries), "junior_ok": bool (interns, new grads, juniors or
roles without a senior requirement)}}]}}"""


async def hn_who_is_hiring(max_posts: int = 60) -> list[dict]:
    """Companies from the latest Hacker News "Who is hiring?" thread that fit: relevant roles, open to India."""
    from jobapplier.llm import llm  # local import: only needed here
    async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers={"User-Agent": UA}) as client:
        try:
            r = await client.get("https://hn.algolia.com/api/v1/search_by_date",
                                 params={"tags": "story,author_whoishiring", "query": "who is hiring", "hitsPerPage": 5})
            story = next(h for h in r.json()["hits"] if "who is hiring" in h["title"].lower())
            item = (await client.get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")).json()
        except (httpx.HTTPError, ValueError, StopIteration):
            return []
    posts = []
    for ch in item.get("children", []):
        text = html.unescape(re.sub(r"<p>", "\n", ch.get("text") or ""))
        text = re.sub(r"<[^>]+>", " ", text).strip()
        head = text.split("\n")[0]
        if not text or not RELEVANT.search(text[:600]):
            continue
        if not OPEN_LOC.search(text[:800]) and (RESTRICTED.search(head) or not re.search(r"remote", head, re.I)):
            continue
        posts.append({"id": ch["id"], "text": text[:1500]})
    posts = posts[:max_posts]
    out: list[dict] = []
    for i in range(0, len(posts), 15):
        chunk = posts[i:i + 15]
        listing = "\n\n".join(f"[{n}] {p['text']}" for n, p in enumerate(chunk))
        try:
            res = await llm.complete_json(HN_PROMPT.format(posts=listing), purpose="prospect_hn", max_tokens=3500)
        except Exception:
            continue
        for j in res.get("jobs") or []:
            n = j.get("n")
            if not isinstance(n, int) or n >= len(chunk) or not j.get("open_to_india") or not j.get("junior_ok"):
                continue
            roles = [r for r in j.get("roles") or [] if RELEVANT.search(r)]
            if roles and j.get("company"):
                out.append({"company": j["company"].strip(), "title": ", ".join(roles[:3]), "website": j.get("website") or "",
                            "email": (j.get("email") or "").strip().lower(), "source": "hn",
                            "url": f"https://news.ycombinator.com/item?id={chunk[n]['id']}"})
    return out


async def hiring_companies(limit: int = 60) -> list[dict]:
    """[{company, title, url, source}] for relevant, non-senior developer roles open to India/worldwide."""
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as client:
        try:
            r = await client.get("https://remotive.com/api/remote-jobs", params={"category": "software-dev", "limit": 150})
            for j in r.json().get("jobs", []):
                if _location_ok(j.get("candidate_required_location")):
                    out.append({"company": j.get("company_name"), "title": j.get("title"), "url": j.get("url"),
                                "source": "remotive"})
        except (httpx.HTTPError, ValueError):
            pass
        try:
            r = await client.get("https://himalayas.app/jobs/api", params={"limit": 100})
            for j in r.json().get("jobs", []):
                places = ", ".join(j.get("locationRestrictions") or [])
                if _location_ok(places):
                    out.append({"company": j.get("companyName"), "title": j.get("title"),
                                "url": j.get("applicationLink"), "source": "himalayas"})
        except (httpx.HTTPError, ValueError):
            pass
    seen, result = set(), []
    for j in out:
        name, title = (j["company"] or "").strip(), (j["title"] or "").strip()
        if not name or not RELEVANT.search(title) or SENIOR.search(title) or name.lower() in seen:
            continue
        seen.add(name.lower())
        result.append(j)
    return result[:limit]
