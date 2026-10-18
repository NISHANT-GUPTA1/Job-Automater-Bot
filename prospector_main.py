r"""Prospector bot - run with:  .venv\Scripts\python prospector_main.py          (starts the bot)
                                .venv\Scripts\python prospector_main.py --check  (checks setup + Apollo access)"""
import asyncio
import logging
import socket
import sys
from logging.handlers import RotatingFileHandler

from prospector import config


def check() -> None:
    from prospector import apollo, db
    ok = lambda b: "OK " if b else "MISSING"
    print(f"[{ok(config.TELEGRAM_BOT_TOKEN)}] PROSPECTOR_TELEGRAM_BOT_TOKEN (a second bot from @BotFather)")
    print(f"[{ok(config.TELEGRAM_CHAT_ID)}] TELEGRAM_CHAT_ID (same as the job bot)")
    print(f"[{ok(config.APOLLO_API_KEY)}] APOLLO_API_KEY")
    print(f"[{ok(config.HUNTER_API_KEY)}] HUNTER_API_KEY (optional, hunter.io free plan)")
    from prospector.bot import access_report
    print(asyncio.run(access_report()))
    print(f"Daily limit {config.DAILY_LIMIT}, batch at {config.SEND_HOUR}:00, follow-up after {config.FOLLOWUP_DAYS} days, "
          f"credits used this month {db.credits_this_month()}/{config.APOLLO_MONTHLY_CREDITS}")


def main() -> None:
    handlers = [RotatingFileHandler(config.DATA_DIR / "prospector.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")]
    if sys.stdout:  # no console when run hidden with pythonw
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if "--check" in sys.argv:
        check()
        return
    if not config.TELEGRAM_BOT_TOKEN:
        sys.exit("Set PROSPECTOR_TELEGRAM_BOT_TOKEN in .env (create a second bot with @BotFather).")
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # one copy only
    try:
        lock.bind(("127.0.0.1", 47322))
    except OSError:
        sys.exit("Prospector is already running.")
    from prospector.bot import build_app
    build_app().run_polling()


if __name__ == "__main__":
    main()
