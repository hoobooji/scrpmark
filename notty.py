

import os, re, json, asyncio, logging, subprocess, shutil, time
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo
from PIL import Image

# ---------------- CONFIG ----------------
api_id = 123456890
api_hash = ""

session_name = "corning_session"

target_channel_id = -100xxxxxxx
output_channel_id = -100xxxxxxx

genlink_bot_username = "@YOUR_GENLINK_BOT"   # <<< SET THIS

media_folder = "media_temp"
overlay_path = "watermark.png"

INACTIVITY = 6
FFPRESET = "medium"
FFCRF = "20"

os.makedirs(media_folder, exist_ok=True)

# ---------------- REGEX ----------------
start_link_regex = re.compile(
    r"https?://(?:t\.me|telegram\.me|telegram\.com)/([^?]+)\?start=([\w-]+)",
    re.IGNORECASE
)

any_start_link_regex = re.compile(
    r"https?://(?:t\.me|telegram\.me|telegram\.com)/[^?]+\?start=[\w-]+",
    re.IGNORECASE
)

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("SRP")

# ---------------- CLIENT ----------------
client = TelegramClient(session_name, api_id, api_hash)
queue = asyncio.Queue()
genlink_bot = None

# ---------------- UTIL ----------------
def cleanup_media(path):
    if path and os.path.exists(path):
        os.remove(path)
        log.info(f"Deleted media: {path}")

def replace_only_link(text, new_link):
    updated = any_start_link_regex.sub(new_link, text, count=1)
    return updated if updated != text else None

def extract_video_info(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration",
             "-of", "json", path],
            capture_output=True, text=True
        )
        s = json.loads(r.stdout)["streams"][0]
        return int(s["width"]), int(s["height"]), int(float(s.get("duration", 0)))
    except:
        return None

# ---------------- WATERMARK ----------------
def watermark_image(inp, out):
    base = Image.open(inp).convert("RGBA")
    mw, mh = base.size

    wm = Image.open(overlay_path).convert("RGBA")
    ww = int(mw * 0.35)
    wh = int(ww * wm.height / wm.width)
    wm = wm.resize((ww, wh), Image.Resampling.LANCZOS)

    mx, my = int(mw * 0.15), int(mh * 0.15)
    base.alpha_composite(wm, (mx, my))
    base.alpha_composite(wm, (mw - ww - mx, mh - wh - my))

    base.convert("RGB").save(out, quality=95)
    return out

def watermark_video(inp, out):
    info = extract_video_info(inp)
    if not info:
        shutil.copy(inp, out)
        return out

    w, h, _ = info
    wm = Image.open(overlay_path).convert("RGBA")
    ww = int(w * 0.35)
    wh = int(ww * wm.height / wm.width)
    wm = wm.resize((ww, wh))
    wm_path = f"{media_folder}/wm_tmp.png"
    wm.save(wm_path)

    mx, my = int(w * 0.15), int(h * 0.15)
    fc = (
        f"[0:v][1:v]overlay={mx}:{my}[t];"
        f"[t][2:v]overlay={w-ww-mx}:{h-wh-my}"
    )

    subprocess.run([
        "ffmpeg", "-y", "-i", inp, "-i", wm_path, "-i", wm_path,
        "-filter_complex", fc,
        "-map", "0:a?", "-c:v", "libx264",
        "-preset", FFPRESET, "-crf", FFCRF,
        "-pix_fmt", "yuv420p", out
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cleanup_media(wm_path)
    return out

# ---------------- COLLECT SOURCE MEDIA ----------------
async def collect_all_media(bot, since_id):
    collected, seen = [], set()
    last_activity = time.time()

    while True:
        msgs = await client.get_messages(bot, limit=10)
        for m in reversed(msgs):
            if m.id <= since_id or m.id in seen:
                continue
            if m.media:
                collected.append(m)
                seen.add(m.id)
                last_activity = time.time()
                log.info(f"Collected source media {m.id}")

        if time.time() - last_activity >= INACTIVITY:
            break

        await asyncio.sleep(1)

    return collected

async def wait_for_link(bot, since_id):
    start = time.time()
    while time.time() - start < INACTIVITY:
        msgs = await client.get_messages(bot, limit=5)
        for m in msgs:
            if m.id > since_id and m.message:
                match = any_start_link_regex.search(m.message)
                if match:
                    return match.group(0)
        await asyncio.sleep(1)
    return None

# ---------------- PROCESS MEDIA ----------------
async def process_media(msg):
    downloaded = await client.download_media(msg, file=media_folder)
    out = f"{media_folder}/wm_{os.path.basename(downloaded)}"

    if downloaded.lower().endswith((".mp4", ".mkv", ".mov")):
        final = watermark_video(downloaded, out)
        w, h, d = extract_video_info(final)
        attrs = [DocumentAttributeVideo(duration=d, w=w, h=h, supports_streaming=True)]
        return downloaded, final, attrs, True
    else:
        final = watermark_image(downloaded, out)
        return downloaded, final, [], False

# ---------------- HANDLER ----------------
@client.on(events.NewMessage(chats=target_channel_id))
async def handler(event):
    await queue.put(event.message)
    log.info(f"Queued post {event.message.id}")

async def worker():
    while True:
        msg = await queue.get()
        text = msg.text or ""

        m = start_link_regex.search(text)
        if not m:
            queue.task_done()
            continue

        source_bot = await client.get_entity("@" + m.group(1))
        token = m.group(2)

        since_media_id = (await client.get_messages(source_bot, limit=1))[0].id
        await client.send_message(source_bot, f"/start {token}")

        media_msgs = await collect_all_media(source_bot, since_media_id)
        if not media_msgs:
            queue.task_done()
            continue

        sent_msgs = []
        for media in media_msgs:
            orig, final, attrs, is_vid = await process_media(media)
            sent = await client.send_file(
                output_channel_id,
                final,
                attributes=attrs,
                supports_streaming=is_vid
            )
            sent_msgs.append(sent)
            cleanup_media(orig)
            cleanup_media(final)

        # -------- GENLINK --------
        since_link_id = (await client.get_messages(genlink_bot, limit=1))[0].id

        if len(sent_msgs) == 1:
            await client.send_message(genlink_bot, "/genlink")
            await asyncio.sleep(2)
            await client.forward_messages(genlink_bot, sent_msgs[0])
        else:
            await client.send_message(genlink_bot, "/batch")
            await asyncio.sleep(2)
            await client.forward_messages(genlink_bot, sent_msgs[0])
            await asyncio.sleep(3)
            await client.forward_messages(genlink_bot, sent_msgs[-1])

        link = await wait_for_link(genlink_bot, since_link_id)
        if link:
            new_caption = replace_only_link(text, link)
            if new_caption:
                await client.edit_message(
                    target_channel_id,
                    msg.id,
                    new_caption,
                    parse_mode="html",
                    link_preview=False
                )

        queue.task_done()

# ---------------- START ----------------
async def main():
    global genlink_bot
    await client.start()
    genlink_bot = await client.get_entity(genlink_bot_username)
    log.info("SRP started")
    asyncio.create_task(worker())
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())