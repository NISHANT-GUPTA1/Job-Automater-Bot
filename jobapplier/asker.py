"""Lets background automation ask the user a question on Telegram and wait for the reply."""
import asyncio
import itertools
import logging
from pathlib import Path

from telegram import Bot, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from . import config

log = logging.getLogger(__name__)
MAX_LEN = 4000


class Asker:
    def __init__(self) -> None:
        self.bot: Bot | None = None
        self.chat_id: int | None = None
        self._ids = itertools.count(1)
        # key -> (future, options, telegram message id)
        self.pending: dict[int, tuple[asyncio.Future, list[str] | None, int, str]] = {}

    def bind(self, bot: Bot, chat_id: int) -> None:
        self.bot, self.chat_id = bot, chat_id

    @staticmethod
    def browser_button() -> list | None:
        """A one-tap button to the bot's browser (phone control), when the link is available."""
        return [InlineKeyboardButton("🖥 Open the bot's browser", url=config.BROWSER_VIEW_URL)] \
            if config.BROWSER_VIEW_URL else None

    async def say(self, text: str, browser: bool = False, **kwargs) -> int | None:
        if not self.bot:
            log.info("[telegram not bound] %s", text)
            return None
        if browser and self.browser_button():
            kwargs["reply_markup"] = InlineKeyboardMarkup([self.browser_button()])
        msg = None
        for i in range(0, len(text), MAX_LEN):
            msg = await self.bot.send_message(self.chat_id, text[i:i + MAX_LEN], **kwargs)
        return msg.message_id if msg else None

    async def send_file(self, path: str | Path, caption: str = "") -> None:
        if self.bot:
            with open(path, "rb") as f:
                await self.bot.send_document(self.chat_id, f, caption=caption[:1000])

    async def send_photo(self, path: str | Path, caption: str = "", browser: bool = False) -> None:
        if self.bot:
            markup = InlineKeyboardMarkup([self.browser_button()]) if browser and self.browser_button() else None
            with open(path, "rb") as f:
                await self.bot.send_photo(self.chat_id, f, caption=caption[:1000], reply_markup=markup)

    async def ask(self, question: str, options: list[str] | None = None,
                  timeout_min: int | None = None, browser: bool = False) -> str | None:
        """Send a question and wait for the answer. Returns None on timeout.
        browser=True adds a button that opens the bot's browser on the phone."""
        if not self.bot:
            raise RuntimeError("Telegram bot not bound")
        key = next(self._ids)
        link = self.browser_button() if browser else None
        if options or link:
            rows = [[InlineKeyboardButton(o, callback_data=f"ans|{key}|{i}")] for i, o in enumerate(options or [])]
            markup = InlineKeyboardMarkup(([link] if link else []) + rows)
            text = f"❓ {question}" + ("" if options else "\n\n(Send your answer as a message)")
        else:
            markup = ForceReply(selective=True)
            text = f"❓ {question}\n\n(Reply to this message with your answer)"
        msg = await self.bot.send_message(self.chat_id, text[:MAX_LEN], reply_markup=markup)
        fut = asyncio.get_running_loop().create_future()
        self.pending[key] = (fut, options, msg.message_id, question)
        try:
            return await asyncio.wait_for(fut, (timeout_min or config.QUESTION_TIMEOUT_MIN) * 60)
        except asyncio.TimeoutError:
            await self.say(f"⏰ No answer received, moving on: {question[:200]}")
            return None
        finally:
            self.pending.pop(key, None)

    def answer_button(self, key: int, idx: int) -> str | None:
        item = self.pending.get(key)
        if not item or item[0].done() or not item[1]:
            return None
        choice = item[1][idx]
        item[0].set_result(choice)
        return choice

    def answer_text(self, text: str, reply_to_msg_id: int | None) -> bool:
        """Route a free-text message to a waiting question. Returns True if consumed."""
        open_items = [(k, v) for k, v in self.pending.items() if not v[0].done()]
        if not open_items:
            return False
        target = None
        if reply_to_msg_id:
            target = next((v for k, v in open_items if v[2] == reply_to_msg_id), None)
        if target is None:
            # Not a reply: answer the oldest open text question (or a button question as free text)
            target = min(open_items, key=lambda kv: kv[0])[1]
        target[0].set_result(text)
        return True

    def pending_question(self) -> str:
        """Text of the oldest question still waiting for an answer ('' if none)."""
        open_items = sorted((k, v) for k, v in self.pending.items() if not v[0].done())
        return open_items[0][1][3] if open_items else ""

    def is_reply_to_pending(self, reply_to_msg_id: int | None) -> bool:
        return bool(reply_to_msg_id) and any(v[2] == reply_to_msg_id and not v[0].done() for v in self.pending.values())

    def has_pending(self) -> bool:
        return any(not v[0].done() for v in self.pending.values())


asker = Asker()
