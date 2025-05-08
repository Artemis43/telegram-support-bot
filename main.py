import os
import logging
import sqlite3
import asyncio
import requests
import threading # Ensure threading is imported
from flask import Flask, request
from telegram import Update, Bot, ForumTopic # Removed Bot, ForumTopic if not used directly here
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import NetworkError, BadRequest


# Load environment variables
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GROUP_ID = int(os.getenv('TELEGRAM_GROUP_ID'))
ADMIN_USER_IDS = [int(admin_id.strip()) for admin_id in os.getenv('TELEGRAM_ADMINS', '').split(',') if admin_id.strip()]
PORT = int(os.getenv('PORT', "8443")) # Gunicorn will use this if specified in Procfile/command
WEBSITE_URL = os.getenv('WEBSITE_URL')

# Critical environment variable checks
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set.")
if not GROUP_ID:
    raise ValueError("TELEGRAM_GROUP_ID environment variable not set.")
if not WEBSITE_URL:
    raise ValueError("WEBSITE_URL environment variable not set.")
if not ADMIN_USER_IDS:
    logging.warning("TELEGRAM_ADMINS environment variable not set or empty. Admin-specific functions might not work as expected.")

# Initialize Flask app - Gunicorn will look for this 'app' object
app = Flask(__name__)

# Logging configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Global variables for PTB ---
ptb_application = Application.builder().token(TOKEN).build()
PTB_EVENT_LOOP = None
PTB_THREAD = None # To hold the reference to the PTB thread

# --- Database Functions ---
DB_NAME = 'bot_data.db'

def init_db():
    # Ensure this function is safe to call multiple times or is called only once.
    # If Gunicorn workers all import main.py, this might be called by each.
    # For SQLite, file creation is usually fine.
    # More complex DB setups might need a dedicated migration/setup step.
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users
                     (chat_id INTEGER PRIMARY KEY, username TEXT, thread_id INTEGER UNIQUE)''')
    conn.commit()
    conn.close()
    logger.info("Database initialized (or already exists).")

# ... (Your existing save_user_to_db, get_user_chat_id, get_user_thread functions) ...
def save_user_to_db(chat_id: int, username: str, thread_id: int):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO users (chat_id, username, thread_id) VALUES (?, ?, ?)',
                       (chat_id, username, thread_id))
        conn.commit()
        logger.info(f"Saved/Updated user {username} ({chat_id}) with thread_id {thread_id}")
    except sqlite3.Error as e:
        logger.error(f"Database error in save_user_to_db: {e}")
    finally:
        if conn:
            conn.close()

def get_user_chat_id(thread_id: int) -> int | None:
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT chat_id FROM users WHERE thread_id=?', (thread_id,))
        result = cursor.fetchone()
        return result[0] if result else None
    except sqlite3.Error as e:
        logger.error(f"Database error in get_user_chat_id: {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_user_thread(chat_id: int) -> int | None:
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT thread_id FROM users WHERE chat_id=?', (chat_id,))
        result = cursor.fetchone()
        return result[0] if result else None
    except sqlite3.Error as e:
        logger.error(f"Database error in get_user_thread: {e}")
        return None
    finally:
        if conn:
            conn.close()

# --- Webhook Setup ---
async def set_webhook():
    global ptb_application # Use the global ptb_application instance
    webhook_url = f"{WEBSITE_URL}/webhook/{TOKEN}"
    try:
        # Use ptb_application.bot for sending the request
        bot = ptb_application.bot
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        logger.info(f"Webhook set successfully: {webhook_url}")
    except NetworkError as e:
        logger.error(f"Network error while setting webhook: {e}")
    except Exception as e: # Catch more specific exceptions if possible
        logger.error(f"An unexpected error occurred during set_webhook: {e}")


# --- Command Handlers & Message Handlers (use global ptb_application) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    chat_id = update.message.chat_id
    username = user.username or user.first_name

    existing_thread_id = get_user_thread(chat_id)
    if existing_thread_id:
        try:
            await update.message.reply_text(
                f"Hello {username}👋,\nHow can I assist you today?"
            )
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=f"User {username} (Chat ID: {chat_id}) interacted with /start again.",
                message_thread_id=existing_thread_id
            )
        except BadRequest as e:
            logger.error(f"Error informing user/topic about existing session for {chat_id}: {e}")
        return

    topic_name = f"Support: {username} ({user.id})"
    try:
        forum_topic = await context.bot.create_forum_topic(chat_id=GROUP_ID, name=topic_name)
        thread_id = forum_topic.message_thread_id
        if not thread_id:
            logger.error(f"Failed to create or retrieve valid thread_id for topic: {topic_name}.")
            await update.message.reply_text("Sorry, I couldn't set up a support channel. Try again later.")
            return
        save_user_to_db(chat_id, username, thread_id)
        await update.message.reply_text(
            f"Hello {username}👋,\nHow can I assist you today?"
        )
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=f"New support session: {username} (Chat ID: {chat_id}, User ID: {user.id}).",
            message_thread_id=thread_id
        )
    except BadRequest as e:
        logger.error(f"BadRequest creating topic for {username} ({chat_id}): {e}.")
        await update.message.reply_text("Error setting up support channel. Contact admin.")
    except Exception as e:
        logger.error(f"Unexpected error in start for {username} ({chat_id}): {e}", exc_info=True)
        await update.message.reply_text("Unexpected error starting session. Try again.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.message.chat_id
    user = update.message.from_user
    username = user.username or user.first_name
    thread_id = get_user_thread(chat_id)

    if not thread_id:
        await update.message.reply_text("No active session. Use /start.")
        return

    try:
        # Forward the user's message to the corresponding thread in the support group
        if update.message.text:
            await context.bot.send_message(chat_id=GROUP_ID, text=update.message.text, message_thread_id=thread_id)
        elif update.message.photo:
            await context.bot.send_photo(chat_id=GROUP_ID, photo=update.message.photo[-1].file_id, caption=update.message.caption, message_thread_id=thread_id)
        elif update.message.document:
            await context.bot.send_document(chat_id=GROUP_ID, document=update.message.document.file_id, caption=update.message.caption, message_thread_id=thread_id)
        elif update.message.video:
            await context.bot.send_video(chat_id=GROUP_ID, video=update.message.video.file_id, caption=update.message.caption, message_thread_id=thread_id)
        elif update.message.voice:
            await context.bot.send_voice(chat_id=GROUP_ID, voice=update.message.voice.file_id, caption=update.message.caption, message_thread_id=thread_id)
        elif update.message.audio:
            await context.bot.send_audio(chat_id=GROUP_ID, audio=update.message.audio.file_id, caption=update.message.caption, message_thread_id=thread_id)
        # Add other media types as needed (sticker, video_note, etc.)
        else:
            await update.message.reply_text("Can only forward text messages currently.")
    except BadRequest as e:
        logger.error(f"BadRequest sending user message from {chat_id} to thread {thread_id}: {e}")
        await update.message.reply_text("Issue sending message. Try again.")
    except Exception as e:
        logger.error(f"Error handling message from {chat_id} to {thread_id}: {e}", exc_info=True)
        await update.message.reply_text("Unexpected error sending message.")

async def forward_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.is_topic_message or not update.message.message_thread_id:
        return
    message = update.message
    thread_id = message.message_thread_id
    user_chat_id = get_user_chat_id(thread_id)

    if not user_chat_id:
        # logger.warning(f"No user for thread ID: {thread_id}. Admin: {message.from_user.username or message.from_user.first_name}")
        return

    try:
        if message.text:
            await context.bot.send_message(chat_id=user_chat_id, text=message.text)
        elif message.photo:
            await context.bot.send_photo(chat_id=user_chat_id, photo=message.photo[-1].file_id, caption=message.caption)
        elif message.document:
            await context.bot.send_document(chat_id=user_chat_id, document=message.document.file_id, caption=message.caption)
        elif message.video:
            await context.bot.send_video(chat_id=user_chat_id, video=message.video.file_id, caption=message.caption)
        elif message.voice:
            await context.bot.send_voice(chat_id=user_chat_id, voice=message.voice.file_id, caption=message.caption)
        elif message.audio:
            await context.bot.send_audio(chat_id=user_chat_id, audio=message.audio.file_id, caption=message.caption)
        # Add other media types as needed
        else:
            logger.info(f"Admin sent unhandled message type to user for thread {thread_id}")
            await message.reply_text("This message type cannot be forwarded to the user at this time.")


    except BadRequest as e:
        logger.error(f"BadRequest sending admin message from thread {thread_id} to user {user_chat_id}: {e}")
        await message.reply_text(f"Failed to send to user. Error: {e.message}")
    except Exception as e:
        logger.error(f"Error forwarding admin message from {thread_id} to {user_chat_id}: {e}", exc_info=True)
        await message.reply_text("Unexpected error sending to user.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    # For specific errors, you might want to inform the user if the update object is available
    if isinstance(update, Update) and update.effective_message:
        try:
            # Avoid sending error messages for some known issues, e.g. if a user blocks the bot
            if isinstance(context.error, BadRequest) and "bot was blocked by the user" in str(context.error).lower():
                logger.warning(f"Bot was blocked by user {update.effective_chat.id if update.effective_chat else 'N/A'}. Cannot send message.")
                return # Don't try to send a message back

            # await update.effective_message.reply_text(
            #     "Sorry, an unexpected error occurred. The developers have been notified."
            # )
        except Exception as e:
            logger.error(f"Exception in error_handler while trying to inform user: {e}")

# --- Flask Webhook Route ---
@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook_handler_route():
    global PTB_EVENT_LOOP, ptb_application # Access global variables
    json_data = request.get_json()
    if not json_data:
        logger.warning("Received empty JSON in webhook")
        return "Empty request", 400

    update = Update.de_json(json_data, ptb_application.bot) # Use ptb_application.bot

    if PTB_EVENT_LOOP and PTB_EVENT_LOOP.is_running():
        future = asyncio.run_coroutine_threadsafe(ptb_application.process_update(update), PTB_EVENT_LOOP)
        try:
            # Optionally, you can add a timeout to future.result() if you want to wait
            # for a brief moment, but for webhooks, it's best to return "OK" quickly.
            # future.result(timeout=1) # e.g., wait 1 second
            pass
        except Exception as e:
            logger.error(f"Error when submitting/awaiting process_update via run_coroutine_threadsafe: {e}")
            # Consider returning 500 if the submission itself fails critically
    else:
        logger.error("PTB application event loop is not available or not running. Update cannot be processed.")
        return "Internal server error: Bot not ready", 500
    return "OK", 200

@app.route('/keep_alive', methods=['GET']) # For Render's health checks or other services
def keep_alive():
    return "Bot's Flask component is running!", 200

# --- PTB Main Async Logic (to be run in a separate thread) ---
async def ptb_main_runner():
    global PTB_EVENT_LOOP, ptb_application # Use global instances

    logger.info("PTB main_runner: Initializing database...")
    init_db() # Initialize DB once when PTB starts

    logger.info("PTB main_runner: Adding handlers...")
    ptb_application.add_handler(CommandHandler("start", start))
    user_message_filters = (
        filters.ChatType.PRIVATE & ~filters.COMMAND &
        (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.VOICE | filters.AUDIO)
    )
    ptb_application.add_handler(MessageHandler(user_message_filters, handle_message))
    if ADMIN_USER_IDS:
        admin_message_filters = (
            filters.Chat(GROUP_ID) & filters.User(user_id=ADMIN_USER_IDS) & ~filters.COMMAND &
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.VOICE | filters.AUDIO) &
            filters.UpdateType.MESSAGE
        )
        ptb_application.add_handler(MessageHandler(admin_message_filters, forward_admin_message))
    ptb_application.add_error_handler(error_handler)

    logger.info("PTB main_runner: Initializing PTB application...")
    await ptb_application.initialize() # Initializes bot, updater, etc.
    logger.info("PTB main_runner: Telegram Application initialized.")

    PTB_EVENT_LOOP = asyncio.get_running_loop()
    logger.info(f"PTB main_runner: Event Loop captured: {PTB_EVENT_LOOP}")

    # Set the webhook once PTB application is initialized and loop is running
    logger.info("PTB main_runner: Setting webhook...")
    await set_webhook() # Ensure this uses ptb_application.bot

    logger.info("PTB main_runner: Asyncio event loop running. PTB ready for updates.")
    try:
        # Keep the asyncio event loop running indefinitely
        while True:
            await asyncio.sleep(3600) # Or some other mechanism to keep alive
    except asyncio.CancelledError:
        logger.info("PTB main_runner: Asyncio loop cancelled.")
    finally:
        logger.info("PTB main_runner: Shutting down PTB application...")
        await ptb_application.shutdown()
        logger.info("PTB main_runner: PTB application shutdown complete.")

def start_ptb_thread():
    """Starts the PTB's asyncio event loop in a new thread."""
    logger.info("Attempting to start PTB asyncio event loop in a new thread.")
    try:
        asyncio.run(ptb_main_runner())
    except Exception as e:
        logger.critical(f"Critical error in PTB thread (start_ptb_thread): {e}", exc_info=True)

# --- Application Startup ---
# This block runs when main.py is imported by Gunicorn.
# It ensures the PTB part is initialized and running in its own thread.
if PTB_THREAD is None or not PTB_THREAD.is_alive():
    logger.info("main.py loaded: Initializing and starting PTB thread.")
    PTB_THREAD = threading.Thread(target=start_ptb_thread, daemon=True)
    PTB_THREAD.start()
else:
    logger.info("main.py loaded: PTB thread already appears to be running.")

# Note: The Flask app 'app' is now ready to be served by Gunicorn.
# The if __name__ == '__main__': block that previously ran app.run() and asyncio.run(main_async_logic)
# is removed because Gunicorn will manage the Flask app's lifecycle,
# and we've started the PTB logic in a daemon thread above.