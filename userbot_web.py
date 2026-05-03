import os, re, time, json, asyncio, logging
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
RULES_FILE = os.environ.get("RULES_FILE", "rules.json")

# ---- Пауза (оставил, раз у вас уже было) ----
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

# ---- Правила: raw -> compiled ----
DEFAULT_RULES_RAW = [
    {"match": "w:найден", "reply": ["Привет!", "/next"]},
    {"match": "w:кнопки", "reply": ["/next"]},
    {"match": "w:доставка", "reply": ["Доставка: сроки и варианты — в закрепе. Уточните город, пожалуйста."]},
]

def compile_pattern(spec: str) -> re.Pattern:
    """
    spec formats:
      re:<regex>  - как есть
      w:<word>    - отдельное слово (\\bword\\b)
      <text>      - подстрока (без границ слова)
    """
    spec = spec.strip()
    if spec.startswith("re:"):
        rgx = spec[3:]
        return re.compile(rgx, re.I)
    if spec.startswith("w:"):
        word = spec[2:]
        return re.compile(rf"\b{re.escape(word)}\b", re.I)
    return re.compile(re.escape(spec), re.I)

def load_rules_raw():
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_RULES_RAW

def save_rules_raw(rules_raw):
    os.makedirs(os.path.dirname(RULES_FILE) or ".", exist_ok=True)
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(rules_raw, f, ensure_ascii=False, indent=2)

def build_rules(rules_raw):
    compiled = []
    for item in rules_raw:
        pat = compile_pattern(item["match"])
        replies = item["reply"]
        if isinstance(replies, str):
            replies = [replies]
        compiled.append((item["match"], pat, replies))
    return compiled

RULES_RAW = load_rules_raw()
RULES = build_rules(RULES_RAW)

last_reply_ts = {}

client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
ME_ID = None  # выставим после start()

# ---- Команды управления (только от вас и только в Избранном) ----
@client.on(events.NewMessage(pattern=r"^/ub_(add|del|list|pause|resume|status)(?:\s+(.+))?$"))
async def control(event):
    global RULES_RAW, RULES, paused_until, ME_ID

    if not event.out:
        return

    # принимаем команды только из "Избранного"
    if ME_ID is None:
        me = await client.get_me()
        ME_ID = me.id
    if event.chat_id != ME_ID:
        return

    cmd = event.pattern_match.group(1)
    arg = (event.pattern_match.group(2) or "").strip()

    if cmd == "list":
        if not RULES_RAW:
            await event.reply("Правил нет.")
            return
        lines = []
        for i, r in enumerate(RULES_RAW):
            lines.append(f"{i}: {r['match']} => {r['reply']}")
        await event.reply("RULES:\n" + "\n".join(lines))
        return

    if cmd == "del":
        if arg == "":
            await event.reply("Использование: /ub_del <index>")
            return
        idx = int(arg)
        if idx < 0 or idx >= len(RULES_RAW):
            await event.reply("Нет такого index.")
            return
        removed = RULES_RAW.pop(idx)
        save_rules_raw(RULES_RAW)
        RULES = build_rules(RULES_RAW)
        await event.reply(f"Удалено: {removed['match']}")
        return

    if cmd == "add":
        # формат: /ub_add <match> => msg1 || msg2 || msg3
        # примеры match: w:слово, re:\bслово\b, или просто текст
        if "=>" not in arg:
            await event.reply("Формат: /ub_add <match> => msg1 || msg2")
            return
        match_part, replies_part = arg.split("=>", 1)
        match_spec = match_part.strip()
        replies = [x.strip() for x in replies_part.split("||")]
        replies = [x for x in replies if x]

        if not match_spec or not replies:
            await event.reply("Пустой match или reply.")
            return

        # проверим что regex компилируется
        try:
            compile_pattern(match_spec)
        except Exception as e:
            await event.reply(f"Ошибка в match: {e!r}")
            return

        RULES_RAW.append({"match": match_spec, "reply": replies})
        save_rules_raw(RULES_RAW)
        RULES = build_rules(RULES_RAW)
        await event.reply(f"Добавлено правило: {match_spec} => {replies}")
        return

    if cmd == "status":
        await event.reply("Пауза: ВКЛ" if is_paused() else "Пауза: ВЫКЛ")
        return

    if cmd == "resume":
        paused_until = 0
        await event.reply("Ок, снял с паузы.")
        return

    if cmd == "pause":
        if arg:
            secs = parse_duration(arg)
            paused_until = int(time.time() + secs)
            await event.reply(f"Ок, пауза на {secs} сек.")
        else:
            paused_until = int(time.time() + 10 * 365 * 24 * 3600)
            await event.reply("Ок, пауза включена (бессрочно).")
        return


@client.on(events.NewMessage(chats=TARGET_CHAT))
async def handler(event):
    if event.out:
        return
    if is_paused():
        return

    text = event.raw_text or ""
    now = time.time()
    key = (event.chat_id, event.sender_id)

    if now - last_reply_ts.get(key, 0) < COOLDOWN_SECONDS:
        return

    for match_spec, pattern, messages in RULES:
        if pattern.search(text):
            logging.info("MATCHED: %s | TEXT=%r", match_spec, text)
            last_reply_ts[key] = now

            await event.reply(messages[0])
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
    global ME_ID
    await start_health_server()
    await client.start()
    me = await client.get_me()
    ME_ID = me.id
    logging.info("Userbot started. Control chat (Saved Messages) id=%s", ME_ID)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
