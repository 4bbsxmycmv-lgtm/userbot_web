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

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "2"))
PORT = int(os.environ.get("PORT", "10000"))

# ВАЖНО: второй элемент — список сообщений, которые нужно отправить по порядку
RULES = [
    (re.compile(r"\bнайден\b", re.I), ["Привет!", "/next"]),
    (re.compile(r"\bкнопки\b", re.I), ["/next"]),  # у вас было "кнпопки" (опечатка)
    (re.compile(r"\bдоставка\b", re.I), ["Доставка: сроки и варианты — в закрепе. Уточните город, пожалуйста."]),
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

    for pattern, messages in RULES:
        if pattern.search(text):
            logging.info("MATCHED RULE: %s | TEXT: %r", pattern.pattern, text)
            last_reply_ts[key] = now

            # 1) первое сообщение ответом (reply)
            await event.reply(messages[0])

            # 2) остальные — отдельными сообщениями
            for msg in messages[1:]:
                await asyncio.sleep(0.5)
                await event.respond(msg)

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
    logging.info("Health server listening on :%s", PORT)

async def main():
    await start_health_server()
    await client.start()
    logging.info("Userbot started")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
