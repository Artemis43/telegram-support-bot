"""
Telegram Support Bot — main.py
===============================
A two-way support relay between users (private DM) and a Telegram Forum Group.
Each user gets a dedicated forum topic. Admins reply from the group.

Deployment: Gunicorn (WSGI). PTB runs on a dedicated asyncio thread.
Entry point for Gunicorn: `gunicorn main:app`

Environment variables (see .env.example):
  TELEGRAM_BOT_TOKEN  — Bot API token from @BotFather
  TELEGRAM_GROUP_ID   — Forum-enabled group chat ID
  TELEGRAM_ADMINS     — Comma-separated admin user IDs
  PORT                — Flask port (default: 8443)
  WEBSITE_URL         — Public HTTPS URL for webhook (not needed when USE_POLLING=true)
  USE_POLLING         — Set to "true" to use long-polling instead of a webhook
  DB_PATH             — Path to SQLite DB file (default: bot_data.db)
  RATE_LIMIT_MAX      — Max messages per window per user (default: 5)
  RATE_LIMIT_WINDOW   — Rate limit window in seconds (default: 10)
"""

import os
import shutil
import logging
import sqlite3
import asyncio
import threading
import time
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, request as flask_request
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler, ApplicationHandlerStop
)
from telegram.error import NetworkError, BadRequest, Forbidden

# ──────────────────────────────────────────────────────────────────────────────
# 0. Load environment
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

TOKEN          = os.getenv("TELEGRAM_BOT_TOKEN", "")
GROUP_ID       = int(os.getenv("TELEGRAM_GROUP_ID", "0"))
ADMIN_USER_IDS = [
    int(x.strip())
    for x in os.getenv("TELEGRAM_ADMINS", "").split(",")
    if x.strip()
]
PORT           = int(os.getenv("PORT", "8443"))
WEBSITE_URL    = os.getenv("WEBSITE_URL", "")
USE_POLLING    = os.getenv("USE_POLLING", "").lower() in ("1", "true", "yes")
DB_PATH        = os.getenv("DB_PATH", "bot_data.db")
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "5"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "10"))  # seconds

# ──────────────────────────────────────────────────────────────────────────────
# 1. Validation
# ──────────────────────────────────────────────────────────────────────────────
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set.")
if not GROUP_ID:
    raise ValueError("TELEGRAM_GROUP_ID is not set.")
if not WEBSITE_URL and not USE_POLLING:
    raise ValueError("WEBSITE_URL is not set. Set it, or set USE_POLLING=true for polling mode.")

# ──────────────────────────────────────────────────────────────────────────────
# 2. Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if not ADMIN_USER_IDS:
    logger.warning("TELEGRAM_ADMINS is empty — admin commands will not work.")

# ──────────────────────────────────────────────────────────────────────────────
# 3. Flask app & PTB globals
# ──────────────────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

ptb_application: Application = Application.builder().token(TOKEN).build()
PTB_EVENT_LOOP: asyncio.AbstractEventLoop | None = None
PTB_THREAD: threading.Thread | None = None
PTB_INITIALIZED_EVENT = threading.Event()

# ──────────────────────────────────────────────────────────────────────────────
# 4. Rate limiter (in-memory, per user)
# ──────────────────────────────────────────────────────────────────────────────
_rate_data: dict[int, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()


def is_rate_limited(chat_id: int) -> bool:
    """Return True if the user has exceeded RATE_LIMIT_MAX in RATE_LIMIT_WINDOW."""
    now = time.monotonic()
    with _rate_lock:
        timestamps = _rate_data[chat_id]
        # Remove old entries outside the window
        _rate_data[chat_id] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
        if len(_rate_data[chat_id]) >= RATE_LIMIT_MAX:
            return True
        _rate_data[chat_id].append(now)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 5. Database
# ──────────────────────────────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db() -> None:
    # Ensure the DB directory exists (e.g. a mounted /data volume on the VPS).
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = _connect()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id         INTEGER PRIMARY KEY,
            username        TEXT,
            thread_id       INTEGER UNIQUE,
            is_banned       INTEGER NOT NULL DEFAULT 0,
            is_closed       INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            last_message_at TEXT
        );
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialised at '%s'.", DB_PATH)


# ── Getters / setters ─────────────────────────────────────────────────────────

def save_user(chat_id: int, username: str, thread_id: int) -> None:
    try:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO users (chat_id, username, thread_id) VALUES (?, ?, ?)",
            (chat_id, username, thread_id),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error("save_user: %s", e)


def get_thread(chat_id: int) -> int | None:
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT thread_id FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error as e:
        logger.error("get_thread: %s", e)
        return None


def get_chat_id(thread_id: int) -> int | None:
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT chat_id FROM users WHERE thread_id=?", (thread_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error as e:
        logger.error("get_chat_id: %s", e)
        return None


def get_user_row(chat_id: int) -> dict | None:
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except sqlite3.Error as e:
        logger.error("get_user_row: %s", e)
        return None


def set_banned(chat_id: int, banned: bool) -> None:
    try:
        conn = _connect()
        conn.execute(
            "UPDATE users SET is_banned=? WHERE chat_id=?",
            (1 if banned else 0, chat_id),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error("set_banned: %s", e)


def set_closed(chat_id: int, closed: bool) -> None:
    try:
        conn = _connect()
        conn.execute(
            "UPDATE users SET is_closed=? WHERE chat_id=?",
            (1 if closed else 0, chat_id),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error("set_closed: %s", e)


def set_closed_by_thread(thread_id: int, closed: bool) -> None:
    try:
        conn = _connect()
        conn.execute(
            "UPDATE users SET is_closed=? WHERE thread_id=?",
            (1 if closed else 0, thread_id),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error("set_closed_by_thread: %s", e)


def touch_last_message(chat_id: int) -> None:
    try:
        conn = _connect()
        conn.execute(
            "UPDATE users SET last_message_at=datetime('now') WHERE chat_id=?",
            (chat_id,),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error("touch_last_message: %s", e)


def get_all_users() -> list[dict]:
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM users").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logger.error("get_all_users: %s", e)
        return []


def get_stats() -> dict:
    try:
        conn = _connect()
        total     = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active    = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_closed=0 AND is_banned=0"
        ).fetchone()[0]
        banned    = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_banned=1"
        ).fetchone()[0]
        today_msg = conn.execute(
            "SELECT COUNT(*) FROM users WHERE date(last_message_at)=date('now')"
        ).fetchone()[0]
        conn.close()
        return {
            "total": total,
            "active": active,
            "banned": banned,
            "today_messages": today_msg,
        }
    except sqlite3.Error as e:
        logger.error("get_stats: %s", e)
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# 6. Media forwarding helpers
# ──────────────────────────────────────────────────────────────────────────────
async def forward_to_group(
    context: ContextTypes.DEFAULT_TYPE, message, thread_id: int
) -> bool:
    """Forward any supported message type from a user to a group topic thread."""
    bot = context.bot
    cap = message.caption
    try:
        if message.text:
            await bot.send_message(GROUP_ID, message.text, message_thread_id=thread_id)
        elif message.photo:
            await bot.send_photo(GROUP_ID, message.photo[-1].file_id, caption=cap, message_thread_id=thread_id)
        elif message.video:
            await bot.send_video(GROUP_ID, message.video.file_id, caption=cap, message_thread_id=thread_id)
        elif message.document:
            await bot.send_document(GROUP_ID, message.document.file_id, caption=cap, message_thread_id=thread_id)
        elif message.voice:
            await bot.send_voice(GROUP_ID, message.voice.file_id, caption=cap, message_thread_id=thread_id)
        elif message.audio:
            await bot.send_audio(GROUP_ID, message.audio.file_id, caption=cap, message_thread_id=thread_id)
        elif message.sticker:
            await bot.send_sticker(GROUP_ID, message.sticker.file_id, message_thread_id=thread_id)
        elif message.animation:
            await bot.send_animation(GROUP_ID, message.animation.file_id, caption=cap, message_thread_id=thread_id)
        elif message.video_note:
            await bot.send_video_note(GROUP_ID, message.video_note.file_id, message_thread_id=thread_id)
        elif message.location:
            await bot.send_location(
                GROUP_ID,
                latitude=message.location.latitude,
                longitude=message.location.longitude,
                message_thread_id=thread_id,
            )
        elif message.contact:
            await bot.send_contact(
                GROUP_ID,
                phone_number=message.contact.phone_number,
                first_name=message.contact.first_name,
                last_name=message.contact.last_name,
                message_thread_id=thread_id,
            )
        else:
            return False  # Unsupported type
        return True
    except (BadRequest, Forbidden) as e:
        logger.error("forward_to_group thread=%s: %s", thread_id, e)
        raise


async def forward_to_user(
    context: ContextTypes.DEFAULT_TYPE, message, user_chat_id: int
) -> bool:
    """Forward any supported message type from the group topic to a user."""
    bot = context.bot
    cap = message.caption
    try:
        if message.text:
            await bot.send_message(user_chat_id, message.text)
        elif message.photo:
            await bot.send_photo(user_chat_id, message.photo[-1].file_id, caption=cap)
        elif message.video:
            await bot.send_video(user_chat_id, message.video.file_id, caption=cap)
        elif message.document:
            await bot.send_document(user_chat_id, message.document.file_id, caption=cap)
        elif message.voice:
            await bot.send_voice(user_chat_id, message.voice.file_id, caption=cap)
        elif message.audio:
            await bot.send_audio(user_chat_id, message.audio.file_id, caption=cap)
        elif message.sticker:
            await bot.send_sticker(user_chat_id, message.sticker.file_id)
        elif message.animation:
            await bot.send_animation(user_chat_id, message.animation.file_id, caption=cap)
        elif message.video_note:
            await bot.send_video_note(user_chat_id, message.video_note.file_id)
        elif message.location:
            await bot.send_location(
                user_chat_id,
                latitude=message.location.latitude,
                longitude=message.location.longitude,
            )
        elif message.contact:
            await bot.send_contact(
                user_chat_id,
                phone_number=message.contact.phone_number,
                first_name=message.contact.first_name,
                last_name=message.contact.last_name,
            )
        else:
            return False
        return True
    except Forbidden:
        logger.warning("forward_to_user: bot blocked by user %s", user_chat_id)
        raise
    except BadRequest as e:
        logger.error("forward_to_user user=%s: %s", user_chat_id, e)
        raise


# ──────────────────────────────────────────────────────────────────────────────
# 7. User-facing command handlers
# ──────────────────────────────────────────────────────────────────────────────
WELCOME_TEXT = (
    "⚡ *Telegram Support Desk* ⚡\n\n"
    "👋 *Hello {name}!*\n"
    "Welcome to our support desk. We are here to help you.\n\n"
    "💬 *How it works:*\n"
    "1. Type your message and hit send.\n"
    "2. Our support team will receive it and reply directly.\n"
    "3. We aim to respond within a few hours.\n\n"
    "📌 *Supported Media:*\n"
    "• Text & Links\n"
    "• Photos & Videos\n"
    "• Voice messages & Audio\n"
    "• Stickers & GIFs\n"
    "• Locations & Contacts\n\n"
    "⚙️ *Available Commands:*\n"
    "• /start — Restart support session\n"
    "• /close — Close your active session\n"
    "• /help  — View help instructions"
)

HELP_TEXT = (
    "ℹ️ *Support Bot Guide*\n\n"
    "This bot acts as a direct line to our support team. "
    "Every message you send here is relayed directly to our staff.\n\n"
    "⌨️ *Commands:*\n"
    "• /start — Start or resume your support session\n"
    "• /close — Close your active session\n"
    "• /help  — Display this guide\n\n"
    "💡 _Simply type a message and press send to get in touch!_"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.message
    user = msg.from_user
    chat_id  = msg.chat_id
    username = user.username or user.first_name

    row = get_user_row(chat_id)

    # --- Banned ---
    if row and row["is_banned"]:
        await msg.reply_text(
            "⛔ Your account has been restricted from using this support bot. "
            "Please contact us through another channel if you believe this is a mistake."
        )
        return

    # Check if thread is valid if user has a thread_id
    thread_exists = False
    if row and row["thread_id"]:
        thread_id = row["thread_id"]
        try:
            # Send a typing chat action as a lightweight check to verify if the thread still exists.
            await context.bot.send_chat_action(
                chat_id=GROUP_ID,
                action="typing",
                message_thread_id=thread_id
            )
            thread_exists = True
        except BadRequest as e:
            if "thread not found" in str(e).lower():
                thread_exists = False
                logger.info("Thread %s not found for user %s (probably deleted by admin).", thread_id, chat_id)
            else:
                thread_exists = True
        except Exception:
            # Assume thread exists on other errors (like connection problems) to avoid recreation loops
            thread_exists = True

    if row and row["thread_id"] and thread_exists:
        # --- Existing active session ---
        if not row["is_closed"]:
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("ℹ️ Help Guide", callback_data="user_help"),
                    InlineKeyboardButton("🔒 Close Session", callback_data=f"user_close:{chat_id}:{thread_id}")
                ]
            ])
            await msg.reply_text(
                f"👋 *Hello {username}!*\n\n"
                "You already have an active support session. "
                "Just send your message here and we'll forward it to the team.",
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return
        # --- Closed session: re-open ---
        else:
            set_closed(chat_id, False)
            thread_id = row["thread_id"]
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("ℹ️ Help Guide", callback_data="user_help"),
                    InlineKeyboardButton("🔒 Close Session", callback_data=f"user_close:{chat_id}:{thread_id}")
                ]
            ])
            await msg.reply_text(
                WELCOME_TEXT.format(name=username),
                parse_mode="Markdown",
                reply_markup=kb,
            )
            
            admin_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Resolve & Close", callback_data=f"admin_close:{chat_id}:{thread_id}"),
                    InlineKeyboardButton("🚫 Ban User", callback_data=f"admin_ban:{chat_id}:{thread_id}")
                ]
            ])
            await context.bot.send_message(
                GROUP_ID,
                f"🔄 *Support Ticket Re-opened*\n"
                f"────────────────────────\n"
                f"👤 *User:* {username}\n"
                f"🆔 *Chat ID:* `{chat_id}`\n"
                f"🪪 *User ID:* `{user.id}`\n"
                f"📅 *Opened At:* `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`\n"
                f"────────────────────────\n"
                f"Reply here to send messages directly to the user.",
                message_thread_id=thread_id,
                parse_mode="Markdown",
                reply_markup=admin_kb,
            )
            return

    # --- New user or recreated session ---
    topic_name = f"🆕 {username} ({user.id})"
    try:
        topic = await context.bot.create_forum_topic(chat_id=GROUP_ID, name=topic_name)
        thread_id = topic.message_thread_id

        if not thread_id:
            logger.error("create_forum_topic returned no thread_id for %s", username)
            await msg.reply_text("❌ Couldn't create a support channel. Please try again later.")
            return

        save_user(chat_id, username, thread_id)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("ℹ️ Help Guide", callback_data="user_help"),
                InlineKeyboardButton("🔒 Close Session", callback_data=f"user_close:{chat_id}:{thread_id}")
            ]
        ])
        await msg.reply_text(
            WELCOME_TEXT.format(name=username),
            parse_mode="Markdown",
            reply_markup=kb,
        )

        # Notify group with user card
        title = "🔄 *Support Ticket Re-created*" if row else "🎫 *New Support Ticket Created*"
        notice = "\n\n⚠️ *Notice:* The previous thread was deleted by an admin." if row else ""
        admin_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Resolve & Close", callback_data=f"admin_close:{chat_id}:{thread_id}"),
                InlineKeyboardButton("🚫 Ban User", callback_data=f"admin_ban:{chat_id}:{thread_id}")
            ]
        ])
        await context.bot.send_message(
            GROUP_ID,
            f"{title}\n"
            f"────────────────────────\n"
            f"👤 *User:* {username}\n"
            f"🆔 *Chat ID:* `{chat_id}`\n"
            f"🪪 *User ID:* `{user.id}`\n"
            f"📅 *Opened At:* `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`\n"
            f"────────────────────────{notice}\n"
            f"Reply here to send messages directly to the user.",
            message_thread_id=thread_id,
            parse_mode="Markdown",
            reply_markup=admin_kb,
        )

    except BadRequest as e:
        logger.error("cmd_start BadRequest for %s (%s): %s", username, chat_id, e)
        await msg.reply_text(
            "❌ I encountered an issue setting up your support channel. "
            "Please try again or contact an administrator directly."
        )
    except Exception as e:
        logger.error("cmd_start unexpected error for %s (%s): %s", username, chat_id, e, exc_info=True)
        await msg.reply_text("❌ An unexpected error occurred. Please try again in a moment.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_user_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User command: /close — close their own support session from PM."""
    msg = update.message
    chat_id = msg.chat_id

    row = get_user_row(chat_id)
    if not row or not row["thread_id"] or row["is_closed"]:
        await msg.reply_text("⚠️ You do not have an active support session.")
        return

    thread_id = row["thread_id"]
    set_closed(chat_id, True)

    await msg.reply_text(
        "✅ *Your support session has been closed.*\n\n"
        "If you need help in the future, just send a new message or run /start.",
        parse_mode="Markdown"
    )

    # Notify admin group
    try:
        await context.bot.send_message(
            GROUP_ID,
            "🔒 *Ticket closed by the user.*",
            message_thread_id=thread_id,
            parse_mode="Markdown"
        )
        await context.bot.close_forum_topic(chat_id=GROUP_ID, message_thread_id=thread_id)
    except Exception as e:
        logger.warning("cmd_user_close: could not notify group or close topic: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# 8. User message handler
# ──────────────────────────────────────────────────────────────────────────────
_SUPPORTED_USER_FILTERS = (
    filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL |
    filters.VOICE | filters.AUDIO | filters.Sticker.ALL |
    filters.ANIMATION | filters.VIDEO_NOTE | filters.LOCATION | filters.CONTACT
)


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    msg      = update.message
    chat_id  = msg.chat_id
    user     = msg.from_user
    username = user.username or user.first_name

    row = get_user_row(chat_id)

    # No session
    if not row or not row["thread_id"]:
        await msg.reply_text(
            "⚠️ You don't have an active support session.\n"
            "Use /start to begin one."
        )
        return

    # Banned
    if row["is_banned"]:
        await msg.reply_text("⛔ Your account is restricted. You cannot send messages.")
        return

    # Closed session — prompt to restart
    if row["is_closed"]:
        await msg.reply_text(
            "ℹ️ Your previous support session was closed.\n"
            "Use /start to open a new one."
        )
        return

    # Rate limit
    if is_rate_limited(chat_id):
        await msg.reply_text(
            f"⏳ You're sending messages too fast. Please wait a moment before sending again."
        )
        return

    thread_id = row["thread_id"]
    try:
        forwarded = await forward_to_group(context, msg, thread_id)
        if forwarded:
            touch_last_message(chat_id)
            # Delivery receipt: react with 👍
            try:
                await context.bot.set_message_reaction(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    reaction=[ReactionTypeEmoji("👍")],
                )
            except Exception:
                pass  # Reactions may not be supported in all clients — silently ignore
        else:
            await msg.reply_text("⚠️ This message type isn't supported yet.")
    except BadRequest as e:
        if "thread not found" in str(e).lower():
            logger.info("Thread %s was deleted by admin. Re-creating support thread for user %s.", thread_id, chat_id)
            topic_name = f"🆕 {username} ({user.id})"
            try:
                topic = await context.bot.create_forum_topic(chat_id=GROUP_ID, name=topic_name)
                new_thread_id = topic.message_thread_id
                if not new_thread_id:
                    raise Exception("Failed to obtain new thread ID during re-creation")
                
                save_user(chat_id, username, new_thread_id)
                
                admin_kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Resolve & Close", callback_data=f"admin_close:{chat_id}:{new_thread_id}"),
                        InlineKeyboardButton("🚫 Ban User", callback_data=f"admin_ban:{chat_id}:{new_thread_id}")
                    ]
                ])
                # Notify group with user card (recreated)
                await context.bot.send_message(
                    GROUP_ID,
                    f"🔄 *Support Ticket Re-created*\n"
                    f"────────────────────────\n"
                    f"👤 *User:* {username}\n"
                    f"🆔 *Chat ID:* `{chat_id}`\n"
                    f"🪪 *User ID:* `{user.id}`\n"
                    f"📅 *Opened At:* `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`\n"
                    f"────────────────────────\n"
                    f"⚠️ *Notice:* The previous thread was deleted by an admin.\n\n"
                    f"Reply here to send messages directly to the user.",
                    message_thread_id=new_thread_id,
                    parse_mode="Markdown",
                    reply_markup=admin_kb,
                )
                
                # Try forwarding the message to the new thread
                forwarded = await forward_to_group(context, msg, new_thread_id)
                if forwarded:
                    touch_last_message(chat_id)
                    try:
                        await context.bot.set_message_reaction(
                            chat_id=chat_id,
                            message_id=msg.message_id,
                            reaction=[ReactionTypeEmoji("👍")],
                        )
                    except Exception:
                        pass
                else:
                    await msg.reply_text("⚠️ This message type isn't supported yet.")
            except Exception as re_err:
                logger.error("Failed to re-create support thread for user %s: %s", chat_id, re_err, exc_info=True)
                await msg.reply_text("❌ Failed to send your message. Please try again.")
        else:
            logger.error("handle_user_message BadRequest user=%s thread=%s: %s", chat_id, thread_id, e)
            await msg.reply_text("❌ Failed to send your message. Please try again.")
    except Exception as e:
        logger.error("handle_user_message unexpected user=%s: %s", chat_id, e, exc_info=True)
        await msg.reply_text("❌ An unexpected error occurred. Please try again.")


# ──────────────────────────────────────────────────────────────────────────────
# 9. Admin message handler (group topic → user)
# ──────────────────────────────────────────────────────────────────────────────
async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.is_topic_message or not update.message.message_thread_id:
        return

    msg       = update.message
    thread_id = msg.message_thread_id
    user_chat_id = get_chat_id(thread_id)

    if not user_chat_id:
        return  # Topic not managed by this bot

    try:
        forwarded = await forward_to_user(context, msg, user_chat_id)
        if not forwarded:
            await msg.reply_text("⚠️ This message type cannot be forwarded to the user.")
    except Forbidden:
        await msg.reply_text(
            "⛔ *Cannot deliver message* — the user has blocked the bot.",
            parse_mode="Markdown",
        )
    except BadRequest as e:
        logger.error("handle_admin_message BadRequest thread=%s user=%s: %s", thread_id, user_chat_id, e)
        await msg.reply_text(f"❌ Failed to send to user: {e.message}")
    except Exception as e:
        logger.error("handle_admin_message unexpected thread=%s user=%s: %s", thread_id, user_chat_id, e, exc_info=True)
        await msg.reply_text("❌ An unexpected error occurred.")


# ──────────────────────────────────────────────────────────────────────────────
# 10. Admin commands (issued inside a group topic)
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: /close — resolve and close the current support ticket."""
    msg       = update.message
    thread_id = msg.message_thread_id if msg.is_topic_message else None

    if not thread_id:
        await msg.reply_text("⚠️ This command must be used inside a support topic.")
        return

    user_chat_id = get_chat_id(thread_id)
    if not user_chat_id:
        await msg.reply_text("⚠️ No user linked to this topic.")
        return

    set_closed_by_thread(thread_id, True)

    # Notify user
    try:
        await context.bot.send_message(
            user_chat_id,
            "✅ *Your support ticket has been resolved.*\n\n"
            "Thank you for reaching out! If you need further assistance, "
            "feel free to use /start to open a new session.",
            parse_mode="Markdown",
        )
    except Forbidden:
        logger.warning("cmd_close: bot blocked by user %s", user_chat_id)

    # Close the forum topic
    try:
        await context.bot.close_forum_topic(chat_id=GROUP_ID, message_thread_id=thread_id)
    except BadRequest as e:
        logger.warning("cmd_close: could not close forum topic %s: %s", thread_id, e)

    await msg.reply_text(
        "✅ *Ticket closed.*\n"
        "The user has been notified. The topic is now archived.",
        parse_mode="Markdown",
    )


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: /ban — ban the user linked to this topic."""
    msg       = update.message
    thread_id = msg.message_thread_id if msg.is_topic_message else None

    if not thread_id:
        await msg.reply_text("⚠️ This command must be used inside a support topic.")
        return

    user_chat_id = get_chat_id(thread_id)
    if not user_chat_id:
        await msg.reply_text("⚠️ No user linked to this topic.")
        return

    set_banned(user_chat_id, True)
    set_closed_by_thread(thread_id, True)

    try:
        await context.bot.send_message(
            user_chat_id,
            "⛔ *You have been restricted* from using this support bot.\n"
            "If you believe this is a mistake, please contact us through another channel.",
            parse_mode="Markdown",
        )
    except Forbidden:
        pass

    try:
        await context.bot.close_forum_topic(chat_id=GROUP_ID, message_thread_id=thread_id)
    except BadRequest:
        pass

    await msg.reply_text(
        f"🚫 *User {user_chat_id} has been banned* and their session closed.",
        parse_mode="Markdown",
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: /unban — unban the user linked to this topic."""
    msg       = update.message
    thread_id = msg.message_thread_id if msg.is_topic_message else None

    if not thread_id:
        await msg.reply_text("⚠️ This command must be used inside a support topic.")
        return

    user_chat_id = get_chat_id(thread_id)
    if not user_chat_id:
        await msg.reply_text("⚠️ No user linked to this topic.")
        return

    set_banned(user_chat_id, False)

    try:
        await context.bot.send_message(
            user_chat_id,
            "✅ *Your restriction has been lifted.*\n"
            "You can now use /start to open a new support session.",
            parse_mode="Markdown",
        )
    except Forbidden:
        pass

    await msg.reply_text(
        f"✅ *User {user_chat_id} has been unbanned.*",
        parse_mode="Markdown",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: /stats — show bot statistics (DM only)."""
    msg = update.message
    if msg.chat.type != "private":
        await msg.reply_text("📊 Stats command only works in private chat with the bot.")
        return

    if msg.from_user.id not in ADMIN_USER_IDS:
        await msg.reply_text("⛔ You are not authorised to use this command.")
        return

    s = get_stats()
    text = (
        "📊 *Support Bot Statistics*\n\n"
        f"👥 Total sessions: *{s.get('total', 0)}*\n"
        f"✅ Active sessions: *{s.get('active', 0)}*\n"
        f"🚫 Banned users:   *{s.get('banned', 0)}*\n"
        f"💬 Active today:   *{s.get('today_messages', 0)}*"
    )
    await msg.reply_text(text, parse_mode="Markdown")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: /broadcast <message> — send a message to all non-banned users."""
    msg = update.message
    if msg.chat.type != "private":
        await msg.reply_text("📢 Broadcast command only works in private chat with the bot.")
        return

    if msg.from_user.id not in ADMIN_USER_IDS:
        await msg.reply_text("⛔ You are not authorised to use this command.")
        return

    text = " ".join(context.args) if context.args else ""
    if not text:
        await msg.reply_text("Usage: /broadcast <your message>")
        return

    users     = get_all_users()
    targets   = [u for u in users if not u["is_banned"]]
    sent      = 0
    failed    = 0

    status_msg = await msg.reply_text(f"📢 Broadcasting to {len(targets)} users…")

    for u in targets:
        try:
            await context.bot.send_message(
                u["chat_id"],
                f"📢 *Message from Support Team:*\n\n{text}",
                parse_mode="Markdown",
            )
            sent += 1
            await asyncio.sleep(0.05)  # Respect Telegram rate limits
        except Forbidden:
            set_banned(u["chat_id"], True)
            failed += 1
        except Exception as e:
            logger.warning("broadcast failed for %s: %s", u["chat_id"], e)
            failed += 1

    await status_msg.edit_text(
        f"📢 *Broadcast complete*\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}",
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 10b. Admin DB backup / restore (DM only)
# ──────────────────────────────────────────────────────────────────────────────
# Admin user IDs that ran /restore and are awaiting a .db upload.
_awaiting_restore: set[int] = set()

_SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_valid_sqlite(path: str) -> tuple[bool, str]:
    """Return (ok, detail): verify the SQLite header then run PRAGMA integrity_check."""
    try:
        with open(path, "rb") as fh:
            if fh.read(16) != _SQLITE_MAGIC:
                return False, "not a SQLite database (bad header)"
    except OSError as e:
        return False, f"cannot read file: {e}"
    try:
        conn = sqlite3.connect(path)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return False, f"integrity check error: {e}"
    if not result or result[0] != "ok":
        return False, f"integrity check failed: {result[0] if result else 'no result'}"
    return True, "ok"


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: /backup — DM the admin a consistent snapshot of the database."""
    msg = update.message
    if msg.chat.type != "private":
        await msg.reply_text("📦 Backup only works in private chat with the bot.")
        return
    if msg.from_user.id not in ADMIN_USER_IDS:
        await msg.reply_text("⛔ You are not authorised to use this command.")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_path = f"{DB_PATH}.snap_{ts}"
    try:
        # Consistent online snapshot via the SQLite backup API (safe while the bot runs).
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp_path)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        with open(tmp_path, "rb") as fh:
            await context.bot.send_document(
                msg.chat_id,
                document=InputFile(fh, filename=f"bot_data_{ts}.db"),
                caption=f"📦 Database backup · {ts}",
            )
        logger.info("DB backup sent to admin %s", msg.from_user.id)
    except Exception as e:
        logger.error("cmd_backup failed: %s", e, exc_info=True)
        await msg.reply_text(f"❌ Backup failed: {e}")
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: /restore — arm the bot to replace the DB with the next uploaded .db file."""
    msg = update.message
    if msg.chat.type != "private":
        await msg.reply_text("📥 Restore only works in private chat with the bot.")
        return
    if msg.from_user.id not in ADMIN_USER_IDS:
        await msg.reply_text("⛔ You are not authorised to use this command.")
        return

    _awaiting_restore.add(msg.from_user.id)
    await msg.reply_text(
        "📥 *Restore armed.*\n\n"
        "Now upload the `.db` file that should replace the current database.\n"
        "The current DB is backed up first. Send /cancel to abort.",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: /cancel — disarm a pending /restore."""
    msg = update.message
    if msg.chat.type != "private" or msg.from_user.id not in ADMIN_USER_IDS:
        return
    if msg.from_user.id in _awaiting_restore:
        _awaiting_restore.discard(msg.from_user.id)
        await msg.reply_text("✖️ Restore cancelled.")


async def handle_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Group -1 handler: when an admin who ran /restore uploads a document in DM, validate
    it and atomically swap the live DB. If the admin is not in a restore flow, return
    quietly so the normal group-0 handlers process the message as usual.
    """
    msg = update.message
    if not msg or not msg.document:
        return
    admin_id = msg.from_user.id
    if admin_id not in _awaiting_restore:
        return  # not restoring — let normal handlers run

    incoming = f"{DB_PATH}.incoming"
    try:
        tg_file = await context.bot.get_file(msg.document.file_id)
        await tg_file.download_to_drive(custom_path=incoming)

        ok, detail = _is_valid_sqlite(incoming)
        if not ok:
            await msg.reply_text(
                f"❌ Rejected: {detail}\nThe database was *not* changed. Upload a valid `.db` or send /cancel.",
                parse_mode="Markdown",
            )
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{DB_PATH}.prerestore_{ts}"
            if os.path.exists(DB_PATH):
                shutil.copy2(DB_PATH, backup_path)
            # Drop stale WAL/SHM sidecars so they don't shadow the restored file.
            for sidecar in (f"{DB_PATH}-wal", f"{DB_PATH}-shm"):
                if os.path.exists(sidecar):
                    os.remove(sidecar)
            os.replace(incoming, DB_PATH)  # atomic (same filesystem)
            init_db()                      # ensure schema on the restored file
            _awaiting_restore.discard(admin_id)
            await msg.reply_text(
                f"✅ *Database restored.*\nPrevious DB saved on the server as `{os.path.basename(backup_path)}`.",
                parse_mode="Markdown",
            )
            logger.info("DB restored by admin %s (previous saved as %s)", admin_id, backup_path)
    except Exception as e:
        logger.error("handle_admin_document (restore) failed: %s", e, exc_info=True)
        await msg.reply_text(f"❌ Restore failed: {e}\nThe database was not changed.")
    finally:
        if os.path.exists(incoming):
            try:
                os.remove(incoming)
            except OSError:
                pass

    # We consumed this upload as a restore attempt — don't let it open a support ticket.
    raise ApplicationHandlerStop


# ──────────────────────────────────────────────────────────────────────────────
# 11. Callback query handler (inline keyboard noop)
# ──────────────────────────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data == "noop":
        await query.answer()
        return

    # User guide request
    if data == "user_help":
        await query.answer()
        await context.bot.send_message(
            chat_id=user_id,
            text=HELP_TEXT,
            parse_mode="Markdown"
        )
        return

    # User close ticket request
    if data.startswith("user_close:"):
        parts = data.split(":")
        target_chat_id = int(parts[1])
        thread_id = int(parts[2])

        row = get_user_row(target_chat_id)
        if row and not row["is_closed"]:
            set_closed(target_chat_id, True)

            await query.edit_message_text(
                "✅ *Your support ticket has been closed.*\n\n"
                "Thank you! Feel free to send a message or run /start to open a new session.",
                parse_mode="Markdown"
            )

            # Notify admins in group
            try:
                await context.bot.send_message(
                    GROUP_ID,
                    "🔒 *Ticket closed by the user.*",
                    message_thread_id=thread_id,
                    parse_mode="Markdown"
                )
                await context.bot.close_forum_topic(chat_id=GROUP_ID, message_thread_id=thread_id)
            except Exception as e:
                logger.warning("user_close callback: failed to close thread/notify: %s", e)
            await query.answer("Support ticket closed.")
        else:
            await query.answer("Your ticket is already closed.")
        return

    # Verify admin permissions for admin actions
    if data.startswith("admin_"):
        if user_id not in ADMIN_USER_IDS:
            await query.answer("⛔ You are not authorised to perform this action.", show_alert=True)
            return

        parts = data.split(":")
        action = parts[0]
        target_chat_id = int(parts[1])
        thread_id = int(parts[2])

        if action == "admin_close":
            set_closed_by_thread(thread_id, True)
            try:
                await context.bot.send_message(
                    target_chat_id,
                    "✅ *Your support ticket has been resolved.*\n\n"
                    "Thank you for reaching out! If you need further assistance, "
                    "feel free to use /start to open a new session.",
                    parse_mode="Markdown",
                )
            except Forbidden:
                pass

            try:
                await context.bot.close_forum_topic(chat_id=GROUP_ID, message_thread_id=thread_id)
            except BadRequest as e:
                logger.warning("handle_callback admin_close: could not close forum topic %s: %s", thread_id, e)

            await query.edit_message_text(
                query.message.text + "\n\n✅ *Ticket closed by admin.*",
                parse_mode="Markdown"
            )
            await query.answer("Ticket closed.")

        elif action == "admin_ban":
            set_banned(target_chat_id, True)
            set_closed_by_thread(thread_id, True)
            try:
                await context.bot.send_message(
                    target_chat_id,
                    "⛔ *You have been restricted* from using this support bot.\n"
                    "If you believe this is a mistake, please contact us through another channel.",
                    parse_mode="Markdown",
                )
            except Forbidden:
                pass

            try:
                await context.bot.close_forum_topic(chat_id=GROUP_ID, message_thread_id=thread_id)
            except BadRequest:
                pass

            await query.edit_message_text(
                query.message.text + f"\n\n🚫 *User {target_chat_id} has been banned.*",
                parse_mode="Markdown"
            )
            await query.answer("User banned.", show_alert=True)
        return

    await query.answer()


# ──────────────────────────────────────────────────────────────────────────────
# 12. Global error handler
# ──────────────────────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    logger.error("Unhandled exception:", exc_info=err)

    if isinstance(err, Forbidden):
        chat_id = (
            update.effective_chat.id
            if isinstance(update, Update) and update.effective_chat
            else None
        )
        if chat_id:
            logger.warning("Bot blocked by user %s — marking as banned.", chat_id)
            set_banned(chat_id, True)
        return

    if isinstance(err, NetworkError):
        logger.warning("NetworkError (transient): %s", err)
        return


# ──────────────────────────────────────────────────────────────────────────────
# 13. Webhook setup
# ──────────────────────────────────────────────────────────────────────────────
async def set_webhook() -> None:
    webhook_url = f"{WEBSITE_URL}/webhook/{TOKEN}"
    try:
        await ptb_application.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        logger.info("Webhook set: %s", webhook_url)
    except NetworkError as e:
        logger.error("Network error setting webhook: %s", e)
    except Exception as e:
        logger.error("Unexpected error setting webhook: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# 14. PTB setup & dedicated event loop thread
# ──────────────────────────────────────────────────────────────────────────────
async def _ptb_setup_and_run() -> None:
    """Initialise DB, register handlers, set webhook, and keep PTB loop alive."""
    init_db()

    # ── User commands ──
    ptb_application.add_handler(CommandHandler("start", cmd_start))
    ptb_application.add_handler(CommandHandler("help",  cmd_help))
    user_cmd_filter = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
    ptb_application.add_handler(CommandHandler("close", cmd_user_close, filters=user_cmd_filter))

    # ── Admin commands (private DM) ──
    ptb_application.add_handler(CommandHandler("stats",     cmd_stats))
    ptb_application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    ptb_application.add_handler(CommandHandler("backup",    cmd_backup))
    ptb_application.add_handler(CommandHandler("restore",   cmd_restore))
    ptb_application.add_handler(CommandHandler("cancel",    cmd_cancel))

    # ── Admin DB restore: intercept an uploaded .db in DM BEFORE the ticket handler ──
    # group=-1 runs ahead of the group-0 user-message handler; it raises
    # ApplicationHandlerStop once it consumes a restore upload.
    if ADMIN_USER_IDS:
        ptb_application.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & filters.User(user_id=ADMIN_USER_IDS) & filters.Document.ALL,
                handle_admin_document,
            ),
            group=-1,
        )

    # ── Admin commands (group topic) ──
    group_cmd_filter = filters.Chat(GROUP_ID) & filters.UpdateType.MESSAGE
    ptb_application.add_handler(CommandHandler("close", cmd_close,  filters=group_cmd_filter))
    ptb_application.add_handler(CommandHandler("ban",   cmd_ban,    filters=group_cmd_filter))
    ptb_application.add_handler(CommandHandler("unban", cmd_unban,  filters=group_cmd_filter))

    # ── User messages (private) ──
    user_msg_filter = (
        filters.ChatType.PRIVATE & ~filters.COMMAND & _SUPPORTED_USER_FILTERS
    )
    ptb_application.add_handler(MessageHandler(user_msg_filter, handle_user_message))

    # ── Admin replies (group topic) ──
    if ADMIN_USER_IDS:
        admin_msg_filter = (
            filters.Chat(GROUP_ID) &
            filters.User(user_id=ADMIN_USER_IDS) &
            ~filters.COMMAND &
            _SUPPORTED_USER_FILTERS &
            filters.UpdateType.MESSAGE
        )
        ptb_application.add_handler(MessageHandler(admin_msg_filter, handle_admin_message))
    else:
        logger.warning("No TELEGRAM_ADMINS defined — admin reply forwarding is disabled.")

    # ── Inline keyboard callbacks ──
    ptb_application.add_handler(CallbackQueryHandler(handle_callback))

    # ── Error handler ──
    ptb_application.add_error_handler(error_handler)

    await ptb_application.initialize()
    logger.info("PTB application initialised.")

    if USE_POLLING:
        await ptb_application.start()
        await ptb_application.updater.start_polling(drop_pending_updates=True)
        logger.info("PTB polling started.")
    else:
        await set_webhook()
    logger.info("PTB setup complete. Loop running forever.")


def _start_ptb_thread() -> None:
    """Entry point for the dedicated PTB asyncio thread."""
    global PTB_EVENT_LOOP
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    PTB_EVENT_LOOP = loop

    try:
        loop.run_until_complete(_ptb_setup_and_run())
        PTB_INITIALIZED_EVENT.set()
        loop.run_forever()
    except Exception as e:
        logger.critical("Critical error in PTB thread: %s", e, exc_info=True)
        PTB_INITIALIZED_EVENT.set()  # Unblock webhook handler even on failure
    finally:
        logger.info("PTB thread exiting.")


# ──────────────────────────────────────────────────────────────────────────────
# 15. Flask routes
# ──────────────────────────────────────────────────────────────────────────────
@flask_app.route("/keep_alive", methods=["GET"])
def keep_alive():
    return "✅ Bot is running.", 200


@flask_app.route("/health", methods=["GET"])
def health():
    loop_ok = PTB_EVENT_LOOP is not None and PTB_EVENT_LOOP.is_running()
    return {
        "status": "ok" if loop_ok else "degraded",
        "ptb_loop_running": loop_ok,
    }, 200 if loop_ok else 503


@flask_app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook_route():
    if not PTB_INITIALIZED_EVENT.is_set():
        logger.info("Webhook: waiting for PTB init…")
        ready = PTB_INITIALIZED_EVENT.wait(timeout=15.0)
        if not ready:
            logger.error("Webhook: PTB init timed out.")
            return "Service unavailable: bot not ready", 503

    json_data = flask_request.get_json(silent=True)
    if not json_data:
        return "Bad request: empty body", 400

    if PTB_EVENT_LOOP and PTB_EVENT_LOOP.is_running():
        try:
            update = Update.de_json(json_data, ptb_application.bot)
            asyncio.run_coroutine_threadsafe(
                ptb_application.process_update(update), PTB_EVENT_LOOP
            )
        except Exception as e:
            logger.error("webhook_route: failed to submit update: %s", e, exc_info=True)
            return "Internal server error", 500
    else:
        logger.error("webhook_route: PTB event loop not running.")
        return "Internal server error: loop inactive", 500

    return "OK", 200


# ──────────────────────────────────────────────────────────────────────────────
# 16. Start PTB thread when module is loaded (Gunicorn / direct)
# ──────────────────────────────────────────────────────────────────────────────
def _ensure_ptb_started() -> None:
    global PTB_THREAD
    if PTB_THREAD is None or not PTB_THREAD.is_alive():
        logger.info("Starting PTB dedicated loop thread.")
        PTB_THREAD = threading.Thread(target=_start_ptb_thread, daemon=True, name="PTBThread")
        PTB_THREAD.start()


_ensure_ptb_started()

# ──────────────────────────────────────────────────────────────────────────────
# 17. Direct run (python main.py) — for local dev / simple deployment
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    PTB_INITIALIZED_EVENT.wait()
    if PTB_EVENT_LOOP and PTB_EVENT_LOOP.is_running():
        logger.info("Starting Flask dev server on port %s…", PORT)
        flask_app.run(host="0.0.0.0", port=PORT, debug=False)
    else:
        logger.critical("PTB event loop failed to start. Exiting.")
        raise SystemExit(1)