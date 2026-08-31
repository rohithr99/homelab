"""
Jellyfin Telegram Bot
=====================
A real telegram bot (created via @BotFather) that only responds to users you
explicitily allow. Approved users send/forward a video and it gets saved to a
folder Jellyfin scans.
"""

import os
import shutil
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
LOCAL_API_URL = os.environ.get("LOCAL_API_URL", "http://telegram-bot-api:8081")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

ALLOWED_USER_IDS = {
        int(uid.strip())
        for uid in os.environ.get("ALLOWED_USER_IDS","").split(",")
        if uid.strip()
        }

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger(__name__)

def notify_jellyfin_scan():
    """Ask Jellyfin to rescan its library so the new file shows up right away."""
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        return  # Jellyfin auto-scan is not configured, skip
    try:
        requests.post(
                f"{JELLYFIN_URL}/Library/Refresh",
                headers={"X-Emby-Token": JELLYFIN_API_KEY},
                timeout=10,
                )
    except requests.RequestException as e:
        log.warning(f"Could not trigger Jellyfin scan: {e}")

# -----------------------------------------------------------------------
# Message Handler
# This function runs everytime ANY user sends the bot a video/document.
# -----------------------------------------------------------------------

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if user.id not in ALLOWED_USER_IDS:
        await message.reply_text("You're not authorized to use this bot.")
        log.warning(f"Blocked unauthorized user: {user.id} (@{user.username})")
        return

    file_obj = message.video or message.document
    if not file_obj:
        await message.reply_text("Send me a video file and I'll add it to Jellyfin.")
        return

    await message.reply_text(f"Downloading '{file_obj.file_name or 'file'}'...")

    tg_file = await context.bot.get_file(file_obj.file_id)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dest_name = file_obj.file_name or f"{file_obj.file_id}.mp4"
    dest_path = os.path.join(DOWNLOAD_DIR, dest_name)

    # In local mode, tg_file.file_path is a real path on the shared disk.
    # we just move it into our downloads folder.
    #(Fallback to a normal HTTP download if that path isn't reachable for some reason.)
    if os.path.exists(tg_file.file_path):
        shutil.move(tg_file.file_path, dest_path)
    else:
        await tg_file.download_to_drive(dest_path)

    await message.reply_text(f"Saved as '{dest_name}'.")
    notify_jellyfin_scan()
    log.info(f"saved {dest_path} (requested by {user.id})")

def main():
    # ApplicationBuilder configures and creates the bot instance
    app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .base_url(f"{LOCAL_API_URL}/bot")           #pointing to our telegram server
            .base_file_url(f"{LOCAL_API_URL}/file/bot") #same, for the file downloads
            .local_mode(True)                           #tells the library files live on the local disk
            .build()
            )
    #Register the handler: "whenever a message contains a video OR any document, call the handle_media"
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_media))

    log.info("Bot starting...!")
    app.run_polling() # Keeps the process alive, continuously checking for new messages


if __name__ == "__main__":
    main()
