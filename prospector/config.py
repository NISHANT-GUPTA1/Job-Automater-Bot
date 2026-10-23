"""Prospector settings. Shares .env (and the AI keys, Gmail, profile, resume) with the job bot."""
from jobapplier import config as base
from jobapplier.config import DATA_DIR, env, env_bool

TELEGRAM_BOT_TOKEN = env("PROSPECTOR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = base.TELEGRAM_CHAT_ID  # same owner chat as the job bot

APOLLO_API_KEY = env("APOLLO_API_KEY")
APOLLO_MONTHLY_CREDITS = int(env("APOLLO_MONTHLY_CREDITS", "100"))  # stop using Apollo credits above this
HUNTER_API_KEY = env("HUNTER_API_KEY")                              # hunter.io (free: 50 searches / month)
HUNTER_MONTHLY_SEARCHES = int(env("HUNTER_MONTHLY_SEARCHES", "50"))
HUNTER_MONTHLY_VERIFICATIONS = int(env("HUNTER_MONTHLY_VERIFICATIONS", "100"))

# emails per day (new + follow-ups). Gmail allows ~500, but a personal address sending much more than ~50 cold
# emails a day starts landing in spam - and this is the same address you apply to jobs with.
DAILY_LIMIT = int(env("PROSPECT_DAILY_LIMIT", "40"))
SEND_HOUR = int(env("PROSPECT_SEND_HOUR", "10"))               # local hour the daily batch is prepared
FOLLOWUP_DAYS = int(env("PROSPECT_FOLLOWUP_DAYS", "5"))        # one follow-up after this many days
CONTACTS_PER_COMPANY = int(env("PROSPECT_CONTACTS_PER_COMPANY", "5"))  # people emailed at one company
# random pause between emails, in seconds (a steady, human-like pace keeps Gmail happy)
SEND_GAP_SECONDS = tuple(int(x) for x in env("PROSPECT_SEND_GAP_SECONDS", "40-100").split("-"))
# LinkedIn (after you log in once with `python linkedin_login.py`): requests to people at companies you emailed.
# LinkedIn allows ~100 requests a week; staying well below that keeps the account safe.
LI_DAILY_INVITES = int(env("LINKEDIN_DAILY_INVITES", "15"))
LI_WEEKLY_INVITES = int(env("LINKEDIN_WEEKLY_INVITES", "90"))
LI_GAP_SECONDS = tuple(int(x) for x in env("LINKEDIN_GAP_SECONDS", "45-150").split("-"))
LI_ACTIVE_HOURS = tuple(int(x) for x in env("LINKEDIN_ACTIVE_HOURS", "10-20").split("-"))  # local time
LI_SEND_NOTES = env_bool("LINKEDIN_SEND_NOTES", False)  # free accounts get ~5 notes a month: plain requests
LI_AUTO_MESSAGE = env_bool("LINKEDIN_AUTO_MESSAGE", False)  # false: you approve each first message in Telegram
LI_EVERY_HOURS = float(env("LINKEDIN_EVERY_HOURS", "3"))
AUTO_DISCOVER = env_bool("PROSPECT_AUTO_DISCOVER", True)        # search Apollo for new companies daily

LOCATIONS = [x.strip() for x in env("PROSPECT_LOCATIONS", "India").split(",") if x.strip()]
ROLE_TITLES = [x.strip() for x in env(
    "PROSPECT_ROLES",
    "software engineer,backend engineer,python developer,full stack developer,react developer,"
    "ai engineer,machine learning engineer,software engineer intern,backend intern,ai intern,sde intern",
).split(",") if x.strip()]

# Company size stages: start with 1-50 employees, widen only when you choose (/stage 2, /stage 3)
STAGES = {1: ["1,10", "11,50"], 2: ["51,100", "101,200"], 3: ["201,500"]}
STAGE_MAX_EMPLOYEES = {1: 50, 2: 200, 3: 500}

# Who to contact, in priority order (tier 1 first)
TIERS = {
    1: ("HR / recruiter", ["recruiter", "talent acquisition", "technical recruiter", "hr", "human resources",
                           "hr manager", "people operations", "hiring manager", "talent partner"]),
    2: ("engineering manager", ["engineering manager", "head of engineering", "cto", "vp engineering",
                                "tech lead", "director of engineering"]),
    3: ("founder", ["founder", "co-founder", "ceo", "chief executive officer"]),
    4: ("general company inbox", []),  # hello@ / contact@ - last resort
}

DB_PATH = DATA_DIR / "prospector.db"
SHEET_PATH = DATA_DIR / "Prospects.xlsx"
