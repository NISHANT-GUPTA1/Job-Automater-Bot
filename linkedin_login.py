"""Log in to LinkedIn once for the Prospector bot.

    .venv\\Scripts\\python linkedin_login.py

A browser window opens on LinkedIn's sign-in page. Sign in yourself (password, 2-step code, whatever LinkedIn
asks). When your feed shows, the window closes and the session is saved in data/linkedin_profile - the bot reuses
it for months. Your password is never stored. Run this again only if the bot tells you LinkedIn logged you out.
"""
import asyncio

from prospector import db, linkedin


async def main() -> None:
    print("Opening LinkedIn - sign in in the browser window (you have 10 minutes).")
    async with linkedin.session(visible=True) as page:
        if await linkedin.logged_in(page):
            db.set_setting("li_session", "1")
            db.set_setting("li_paused", "0")
            print("Already signed in - nothing to do.")
            return
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        for _ in range(600):
            if "/feed" in page.url or "/in/" in page.url:
                await asyncio.sleep(3)  # let LinkedIn finish setting its cookies
                db.set_setting("li_session", "1")
                db.set_setting("li_paused", "0")
                print("Signed in. Session saved - you can close this terminal.")
                return
            await asyncio.sleep(1)
        print("Timed out - run it again when you're ready.")


if __name__ == "__main__":
    asyncio.run(main())
