"""
Access Bot
==========
Decides who may use your other Telegram bots.

- Someone who is not approved sends /start. The bot saves a "pending" request
  and sends each admin a message with Approve / Deny buttons.
- Approving adds their ID to the shared SQLite database (users.db). The other
  bots read that same file, so approval works everywhere straight away.
- Admins (ADMIN_IDS) are always allowed and can also manage the list by hand:
    /adduser <telegram_id> [name]
    /removeuser <telegram_id>
    /users
    /pending
"""

import os
import sqlite3
import logging
from contextlib import closing

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "/shared/users.db")
ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip()
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SHARED SQLITE DATABASE
# timeout=10 makes a connection wait up to 10 seconds if another bot is
# writing at the same moment, instead of failing straight away.
# The "?" placeholders pass values safely (never paste user input into SQL).
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
    run_sql(
        "CREATE TABLE IF NOT EXISTS pending ("
        "user_id INTEGER PRIMARY KEY, name TEXT, "
        "requested_at TEXT DEFAULT CURRENT_TIMESTAMP)",
        write=True,
    )


def is_allowed(user_id):
    if user_id in ADMIN_IDS:
        return True
    return bool(run_sql("SELECT 1 FROM users WHERE user_id = ?", (user_id,)))


def add_user(user_id, name=""):
    run_sql("INSERT OR REPLACE INTO users (user_id, name) VALUES (?, ?)", (user_id, name), write=True)


def remove_user(user_id):
    run_sql("DELETE FROM users WHERE user_id = ?", (user_id,), write=True)


def list_users():
    return run_sql("SELECT user_id, name FROM users ORDER BY added_at")


def add_pending(user_id, name=""):
    run_sql("INSERT OR REPLACE INTO pending (user_id, name) VALUES (?, ?)", (user_id, name), write=True)


def get_pending(user_id):
    return run_sql("SELECT name FROM pending WHERE user_id = ?", (user_id,))


def remove_pending(user_id):
    run_sql("DELETE FROM pending WHERE user_id = ?", (user_id,), write=True)


def list_pending():
    return run_sql("SELECT user_id, name FROM pending ORDER BY requested_at")


def describe(user):
    text = f"{user.full_name} (id {user.id})"
    if user.username:
        text = f"{user.full_name} (@{user.username}, id {user.id})"
    return text


# ---------------------------------------------------------------------------
# HANDLERS
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs for /start and for any plain message."""
    user = update.effective_user
    message = update.effective_message

    if user.id in ADMIN_IDS:
        await message.reply_text(
            "Hi admin. Commands:\n"
            "/pending  waiting requests\n"
            "/users  approved users\n"
            "/adduser <id> [name]\n"
            "/removeuser <id>"
        )
        return

    if is_allowed(user.id):
        await message.reply_text("You already have access to the bots.")
        return

    if get_pending(user.id):
        await message.reply_text("Your request is already waiting for approval.")
        return

    add_pending(user.id, user.full_name or "")
    await message.reply_text("Request sent. You'll get a message here when you're approved.")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve", callback_data=f"approve|{user.id}"),
        InlineKeyboardButton("Deny", callback_data=f"deny|{user.id}"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id, f"Access request:\n{describe(user)}", reply_markup=keyboard
            )
        except Exception as e:
            # Happens if the admin never pressed /start in THIS bot.
            log.warning(f"Could not notify admin {admin_id}: {e}")


async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return

    action, uid_text = q.data.split("|", 1)
    uid = int(uid_text)

    rows = get_pending(uid)
    if not rows:
        await q.edit_message_text("That request was already handled.")
        return
    name = rows[0][0] or ""

    if action == "approve":
        add_user(uid, name)
        remove_pending(uid)
        await q.edit_message_text(f"Approved: {name} ({uid})")
        user_msg = "You've been approved. You can now use the bots."
    else:
        remove_pending(uid)
        await q.edit_message_text(f"Denied: {name} ({uid})")
        user_msg = "Your access request was declined."

    try:
        await context.bot.send_message(uid, user_msg)
    except Exception as e:
        log.warning(f"Could not message user {uid}: {e}")


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /adduser <telegram_id> [name]")
        return
    uid = int(context.args[0])
    name = " ".join(context.args[1:])
    add_user(uid, name)
    remove_pending(uid)
    await update.message.reply_text(f"Added {uid} {name}".strip())


async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /removeuser <telegram_id>")
        return
    remove_user(int(context.args[0]))
    await update.message.reply_text(f"Removed {context.args[0]}")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    rows = list_users()
    if not rows:
        await update.message.reply_text("No extra users yet. (Admins are always allowed.)")
        return
    await update.message.reply_text("\n".join(f"{uid}  {name or ''}" for uid, name in rows))


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    rows = list_pending()
    if not rows:
        await update.message.reply_text("No pending requests.")
        return
    await update.message.reply_text("\n".join(f"{uid}  {name or ''}" for uid, name in rows))


def main():
    init_db()
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: nobody can approve requests.")

    # This bot only sends short messages, so it uses Telegram's normal servers
    # and keeps working even if the local Bot API server is down.
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("adduser", adduser_cmd))
    app.add_handler(CommandHandler("removeuser", removeuser_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(handle_decision))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, start_cmd))

    log.info("Access bot starting...!")
    app.run_polling()


if __name__ == "__main__":
    main()
