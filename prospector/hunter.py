"""Hunter.io: people + emails at a company domain (free plan: ~50 searches and 100 verifications a month).

Docs: https://hunter.io/api-documentation/v2 (Domain Search, Email Verifier).
"""
import calendar
import logging
import math
from datetime import date

import httpx

from . import config, db

log = logging.getLogger(__name__)
BASE = "https://api.hunter.io/v2"


class HunterUnavailable(RuntimeError):
    pass


async def _get(path: str, params: dict, kind: str, monthly_cap: int) -> dict:
    if not config.HUNTER_API_KEY:
        raise HunterUnavailable("HUNTER_API_KEY is missing in .env")
    used = db.credits_this_month(f"hunter:{kind}")
    if used >= monthly_cap:
        raise HunterUnavailable(f"monthly Hunter {kind} limit reached ({monthly_cap})")
    today = date.today()  # spread what's left evenly over the rest of the month
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    if db.credits_today(f"hunter:{kind}") >= math.ceil((monthly_cap - used + db.credits_today(f"hunter:{kind}"))
                                                        / days_left):
        raise HunterUnavailable(f"today's share of Hunter {kind}es is used (more tomorrow)")
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.get(BASE + path, params={**params, "api_key": config.HUNTER_API_KEY})
    if r.status_code in (401, 403, 429):
        raise HunterUnavailable(f"{path}: {r.text[:200]}")
    if r.status_code == 202:  # verification still running
        return {"data": {"status": "unknown"}}
    r.raise_for_status()
    db.log_credits(f"hunter:{kind}", 1)
    return r.json()


async def domain_search(domain: str) -> list[dict]:
    """People with emails at this domain: value, first_name, last_name, position, department, seniority,
    confidence, verification{status}. Free plan returns at most 10."""
    data = await _get("/domain-search", {"domain": domain, "limit": 10, "type": "personal"}, "search",
                      config.HUNTER_MONTHLY_SEARCHES)
    return (data.get("data") or {}).get("emails") or []


async def verify(email: str) -> str:
    """'valid', 'invalid', 'accept_all', 'webmail', 'disposable' or 'unknown'."""
    data = await _get("/email-verifier", {"email": email}, "verify", config.HUNTER_MONTHLY_VERIFICATIONS)
    return ((data.get("data") or {}).get("status") or "unknown").lower()
