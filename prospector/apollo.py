"""Apollo.io API: company search, job postings, people search (free) and email reveal (1 credit each).

Endpoints and parameters follow https://docs.apollo.io (organization search, organization job postings,
people API search, people enrichment). Free accounts need an Apollo login registered with a work email.
"""
import logging

import httpx

from . import config, db

log = logging.getLogger(__name__)
BASE = "https://api.apollo.io/api/v1"


class ApolloBlocked(RuntimeError):
    """The account/plan can't use this endpoint (403/401) - the bot falls back instead of failing."""


class ApolloOutOfCredits(RuntimeError):
    pass


async def _call(method: str, path: str, params: dict | None = None, credits: int = 0) -> dict:
    if not config.APOLLO_API_KEY:
        raise ApolloBlocked("APOLLO_API_KEY is missing in .env")
    if credits and db.credits_this_month() + credits > config.APOLLO_MONTHLY_CREDITS:
        raise ApolloOutOfCredits(f"monthly Apollo credit cap reached ({config.APOLLO_MONTHLY_CREDITS})")
    headers = {"x-api-key": config.APOLLO_API_KEY, "Content-Type": "application/json", "Cache-Control": "no-cache"}
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.request(method, BASE + path, params=params, headers=headers, json={} if method == "POST" else None)
    if r.status_code in (401, 403):
        raise ApolloBlocked(f"{path}: {r.text[:200]}")
    if r.status_code == 429:
        raise ApolloBlocked(f"{path}: rate limited by Apollo, try later")
    r.raise_for_status()
    db.log_credits(path, credits)
    return r.json()


async def search_companies(stage: int, page: int = 1, per_page: int = 25) -> list[dict]:
    """Small companies (by stage) in your locations that recently posted relevant jobs. 1 credit per page."""
    params = {
        "organization_num_employees_ranges[]": config.STAGES[stage],
        "organization_locations[]": config.LOCATIONS,
        "q_organization_job_titles[]": config.ROLE_TITLES,
        "page": page, "per_page": per_page,
    }
    data = await _call("POST", "/mixed_companies/search", params, credits=1)
    return data.get("organizations") or data.get("accounts") or []


async def job_postings(org_id: str) -> list[dict]:
    """A company's current job postings (title, url, city...). 1 credit."""
    data = await _call("GET", f"/organizations/{org_id}/job_postings", {"page": 1, "per_page": 50}, credits=1)
    return data.get("organization_job_postings") or []


async def search_people(domain: str | None, org_id: str | None, titles: list[str], per_page: int = 10) -> list[dict]:
    """People at a company with these titles. No emails here and 0 credits."""
    params = {"person_titles[]": titles, "per_page": per_page, "page": 1}
    if org_id:
        params["organization_ids[]"] = [org_id]
    elif domain:
        params["q_organization_domains_list[]"] = [domain]
    data = await _call("POST", "/mixed_people/api_search", params, credits=0)
    return data.get("people") or []


async def reveal_email(person_id: str) -> dict:
    """The person's work email (1 credit when found). Returns the 'person' object."""
    if db.credits_this_month() + 1 > config.APOLLO_MONTHLY_CREDITS:
        raise ApolloOutOfCredits(f"monthly Apollo credit cap reached ({config.APOLLO_MONTHLY_CREDITS})")
    data = await _call("POST", "/people/match", {"id": person_id, "reveal_personal_emails": "false"})
    person = data.get("person") or {}
    if person.get("email"):  # Apollo charges only when it returns data
        db.log_credits("/people/match", 1)
    return person


async def enrich_company(domain: str) -> dict:
    """Company details by website: estimated_num_employees, short_description, industry... 1 credit (free plan OK)."""
    data = await _call("GET", "/organizations/enrich", {"domain": domain}, credits=1)
    return data.get("organization") or {}


async def check_access() -> dict[str, str]:
    """Which Apollo features this key/plan can use (spends at most 1 credit, for company search)."""
    out = {}
    for name, coro in (("people search (free)", search_people("apollo.io", None, ["recruiter"], 1)),
                       ("company search (1 credit/page)", search_companies(1, 1, 1))):
        try:
            await coro
            out[name] = "OK"
        except ApolloBlocked as e:
            out[name] = f"blocked: {str(e)[:120]}"
        except Exception as e:
            out[name] = f"error: {str(e)[:120]}"
    return out
