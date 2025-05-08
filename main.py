import os
import logging
import sqlite3
import asyncio
import requests # Keep requests if you need it for other things, not strictly for PTB webhook setting in this version
import threading
from flask import Flask, request
from telegram import Update # Bot, ForumTopic might not be needed directly at the top level
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import NetworkError, BadRequest

# Load environment variables
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GROUP_ID = int(os.getenv('TELEGRAM_GROUP_ID'))
ADMIN_USER_IDS = [int(admin_id.strip()) for admin_id in os.getenv('TELEGRAM_ADMINS', '').split(',') if admin_id.strip()]
PORT = int(os.getenv('PORT', "8443"))
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
PTB_EVENT_LOOP = None  # This will store the single event loop for PTB
PTB_THREAD = None
PTB_INITIALIZED_EVENT = threading.Event() # To signal when PTB_EVENT_LOOP is ready

# --- Database Functions ---
DB_NAME = 'bot_data.db'

def init_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False) # check_same_thread=False for SQLite with threads
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users
                     (chat_id INTEGER PRIMARY KEY, username TEXT, thread_id INTEGER UNIQUE)''')
    conn.commit()
    conn.close()
    logger.info("Database initialized (or already exists).")

def save_user_to_db(chat_id: int, username: str, thread_id: int):
    try:
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
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
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
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
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
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
async def set_webhook_async(): # Renamed to avoid conflict if there was a non-async one
    global ptb_application
    webhook_url = f"{WEBSITE_URL}/webhook/{TOKEN}"
    try:
        bot = ptb_application.bot
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        logger.info(f"Webhook set successfully: {webhook_url}")
    except NetworkError as e:
        logger.error(f"Network error while setting webhook: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred during set_webhook_async: {e}")

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
            # Removed redundant message to group topic for existing user on /start
        except BadRequest as e:
            logger.error(f"Error informing user about existing session for {chat_id}: {e}")
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
    # username = user.username or user.first_name # Not strictly needed here
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
            await update.message.reply_text("This message type cannot be forwarded at the moment.") # Fallback
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
            # logger.info(f"Admin sent unhandled message type to user for thread {thread_id}")
            await message.reply_text("This message type cannot be forwarded to the user at this time.")
    except BadRequest as e:
        logger.error(f"BadRequest sending admin message from thread {thread_id} to user {user_chat_id}: {e}")
        await message.reply_text(f"Failed to send to user. Error: {e.message}")
    except Exception as e:
        logger.error(f"Error forwarding admin message from {thread_id} to {user_chat_id}: {e}", exc_info=True)
        await message.reply_text("Unexpected error sending to user.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            if isinstance(context.error, BadRequest) and "bot was blocked by the user" in str(context.error).lower():
                logger.warning(f"Bot was blocked by user {update.effective_chat.id if update.effective_chat else 'N/A'}.")
                return
            # Consider removing automatic error reply to user or making it very generic
            # await update.effective_message.reply_text("An error occurred.")
        except Exception as e:
            logger.error(f"Exception in error_handler while trying to inform user: {e}")

# --- PTB Async Setup and Run Logic (to be run in the dedicated thread) ---
async def ptb_initial_setup_and_run():
    global ptb_application

    logger.info("PTB Async Setup: Initializing database...")
    init_db() # Ensure this is thread-safe for SQLite if called from multiple Gunicorn workers without --preload

    logger.info("PTB Async Setup: Adding handlers...")
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

    logger.info("PTB Async Setup: Initializing PTB application...")
    await ptb_application.initialize() # Initializes bot, updater, etc.
    logger.info("PTB Async Setup: Telegram Application initialized.")

    logger.info("PTB Async Setup: Setting webhook...")
    await set_webhook_async()

    logger.info("PTB Async Setup: Complete. PTB ready for updates via webhook.")
    # The event loop will be kept running by loop.run_forever() in the thread function.

def start_ptb_dedicated_loop_thread():
    """Creates a new event loop, runs ptb_initial_setup_and_run in it, and keeps the loop running."""
    global PTB_EVENT_LOOP, PTB_INITIALIZED_EVENT, ptb_application
    logger.info("Attempting to start PTB asyncio event loop in a dedicated thread.")
    
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)
    PTB_EVENT_LOOP = new_loop
    
    try:
        logger.info(f"PTB Thread: Running initial setup on loop {PTB_EVENT_LOOP}...")
        PTB_EVENT_LOOP.run_until_complete(ptb_initial_setup_and_run())
        
        PTB_INITIALIZED_EVENT.set() # Signal that PTB_EVENT_LOOP is ready and PTB is initialized
        logger.info(f"PTB Thread: Event Loop {PTB_EVENT_LOOP} is set up and running forever.")
        
        PTB_EVENT_LOOP.run_forever() # Keep the loop running
        
    except Exception as e:
        logger.critical(f"Critical error in PTB dedicated thread: {e}", exc_info=True)
        PTB_INITIALIZED_EVENT.set() # Also set event on error to unblock waiting threads, they will find loop not running
    finally:
        if PTB_EVENT_LOOP.is_running():
            logger.info("PTB Thread: Stopping and closing event loop...")
            PTB_EVENT_LOOP.call_soon_threadsafe(PTB_EVENT_LOOP.stop)
            # loop.run_until_complete(ptb_application.shutdown()) # May need careful handling for shutdown
        # PTB_EVENT_LOOP.close() # Close loop after it has stopped. run_forever might block this.
        logger.info("PTB Thread: Exited.")

# --- Flask Webhook Route ---
@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook_handler_route():
    global PTB_EVENT_LOOP, ptb_application, PTB_INITIALIZED_EVENT

    if not PTB_INITIALIZED_EVENT.is_set():
        logger.info("Webhook: Waiting for PTB initialization...")
        initialized = PTB_INITIALIZED_EVENT.wait(timeout=10.0) # Wait up to 10 seconds
        if not initialized:
            logger.error("Webhook: PTB initialization timed out. Update cannot be processed.")
            return "Internal server error: Bot not ready (init timeout)", 503 # Service Unavailable

    json_data = request.get_json()
    if not json_data:
        logger.warning("Received empty JSON in webhook")
        return "Empty request", 400

    if PTB_EVENT_LOOP and PTB_EVENT_LOOP.is_running():
        try:
            update = Update.de_json(json_data, ptb_application.bot)
            # Ensure process_update is a coroutine; if it's already async, it's fine.
            # If ptb_application.process_update itself is not an async function but schedules work,
            # this remains correct.
            asyncio.run_coroutine_threadsafe(ptb_application.process_update(update), PTB_EVENT_LOOP)
        except Exception as e: # Catch errors during update processing submission
            logger.error(f"Error submitting update to PTB event loop: {e}", exc_info=True)
            return "Internal server error: Failed to process update", 500
    else:
        logger.error("PTB application event loop is not available or not running after init. Update cannot be processed.")
        return "Internal server error: Bot not ready (loop inactive)", 500
    return "OK", 200

@app.route('/keep_alive', methods=['GET'])
def keep_alive():
    return "Bot's Flask component is running!", 200

# --- Application Startup (Gunicorn Entry Point) ---
# This block runs when main.py is imported by Gunicorn.
# For Gunicorn, using the --preload flag is highly recommended.
# This ensures this module-level code (and thus thread starting)
# runs once in the master process before workers are forked.

if __name__ != '__main__': # Standard check if run by a WSGI server like Gunicorn
    if PTB_THREAD is None: # Basic check to start the thread only once
        logger.info("Gunicorn mode: Initializing and starting PTB dedicated loop thread.")
        PTB_INITIALIZED_EVENT = threading.Event() # Ensure it's created before thread start
        PTB_THREAD = threading.Thread(target=start_ptb_dedicated_loop_thread, daemon=True)
        PTB_THREAD.start()
        # Note: The webhook handler will wait for PTB_INITIALIZED_EVENT.
    else:
        logger.info("Gunicorn mode: PTB dedicated loop thread reference already exists (possibly due to multiple imports or reloads).")

# If you were to run this file directly (e.g., for local testing without Gunicorn and test.py behavior):
# if __name__ == '__main__':
#     logger.info("Direct run mode: Initializing and starting PTB dedicated loop thread.")
#     PTB_INITIALIZED_EVENT = threading.Event()
#     PTB_THREAD = threading.Thread(target=start_ptb_dedicated_loop_thread, daemon=True)
#     PTB_THREAD.start()
#     PTB_INITIALIZED_EVENT.wait() # Wait for PTB to be ready before starting Flask
#     if PTB_EVENT_LOOP and PTB_EVENT_LOOP.is_running():
#         logger.info("Starting Flask development server.")
#         app.run(host='0.0.0.0', port=PORT, debug=False) # Set debug=True for dev if needed
#     else:
#         logger.error("Failed to initialize PTB event loop. Flask server not started.")