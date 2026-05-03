import os, re, time, asyncio, logging
from aiohttp import web

from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

logging.basicConfig(level=logging.INFO)
logging.getLogger("telethon").setLevel(logging.INFO)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION = os.environ["SESSION"]

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "10"))
PORT = int(os.environ.get("PORT", "10000"))

# --- антифлуд на отправку ---
MIN_SEND_INTERVAL = float(os.environ.get("MIN_SEND_INTERVAL", "1.2"))
send_lock = asyncio.Lock()
_last_send_ts = 0.0

client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
ME_ID = None  # id "Saved Messages"

async def safe_send(chat_id, text, reply_to=None):
    global _last_send_ts
    while True:
        try:
            async with send_lock:
                delay = MIN_SEND_INTERVAL - (time.time() - _last_send_ts)
                if delay > 0:
                    await asyncio.sleep(delay)

                msg = await client.send_message(chat_id, text, reply_to=reply_to)
                _last_send_ts = time.time()
                return msg

        except FloodWaitError as e:
            wait_s = int(getattr(e, "seconds", 0)) + 1
            logging.warning("FloodWait: Telegram просит подождать %s сек", wait_s)
            await asyncio.sleep(wait_s)

# ---------- pause ----------
paused_until = 0
def is_paused():
    global paused_until
    if paused_until == 0:
        return False
    if time.time() >= paused_until:
        paused_until = 0
        return False
    return True

def parse_duration(s: str) -> int:
    s = (s or "").strip().lower()
    if not s:
        return 0
    mult = 1
    if s.endswith("s"): mult = 1; s = s[:-1]
    elif s.endswith("m"): mult = 60; s = s[:-1]
    elif s.endswith("h"): mult = 3600; s = s[:-1]
    elif s.endswith("d"): mult = 86400; s = s[:-1]
    return int(float(s) * mult)

# ---------- rules ----------
def compile_pattern(spec: str) -> re.Pattern:
    spec = spec.strip()
    if spec.startswith("re:"):
        return re.compile(spec[3:], re.I)
    if spec.startswith("w:"):
        word = spec[2:]
        return re.compile(rf"\b{re.escape(word)}\b", re.I)
    return re.compile(re.escape(spec), re.I)

RULES = [
    {"match": "w:найден", "pattern": compile_pattern("w:найден"), "reply": ["Привет!", "/next"]},
]

# ---------- TARGET CHAT (можно менять командой) ----------
TARGET_CHAT_ID = None  # сюда будет записываться -100... (или id лички/группы)

async def resolve_chat_id(spec: str) -> int:
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("empty chat spec")
    if spec.lstrip("-").isdigit():
        return int(spec)

    # @username или username
    if spec.startswith("@"):
        spec = spec[1:]

    entity = await client.get_entity(spec)
    return utils.get_peer_id(entity)

# ---------- КУЛДАУН (ОБЩИЙ НА ЧАТ) ЧЕРЕЗ ОЧЕРЕДЬ ----------
queues = {}
workers = {}
last_sent_ts = {}

async def ensure_worker(key):
    if key in workers and not workers[key].done():
        return
    if key not in queues:
        queues[key] = asyncio.Queue()

    async def worker():
        while True:
            job = await queues[key].get()
            try:
                while is_paused():
                    await asyncio.sleep(1.0)

                now = time.time()
                next_allowed = last_sent_ts.get(key, 0) + COOLDOWN_SECONDS
                if now < next_allowed:
                    await asyncio.sleep(next_allowed - now)

                chat_id = job["chat_id"]
                reply_to = job["reply_to"]
                msgs = job["msgs"]

                await safe_send(chat_id, msgs[0], reply_to=reply_to)
                for msg in msgs[1:]:
                    await asyncio.sleep(0.5)
                    await safe_send(chat_id, msg)

                last_sent_ts[key] = time.time()

            except Exception as e:
                logging.exception("Worker error: %r", e)
            finally:
                queues[key].task_done()

    workers[key] = asyncio.create_task(worker())

# ---------- команды ----------
@client.on(events.NewMessage(pattern=r"^/ub_chat(?:\s+(.+))?$"))
async def cmd_chat(event):
    global ME_ID, TARGET_CHAT_ID

    if not event.out:
        return

    if ME_ID is None:
        me = await client.get_me()
        ME_ID = me.id

    # только из Избранного
    if event.chat_id != ME_ID:
        return

    arg = (event.pattern_match.group(1) or "").strip()
    if not arg:
        await safe_send(event.chat_id, f"Текущий TARGET_CHAT_ID: {TARGET_CHAT_ID}", reply_to=event.id)
        return

    try:
        cid = await resolve_chat_id(arg)
        TARGET_CHAT_ID = cid
        await safe_send(event.chat_id, f"Ок, целевой чат теперь: {TARGET_CHAT_ID}", reply_to=event.id)
    except Exception as e:
        await safe_send(event.chat_id, f"Не смог установить чат: {e!r}", reply_to=event.id)

@client.on(events.NewMessage(pattern=r"^/ub_chatoff$"))
async def cmd_chatoff(event):
    global ME_ID, TARGET_CHAT_ID
    if not event.out:
        return
    if ME_ID is None:
        me = await client.get_me()
        ME_ID = me.id
    if event.chat_id != ME_ID:
        return

    TARGET_CHAT_ID = None
    await safe_send(event.chat_id, "Ок, целевой чат отключен (TARGET_CHAT_ID=None).", reply_to=event.id)

@client.on(events.NewMessage(pattern=r"^/ub(add|del|list|pause|resume|status)(?:\s+(.+))?$"))
async def control(event):
    global ME_ID, paused_until, RULES

    if not event.out:
        return

    if ME_ID is None:
        me = await client.get_me()
        ME_ID = me.id

    if event.chat_id != ME_ID:
        return

    cmd = event.pattern_match.group(1)
    arg = (event.pattern_match.group(2) or "").strip()

    if cmd == "list":
        if not RULES:
            await safe_send(event.chat_id, "Правил нет.", reply_to=event.id)
            return
        lines = [f"{i}: {r['match']} => {r['reply']}" for i, r in enumerate(RULES)]
        await safe_send(event.chat_id, "RULES:\n" + "\n".join(lines), reply_to=event.id)
        return

    if cmd == "del":
        if not arg:
            await safe_send(event.chat_id, "Формат: /ub_del <index>", reply_to=event.id)
            return
        idx = int(arg)
        if idx < 0 or idx >= len(RULES):
            await safe_send(event.chat_id, "Нет такого index.", reply_to=event.id)
            return
        removed = RULES.pop(idx)
        await safe_send(event.chat_id, f"Удалено: {removed['match']}", reply_to=event.id)
        return

    if cmd == "add":
        if "=>" not in arg:
            await safe_send(event.chat_id, "Формат: /ub_add <match> => msg1 ] msg2", reply_to=event.id)
            return

        match_part, replies_part = arg.split("=>", 1)
        match_spec = match_part.strip()

        replies = [x.strip() for x in replies_part.split("]")]
        replies = [x for x in replies if x]

        if not match_spec or not replies:
            await safe_send(event.chat_id, "Пустой match или reply.", reply_to=event.id)
            return

        try:
            pat = compile_pattern(match_spec)
        except Exception as e:
            await safe_send(event.chat_id, f"Ошибка в match: {e!r}", reply_to=event.id)
            return

        RULES.append({"match": match_spec, "pattern": pat, "reply": replies})
        await safe_send(event.chat_id, f"Добавлено: {match_spec} => {replies}", reply_to=event.id)
        return

    if cmd == "status":
        await safe_send(event.chat_id, ("Пауза: ВКЛ" if is_paused() else "Пауза: ВЫКЛ"), reply_to=event.id)
        return

    if cmd == "resume":
        paused_until = 0
        await safe_send(event.chat_id, "Ок, снял с паузы.", reply_to=event.id)
        return

    if cmd == "pause":
        if arg:
            secs = parse_duration(arg)
            paused_until = int(time.time() + secs)
            await safe_send(event.chat_id, f"Ок, пауза на {secs} сек.", reply_to=event.id)
        else:
            paused_until = int(time.time() + 10 * 365 * 24 * 3600)
            await safe_send(event.chat_id, "Ок, пауза включена (бессрочно).", reply_to=event.id)
        return

# ---------- main handler (ловим ВСЕ чаты, но реагируем только на TARGET_CHAT_ID) ----------
@client.on(events.NewMessage)
async def handler(event):
    global TARGET_CHAT_ID

    if event.out:
        return
    if is_paused():
        return
    if TARGET_CHAT_ID is None:
        return
    if event.chat_id != TARGET_CHAT_ID:
        return

    text = event.raw_text or ""

    for r in RULES:
        if r["pattern"].search(text):
            logging.info("MATCHED: %s | TEXT=%r", r["match"], text)

            key = (event.chat_id,)  # общий кулдаун на чат
            await ensure_worker(key)
            await queues[key].put({
                "chat_id": event.chat_id,
                "reply_to": event.id,
                "msgs": r["reply"],
            })
            break

# ---------- health server ----------
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
    global ME_ID, TARGET_CHAT_ID

    await start_health_server()
    await client.start()

    me = await client.get_me()
    ME_ID = me.id

    # опционально: стартовое значение из env TARGET_CHAT (если задано)
    raw = os.environ.get("TARGET_CHAT", "").strip()
    if raw:
        try:
            TARGET_CHAT_ID = await resolve_chat_id(raw)
            logging.info("Initial TARGET_CHAT_ID from env: %s", TARGET_CHAT_ID)
        except Exception as e:
            logging.warning("Cannot resolve TARGET_CHAT from env: %r", e)

    logging.info("Userbot started. Control via Saved Messages (chat_id=%s)", ME_ID)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
