"""Central configuration, loaded from .env at the project root."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
RESUME_DIR = ROOT / "resumes"
GENERATED_DIR = RESUME_DIR / "generated"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"
DB_PATH = DATA_DIR / "tracker.db"
PROFILE_PATH = ROOT / "profile.yaml"
BASE_RESUME_PATH = RESUME_DIR / "base_resume.pdf"

for _d in (DATA_DIR, RESUME_DIR, GENERATED_DIR, SCREENSHOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, str(default)).lower() in ("1", "true", "yes", "on")


# Telegram
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")  # numeric id; only this chat may control the bot
if not TELEGRAM_CHAT_ID.lstrip("-").isdigit() or TELEGRAM_CHAT_ID == TELEGRAM_BOT_TOKEN.split(":")[0]:
    # a @username or the bot's own id was given: the bot replies with the real id on /start
    TELEGRAM_CHAT_ID = ""

# LLM providers, tried in this order: Anthropic -> OpenAI -> Gemini -> Groq
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-haiku-5-5")
ANTHROPIC_EFFORT = env("ANTHROPIC_EFFORT", "low")
OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4.1-mini")
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL", "llama-3.3-70b-versatile")

# Gmail (SMTP to send, IMAP to read verification mails) - needs a Google App Password
GMAIL_ADDRESS = env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD").replace(" ", "")
# The one fixed email used for every job-site signup/login
APPLY_EMAIL = env("APPLY_EMAIL", GMAIL_ADDRESS)
# ...and the one password for every job-site account (created automatically on first use if empty)
APPLY_PASSWORD = env("APPLY_PASSWORD")

# Behaviour
HEADLESS = env_bool("HEADLESS", False)
AUTO_SUBMIT = env_bool("AUTO_SUBMIT", False)  # False = ask before the final submit click
MAX_STEPS_PER_JOB = int(env("MAX_STEPS_PER_JOB", "30"))
QUESTION_TIMEOUT_MIN = int(env("QUESTION_TIMEOUT_MIN", "60"))
ALWAYS_TAILOR = env_bool("ALWAYS_TAILOR", True)  # tailor + recruiter-check the resume for every job
RESUME_MIN_ATS = int(env("RESUME_MIN_ATS", "85"))  # tailored resumes must reach this ATS keyword match (%)
# related = may add JD skills related to your real background (you confirmed); strict = only what the resume says
RESUME_KEYWORD_MODE = env("RESUME_KEYWORD_MODE", "related").lower()
BROWSER_VIEW_URL = env("BROWSER_VIEW_URL")  # on a server: noVNC link to watch/control the bot's browser
SECRET_KEY = env("SECRET_KEY")  # Fernet key for stored passwords; auto-generated if empty


def browser_hint() -> str:
    """Link to the bot's browser (server setups), appended to 'do it in the browser' messages."""
    return f"\n🖥 Open the bot's browser: {BROWSER_VIEW_URL}" if BROWSER_VIEW_URL else ""
