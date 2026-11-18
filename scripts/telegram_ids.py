"""Read only user/chat IDs from YOUR bot's pending updates; run before the worker."""

import asyncio
import os
import sys
from getpass import getpass
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot


async def main():
    load_dotenv(Path.home() / ".teleminder" / ".env")
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN") or getpass("BotFather token (hidden): ")
    async with Bot(token) as bot:
        updates = await bot.get_updates(timeout=0, allowed_updates=["message"])
    pairs = {
        (u.effective_user.id, u.effective_chat.id)
        for u in updates
        if u.effective_user and u.effective_chat
    }
    if not pairs:
        print(
            "No messages found. Send /start to your bot, then run this again with the worker stopped."
        )
    for user_id, chat_id in sorted(pairs):
        print(f"user_id={user_id} chat_id={chat_id}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(
            f"ID lookup failed ({type(exc).__name__}). Check token and stop any polling worker.",
            file=sys.stderr,
        )
        sys.exit(1)
