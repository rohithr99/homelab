"""
YouTube Downloader Telegram Bot
===============================
Send a YouTube link, pick a format (MP3 / MP4 720p / MP4 1080p), and the bot
downloads it with yt-dlp, converts it with ffmpeg, and sends the file back.
Playlist links download item by item.

Who may use it is decided by the shared users.db that the access bot manages.
"""

import os
import re
import time
import shutil
import sqlite3
import asyncio
import logging
import tempfile
from contextlib import closing

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# CONFIG (read from environment variables set in docker-compose.yml)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
LOCAL_API_URL = os.environ.get("LOCAL_API_URL", "http://telegram-bot-api:8081")
DB_PATH = os.environ.get("DB_PATH", "/shared/users.db")
ACCESS_BOT = os.environ.get("ACCESS_BOT", "")  # e.g. @my_access_bot, shown to people who are refused
WORK_DIR = os.environ.get("WORK_DIR", "/tmp/ytdl")
MAX_PLAYLIST = int(os.environ.get("MAX_PLAYLIST", "25"))
TELEGRAM_MAX_BYTES = 1990 * 1024 * 1024  # just under the 2 GB local-server limit

ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip()
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger(__name__)

# Only 2 downloads at once, so a small server isn't overwhelmed.
DOWNLOAD_SLOTS = asyncio.Semaphore(2)

URL_RE = re.compile(r"https?://\S+")


# ---------------------------------------------------------------------------
# SHARED USER LIST (read-only here; the access bot adds and removes people)
# ---------------------------------------------------------------------------
def run_sql(sql, params=(), write=False):
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as con:
        cur = con.execute(sql, params)
        if write:
            con.commit()
        return cur.fetchall()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    run_sql(
        "CREATE TABLE IF NOT EXISTS users ("
        "user_id INTEGER PRIMARY KEY, name TEXT, "
        "added_at TEXT DEFAULT CURRENT_TIMESTAMP)",
        write=True,
    )


def is_allowed(user_id):
    if user_id in ADMIN_IDS:
        return True
    return bool(run_sql("SELECT 1 FROM users WHERE user_id = ?", (user_id,)))


# ---------------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------------
async def safe_edit(message, text):
    """Edit a message, ignoring harmless errors like 'message is not modified'."""
    try:
        await message.edit_text(text)
    except Exception:
        pass


def short(err, limit=300):
    return str(err)[:limit]


def make_bar(percent, width=10):
    """Text progress bar, e.g. 40% -> ████░░░░░░"""
    percent = max(0, min(100, percent))
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


async def keep_chat_action(context, chat_id, action, stop):
    """Show 'sending video...' at the top of the chat until `stop` is set.
    Telegram hides the indicator after ~5 seconds, so it is re-sent every 4."""
    while not stop.is_set():
        try:
            await context.bot.send_chat_action(chat_id, action)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


async def deny(update: Update):
    text = "You're not authorized to use this bot."
    if ACCESS_BOT:
        text += f"\nRequest access with {ACCESS_BOT}."
    await update.effective_message.reply_text(text)


# ---------------------------------------------------------------------------
# yt-dlp WORK (blocking code, so it is run in a thread with asyncio.to_thread)
# ---------------------------------------------------------------------------
def get_entries(url):
    """Return a list of video URLs: one for a single video, many for a playlist."""
    is_playlist = "list=" in url and "v=" not in url and "youtu.be/" not in url
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": not is_playlist,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if "entries" in info:
        urls = []
        for e in info["entries"]:
            if not e:
                continue
            urls.append(e.get("url") or f"https://www.youtube.com/watch?v={e['id']}")
        return urls
    return [info.get("webpage_url") or url]


def download_one(url, fmt, workdir, on_progress=None):
    """Download one video into workdir. Returns (file_path, title).
    on_progress(stage, pct, speed, eta) is called from this worker thread."""

    def progress_hook(d):
        # yt-dlp calls this many times while downloading
        if on_progress and d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                on_progress("download", d["downloaded_bytes"] * 100 / total, d.get("speed"), d.get("eta"))

    def postprocess_hook(d):
        # called when ffmpeg starts converting/merging
        if on_progress and d["status"] == "started":
            on_progress("convert")

    opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": os.path.join(workdir, "%(title).80B.%(ext)s"),
        "restrictfilenames": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocess_hook],
    }
    if fmt == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        height = fmt.split("_")[1]  # "mp4_720" -> "720"
        opts["format"] = (
            f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        )
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    title = info.get("title") or "video"
    files = [os.path.join(workdir, f) for f in os.listdir(workdir)]
    if not files:
        raise RuntimeError("yt-dlp produced no file")
    path = max(files, key=os.path.getsize)
    return path, title


# ---------------------------------------------------------------------------
# HANDLERS
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await deny(update)
        return
    await update.message.reply_text(
        "Send me a YouTube video or playlist link, then choose MP3 or MP4."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await deny(update)
        return

    match = URL_RE.search(update.message.text or "")
    if not match:
        await update.message.reply_text("Send me a YouTube link.")
        return

    # Button data is limited to 64 bytes, so keep the long URL in memory and
    # put only a short key in the button.
    key = str(update.message.message_id)
    context.user_data[key] = match.group(0)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("MP3", callback_data=f"mp3|{key}"),
        InlineKeyboardButton("MP4 720p", callback_data=f"mp4_720|{key}"),
        InlineKeyboardButton("MP4 1080p", callback_data=f"mp4_1080|{key}"),
    ]])
    await update.message.reply_text("Choose a format:", reply_markup=keyboard)


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_allowed(q.from_user.id):
        await safe_edit(q.message, "You're not authorized to use this bot.")
        return

    fmt, key = q.data.split("|", 1)
    url = context.user_data.pop(key, None)
    if not url:
        await safe_edit(q.message, "That request expired. Send the link again.")
        return

    async with DOWNLOAD_SLOTS:
        await process(context, q.message.chat_id, q.message, url, fmt)


async def process(context, chat_id, status, url, fmt):
    await safe_edit(status, "Reading the link...")
    try:
        entries = await asyncio.to_thread(get_entries, url)
    except Exception as e:
        log.error(f"get_entries failed: {e}")
        await safe_edit(status, f"Couldn't read that link: {short(e)}")
        return

    if not entries:
        await safe_edit(status, "Nothing found at that link.")
        return

    entries = entries[:MAX_PLAYLIST]
    total = len(entries)
    sent = 0
    loop = asyncio.get_running_loop()

    for i, entry_url in enumerate(entries, 1):
        await safe_edit(status, f"Downloading {i}/{total}...")
        workdir = tempfile.mkdtemp(dir=WORK_DIR)

        # "sending video..." indicator at the top of the chat
        stop = asyncio.Event()
        action = "upload_document" if fmt == "mp3" else "upload_video"
        action_task = asyncio.create_task(keep_chat_action(context, chat_id, action, stop))

        last_edit = [0.0]  # a list so the function below can change it

        def on_progress(stage, pct=0.0, speed=None, eta=None):
            # Runs in the download thread, so it hands the edit to the main loop.
            now = time.monotonic()
            if stage == "download" and now - last_edit[0] < 3:
                return  # edit at most once every 3 seconds (Telegram rate limits)
            last_edit[0] = now
            if stage == "convert":
                text = f"Converting {i}/{total}..."
            else:
                text = f"Downloading {i}/{total}\n{make_bar(pct)} {pct:.0f}%"
                if speed:
                    text += f"  {speed / 1e6:.1f} MB/s"
                if eta:
                    text += f"  ETA {int(eta)}s"
            asyncio.run_coroutine_threadsafe(safe_edit(status, text), loop)

        try:
            path, title = await asyncio.to_thread(download_one, entry_url, fmt, workdir, on_progress)
            if os.path.getsize(path) > TELEGRAM_MAX_BYTES:
                raise RuntimeError("File is over Telegram's 2 GB limit")

            await safe_edit(status, f"Uploading {i}/{total}: {title}")
            with open(path, "rb") as f:
                if fmt == "mp3":
                    await context.bot.send_audio(
                        chat_id, audio=f, title=title, filename=os.path.basename(path)
                    )
                else:
                    await context.bot.send_video(
                        chat_id, video=f, supports_streaming=True, filename=os.path.basename(path)
                    )
            sent += 1
        except Exception as e:
            log.error(f"item {i} failed: {e}")
            await context.bot.send_message(chat_id, f"Item {i} failed: {short(e)}")
        finally:
            stop.set()
            await action_task
            shutil.rmtree(workdir, ignore_errors=True)  # always clean up temp files

    await safe_edit(status, f"Done: {sent}/{total} sent.")


def main():
    init_db()
    os.makedirs(WORK_DIR, exist_ok=True)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .base_url(f"{LOCAL_API_URL}/bot")
        .base_file_url(f"{LOCAL_API_URL}/file/bot")
        .local_mode(True)
        .read_timeout(3600)
        .write_timeout(3600)
        .connect_timeout(60)
        .pool_timeout(3600)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(handle_choice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    log.info("YouTube bot starting...!")
    app.run_polling()


if __name__ == "__main__":
    main()
