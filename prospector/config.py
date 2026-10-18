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

DAILY_LIMIT = int(env("PROSPECT_DAILY_LIMIT", "10"))          # emails per day (new + follow-ups)
SEND_HOUR = int(env("PROSPECT_SEND_HOUR", "10"))               # local hour the daily batch is prepared
FOLLOWUP_DAYS = int(env("PROSPECT_FOLLOWUP_DAYS", "5"))        # one follow-up after this many days
CONTACTS_PER_COMPANY = int(env("PROSPECT_CONTACTS_PER_COMPANY", "1"))
SEND_GAP_MINUTES = (2, 6)                                       # random pause between emails
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
