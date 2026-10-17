r"""Job Applier - run with:  .venv\Scripts\python main.py        (starts the Telegram bot)
                            .venv\Scripts\python main.py --check (verifies your setup)"""
import asyncio
import logging
import sys

from jobapplier import config


def check() -> None:
    from jobapplier import profile
    from jobapplier.llm import llm

    ok = lambda b: "OK " if b else "MISSING"
    import httpx
    bot_name = None
    if config.TELEGRAM_BOT_TOKEN:
        try:
            r = httpx.get(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe", timeout=20).json()
            bot_name = r.get("ok") and "@" + r["result"]["username"]
        except httpx.HTTPError:
            pass
    print(f"[{ok(bot_name)}] TELEGRAM_BOT_TOKEN " + (f"-> {bot_name}" if bot_name else
          "(invalid: copy the FULL token from @BotFather, it looks like 1234567890:AAE...)"))
    print(f"[{ok(config.TELEGRAM_CHAT_ID)}] TELEGRAM_CHAT_ID (must be a number: send /start to the bot to get it)")
    print(f"[{ok(config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD)}] GMAIL_ADDRESS + GMAIL_APP_PASSWORD")
    print(f"[{ok(config.APPLY_EMAIL)}] APPLY_EMAIL = {config.APPLY_EMAIL}")
    print(f"[{ok(config.PROFILE_PATH.exists())}] profile.yaml")
    from jobapplier import latex
    from jobapplier.resume import ensure_base_pdf
    try:
        ensure_base_pdf()
        print(f"[OK ] resumes/base_resume.tex -> base_resume.pdf ({latex.page_count(config.BASE_RESUME_PATH)} page)")
    except Exception as e:
        print(f"[MISSING] resume: {e}")
    print(f"LLM providers in order: {llm.providers or 'NONE - add at least one API key'}")

    async def ping():
        try:
            out = await llm.complete("Reply with the single word: ready", purpose="check", max_tokens=20)
            print("LLM test:", out.strip())
        except Exception as e:
            print("LLM test FAILED:", e)
    if llm.providers:
        asyncio.run(ping())


def main() -> None:
    from logging.handlers import RotatingFileHandler
    handlers = [RotatingFileHandler(config.DATA_DIR / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")]
    if sys.stdout:  # pythonw (hidden background mode) has no console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if "--check" in sys.argv:
        check()
        return
    if not config.TELEGRAM_BOT_TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env first (see README.md).")
    # Single instance: two bots would both answer every message and fight over the browser profile
    import socket
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 47321))
    except OSError:
        sys.exit("Job Applier is already running (close the other window/terminal first).")
    from jobapplier.bot import build_app
    build_app().run_polling()


if __name__ == "__main__":
    main()
