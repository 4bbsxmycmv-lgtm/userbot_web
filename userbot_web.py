import os
import re
import time
import asyncio
import logging
from aiohttp import web

from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)
logging.getLogger("telethon").setLevel(logging.INFO)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION = os.environ["SESSION"]

TARGET_CHAT_RAW = os.environ["TARGET_CHAT"]
try:
    TARGET_CHAT = int(TARGET_CHAT_RAW)
except ValueError:
    TARGET_CHAT = TARGET_CHAT_RAW

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "20"))
PORT = int(os.environ.get("PORT", "10000"))  # Render прокидывает PORT

RULES = [
    (re.compile(r"\bнайден\b", re.I), "Привет!", "/next"),
    (re.compile(r"\bкнпопки\b", re.I), "/next"),
    (re.compile(r"\bдоставка\b", re.I), "Доставка: сроки и варианты — в закрепе. Уточните город, пожалуйста."),
]

last_reply_ts = {}

client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

@client.on(events.NewMessage(chats=TARGET_CHAT))
async def handler(event):
    if event.out:
        return

    text = event.raw_text or ""
    now = time.time()
    key = (event.chat_id, event.sender_id)

    if now - last_reply_ts.get(key, 0) < COOLDOWN_SECONDS:
        return

    for pattern, answer in RULES:
        if pattern.search(text):
            last_reply_ts[key] = now
            await event.reply(answer)
            break

async def start_health_server():
    async def health(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health server listening on :{PORT}")

async def main():
    await start_health_server()
    await client.start()
    print("Userbot started")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
