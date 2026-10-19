"""Companies hiring developers, from free public sources: the YC startup directory, Hacker News "Who is hiring?",
Remotive, Himalayas."""
import asyncio
import html
import json
import re
from datetime import datetime

import httpx

from jobapplier.jobs import UA

from . import config, db
from .careers import RELEVANT, SENIOR

OK_LOCATIONS = ("worldwide", "anywhere", "india", "asia", "apac", "global", "remote")


def _location_ok(text: str) -> bool:
    t = (text or "").lower()
    return not t or any(k in t for k in OK_LOCATIONS)


OPEN_LOC = re.compile(r"worldwide|anywhere|global|\bindia\b|bangalore|bengaluru|apac|asia|remote ?\(?(global|world)", re.I)
JUNIOR_OK = re.compile(r"\bintern|junior|new grad|graduate|entry[- ]level|early[- ]career|\bsde ?(i|1)\b", re.I)

HN_PROMPT = """These are job posts from Hacker News "Who is hiring?". For each, extract the details.
<posts>
{posts}
</posts>
Return JSON: {{"jobs": [{{"n": int (post number), "company": str, "website": "company website or empty",
"roles": [str] (software/AI/backend/full-stack/frontend roles only), "email": "contact email in the post or empty
(rewrite 'name [at] x [dot] com' as name@x.com)", "open_to_india": bool (remote worldwide/anywhere/APAC/India, or
an office in India; false if restricted to US/EU/other countries), "remote": bool (any remote option, even if limited
to some countries), "junior_ok": bool (interns, new grads, juniors or
roles without a senior requirement)}}]}}"""


async def hn_who_is_hiring(max_posts: int = 150) -> list[dict]:
    """Companies from the latest Hacker News "Who is hiring?" thread that fit: relevant roles, remote or India.
    Reads up to max_posts unread posts per call, so the whole thread gets covered over a few days."""
    from jobapplier.llm import llm  # local import: only needed here
    async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers={"User-Agent": UA}) as client:
        try:
            r = await client.get("https://hn.algolia.com/api/v1/search_by_date",
                                 params={"tags": "story,author_whoishiring", "query": "who is hiring", "hitsPerPage": 5})
            story = next(h for h in r.json()["hits"] if "who is hiring" in h["title"].lower())
            item = (await client.get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")).json()
        except (httpx.HTTPError, ValueError, StopIteration):
            return []
    if db.get_setting("hn_story") != str(story["objectID"]):  # a new month's thread
        db.set_setting("hn_story", str(story["objectID"]))
        db.set_setting("hn_read_ids", "[]")
    read = set(json.loads(db.get_setting("hn_read_ids", "[]")))
    posts = []
    for ch in item.get("children", []):
        text = html.unescape(re.sub(r"<p>", "\n", ch.get("text") or ""))
        text = re.sub(r"<[^>]+>", " ", text).strip()
        head = text.split("\n")[0]
        if ch["id"] in read:
            continue
        if not text or not RELEVANT.search(text[:600]):
            continue
        if not OPEN_LOC.search(text[:800]) and not re.search(r"remote", text[:800], re.I):
            continue  # office-only abroad
        posts.append({"id": ch["id"], "text": text[:1500]})
    posts = posts[:max_posts]
    db.set_setting("hn_read_ids", json.dumps(sorted(read | {p["id"] for p in posts})))
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
            if not isinstance(n, int) or n >= len(chunk) or not j.get("junior_ok") or \
                    not (j.get("open_to_india") or j.get("remote")):
                continue
            roles = [r for r in j.get("roles") or [] if RELEVANT.search(r)]
            if roles and j.get("company"):
                out.append({"company": j["company"].strip(), "title": ", ".join(roles[:3]), "website": j.get("website") or "",
                            "email": (j.get("email") or "").strip().lower(), "source": "hn",
                            "roles": [{"title": t, "url": f"https://news.ycombinator.com/item?id={chunk[n]['id']}",
                                       "source": "hn", "location": "Remote" if j.get("open_to_india") else
                                       "Remote (limited to some countries)"} for t in roles[:5]],
                            # size unknown: after YC companies known to be small, at the same location rank
                            "priority": (0, 0 if j.get("open_to_india") else 1, 4, 0),
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


# ---- YC startup directory -----------------------------------------------------------
# Open dataset of YC companies that are hiring (https://github.com/yc-oss/api), with team size and regions.
# Each company's public YC jobs page lists its openings with role type, experience and location.

YC_HIRING = "https://yc-oss.github.io/api/companies/hiring.json"
YC_JUNIOR = {"", "any", "any (new grads ok)", "1+ years", "0-1 years"}
INDIA = re.compile(r"(?-i:\bIN\b)|india|bengaluru|bangalore|mumbai|delhi|gurgaon|gurugram|hyderabad|pune|noida|chennai|"
                   r"kolkata|ahmedabad|jaipur", re.I)
_yc_cache: dict = {}


def location_rank(location: str) -> int | None:
    """0 = remote open worldwide / to India, 1 = remote limited to other countries (US, UK, EU...),
    2 = India office. None = office abroad only (needs a visa) - skipped."""
    loc = location or ""
    remotes = re.findall(r"Remote(?: \(([^)]*)\))?", loc)
    if any(not limit or re.search(r"anywhere|worldwide|global|asia|apac|(?-i:\bIN\b)|india", limit, re.I) for limit in remotes):
        return 0
    if remotes:
        return 1
    return 2 if INDIA.search(loc) else None


NOT_FOR_YOU = re.compile(r"gtm|go[- ]to[- ]market|sales|pcb|hardware|mechanical|electrical|firmware|embedded|"
                        r"blockchain|web3|paused|ex-founders|contract|control system|clinical|civil|chemical|account executive|"
                        r"business development|representative|marketing|creative|designer|recruit|"
                        r"\b[2-9]\+ ?(yrs?|years)", re.I)


def _yc_role_ok(job: dict) -> bool:
    title = job.get("title") or ""
    software = RELEVANT.search(title) or (job.get("role") == "eng" and re.search(r"\bengineer", title, re.I))
    return bool(software and not SENIOR.search(title) and not NOT_FOR_YOU.search(title)
                and (job.get("minExperience") or "").strip().lower() in YC_JUNIOR
                and location_rank(job.get("location") or "") is not None)


async def _yc_directory(client: httpx.AsyncClient) -> list[dict]:
    day = datetime.now().strftime("%Y-%m-%d")
    if _yc_cache.get("day") != day:
        r = await client.get(YC_HIRING)
        r.raise_for_status()
        _yc_cache.update(day=day, companies=r.json())
    return _yc_cache["companies"]


async def _yc_jobs(client: httpx.AsyncClient, slug: str) -> list[dict]:
    try:
        r = await client.get(f"https://www.ycombinator.com/companies/{slug}/jobs")
        m = re.search(r'data-page="([^"]+)"', r.text)
        return (json.loads(html.unescape(m.group(1)))["props"].get("jobPostings") or []) if m else []
    except (httpx.HTTPError, ValueError, KeyError):
        return []


HOME = {"India", "South Asia"}
REMOTE = {"Remote", "Fully Remote", "Partly Remote"}
NOT_A_PLACE = HOME | REMOTE | {"Unspecified"}


async def yc_companies(skip_names: set[str], limit: int = 25, max_pages: int = 80) -> list[dict]:
    """YC startups with a junior-friendly software opening you can take from India.
    Order: up to 50 people first (51-200 only once those run out), then companies abroad (US, UK, Europe...)
    hiring remotely, then India-based teams. Each company's jobs page is checked once a month."""
    small = config.STAGE_MAX_EMPLOYEES[1]
    max_size = config.STAGE_MAX_EMPLOYEES[max(2, int(db.get_setting("stage", "1")))]
    month = datetime.now().strftime("%Y-%m")
    checked = set(json.loads(db.get_setting(f"yc_checked:{month}", "[]")))

    def company_rank(c: dict) -> tuple:
        regions = set(c.get("regions") or [])
        abroad = bool(regions - NOT_A_PLACE)
        place = (0 if "Fully Remote" in regions else 1) if abroad and regions & REMOTE else 2 if abroad else 3
        return (c["team_size"] > small, place, c["team_size"])

    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as client:
        try:
            directory = await _yc_directory(client)
        except (httpx.HTTPError, ValueError):
            return []
        todo = sorted((c for c in directory
                       if c.get("status") == "Active" and c.get("website") and c.get("slug") not in checked
                       and 0 < (c.get("team_size") or 0) <= max_size and c["name"].lower() not in skip_names
                       and set(c.get("regions") or []) & (HOME | REMOTE)),
                      key=company_rank)[:max_pages]
        for i in range(0, len(todo), 4):  # a few pages at a time, politely
            group = todo[i:i + 4]
            for c, postings in zip(group, await asyncio.gather(*(_yc_jobs(client, c["slug"]) for c in group))):
                checked.add(c["slug"])
                roles = [{"title": j["title"], "url": "https://www.ycombinator.com" + (j.get("url") or ""),
                          "source": "yc", "location": j.get("location") or ""} for j in postings if _yc_role_ok(j)]
                # best location first, then software titles
                roles.sort(key=lambda r: (location_rank(r["location"]), not RELEVANT.search(r["title"])))
                if roles:
                    rank = company_rank(c)
                    out.append({"company": c["name"], "website": c["website"], "title": roles[0]["title"],
                                "url": roles[0]["url"], "roles": roles[:5], "employees": c["team_size"],
                                "description": c.get("one_liner") or "", "source": "yc",
                                "priority": (rank[0], location_rank(roles[0]["location"]), rank[1], rank[2])})
            if len(out) >= limit:
                break
            await asyncio.sleep(1)
    db.set_setting(f"yc_checked:{month}", json.dumps(sorted(checked)))
    return sorted(out, key=lambda c: c["priority"])
