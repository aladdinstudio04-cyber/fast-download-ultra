import os, re, hmac, hashlib, asyncio, logging
from urllib.parse import quote
from aiohttp import web
from pyrogram import Client, filters

logging.basicConfig(level=logging.INFO)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BIN_CHANNEL = int(os.environ["BIN_CHANNEL"])   # private channel id, e.g. -100123...
OWNER_ID = int(os.environ["OWNER_ID"])         # your Telegram user id
BASE_URL = os.environ["BASE_URL"].rstrip("/")  # e.g. https://myapp.onrender.com
SECRET = os.environ.get("SECRET", BOT_TOKEN).encode()
PORT = int(os.environ.get("PORT", 8080))
CHUNK = 1024 * 1024

bot = Client("filebot", api_id=API_ID, api_hash=API_HASH,
             bot_token=BOT_TOKEN, in_memory=True)


def sign(msg_id: int) -> str:
    return hmac.new(SECRET, str(msg_id).encode(), hashlib.sha256).hexdigest()[:16]


def get_media(m):
    for t in ("document", "video", "audio", "voice", "animation", "video_note", "photo"):
        media = getattr(m, t, None)
        if media:
            return media, t
    return None, None


def file_name(media, kind, msg_id):
    name = getattr(media, "file_name", None)
    if name:
        return name
    ext = {"video": ".mp4", "audio": ".mp3", "voice": ".ogg", "photo": ".jpg",
           "animation": ".mp4", "video_note": ".mp4"}.get(kind, "")
    return f"file_{msg_id}{ext}"


@bot.on_message(filters.private & ~filters.user(OWNER_ID))
async def deny(_, m):
    print("Denied user id:", m.from_user.id if m.from_user else None, flush=True)
    await m.reply_text("Private bot.")


@bot.on_message(filters.private & filters.user(OWNER_ID) & filters.command("start"))
async def start(_, m):
    await m.reply_text("Send or forward me any file and I'll give you a download link.")


@bot.on_message(filters.private & filters.user(OWNER_ID) &
                (filters.document | filters.video | filters.audio | filters.voice |
                 filters.animation | filters.video_note | filters.photo))
async def handle(_, m):
    copied = await m.copy(BIN_CHANNEL)
    media, kind = get_media(copied)
    name = file_name(media, kind, copied.id)
    link = f"{BASE_URL}/dl/{copied.id}/{sign(copied.id)}"
    await m.reply_text(f"`{name}`\n\n{link}", quote=True)


async def download(request: web.Request):
    try:
        msg_id = int(request.match_info["id"])
    except ValueError:
        return web.Response(status=404)
    if not hmac.compare_digest(request.match_info["sig"], sign(msg_id)):
        return web.Response(status=403, text="Bad link")

    msg = await bot.get_messages(BIN_CHANNEL, msg_id)
    media, kind = get_media(msg) if msg and not msg.empty else (None, None)
    if not media:
        return web.Response(status=404, text="Not found")

    size = media.file_size
    name = file_name(media, kind, msg_id)
    mime = getattr(media, "mime_type", None) or "application/octet-stream"

    start, end = 0, size - 1
    rng = request.headers.get("Range")
    if rng:
        mt = re.match(r"bytes=(\d*)-(\d*)", rng)
        if mt:
            s, e = mt.groups()
            if s:
                start = int(s)
                end = min(int(e), size - 1) if e else size - 1
            elif e:
                start = max(size - int(e), 0)
    if start > end or start >= size:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})

    length = end - start + 1
    headers = {
        "Content-Type": mime,
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
    }
    status = 200
    if rng:
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)

    skip = start % CHUNK
    remaining = length
    try:
        async for chunk in bot.stream_media(msg, offset=start // CHUNK):
            if skip:
                chunk = chunk[skip:]
                skip = 0
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            await resp.write(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                break
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def health(_):
    return web.Response(text="ok")


async def main():
    await bot.start()
    me = await bot.get_me()
    print("Logged in as @" + str(me.username), "| OWNER_ID =", OWNER_ID, flush=True)
    try:
        await bot.get_chat(BIN_CHANNEL)
    except Exception as e:
        print("Cannot access BIN_CHANNEL. Add the bot as admin there:", e)
    app = web.Application()
    app.add_routes([web.get("/", health),
                    web.get("/dl/{id}/{sig}", download)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print("Running on port", PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
