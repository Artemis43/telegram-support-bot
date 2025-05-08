import os
import logging
import sqlite3
import asyncio
import requests
import threading
from flask import Flask, request
from telegram import Update, Bot, ForumTopic
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import NetworkError, BadRequest

# Load environment variables
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GROUP_ID = int(os.getenv('TELEGRAM_GROUP_ID')) # Ensure GROUP_ID is an integer
ADMIN_USER_IDS = [int(admin_id.strip()) for admin_id in os.getenv('TELEGRAM_ADMINS', '').split(',') if admin_id.strip()] # Ensure ADMINS are integers
PORT = int(os.getenv('PORT', "8443")) # Default to 8443 if not set
WEBSITE_URL = os.getenv('WEBSITE_URL') # e.g., https://your-app-name.on-render.com

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set.")
if not GROUP_ID:
    raise ValueError("TELEGRAM_GROUP_ID environment variable not set.")
if not WEBSITE_URL:
    raise ValueError("WEBSITE_URL environment variable not set.")
if not ADMIN_USER_IDS:
    logging.warning("TELEGRAM_ADMINS environment variable not set or empty. Admin-specific functions might not work as expected.")


# Initialize Flask app
app = Flask(__name__)

# Logging configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize Telegram bot application
application = Application.builder().token(TOKEN).build()

# --- Database Functions ---
DB_NAME = 'bot_data.db'

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users
                     (chat_id INTEGER PRIMARY KEY, username TEXT, thread_id INTEGER UNIQUE)''') # thread_id should be unique
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def save_user_to_db(chat_id: int, username: str, thread_id: int):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        # Use INSERT OR REPLACE to handle cases where user might /start again (though we try to prevent new topic creation)
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
    webhook_url = f"{WEBSITE_URL}/webhook/{TOKEN}"
    try:
        response = requests.get(f'https://api.telegram.org/bot{TOKEN}/setWebhook?url={webhook_url}&drop_pending_updates=True')
        response.raise_for_status() # Raise an exception for HTTP errors
        if response.json().get("ok"):
            logger.info(f"Webhook set successfully: {webhook_url}")
        else:
            logger.error(f"Failed to set webhook: {response.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error while setting webhook: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred during set_webhook: {e}")


# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    chat_id = update.message.chat_id
    username = user.username or user.first_name # Use first_name if username is not set

    # Check if user already has an active topic
    existing_thread_id = get_user_thread(chat_id)
    if existing_thread_id:
        try:
            await update.message.reply_text(
                f"Hello {username}👋,\nI'm already assisting you. Please continue sending your messages here, "
                f"and they will appear in your dedicated support topic in our group."
            )
            # Optionally, notify the existing topic
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=f"User {username} (Chat ID: {chat_id}) interacted with /start again.",
                message_thread_id=existing_thread_id
            )
        except BadRequest as e:
            logger.error(f"Error informing user/topic about existing session for {chat_id}: {e}")
        return

    # Create a unique topic name
    topic_name = f"Support: {username} ({user.id})" # Using user.id ensures uniqueness even if username changes

    try:
        # Create a new forum topic/thread for the user in the group
        logger.info(f"Attempting to create forum topic '{topic_name}' in group {GROUP_ID}")
        forum_topic: ForumTopic = await context.bot.create_forum_topic(chat_id=GROUP_ID, name=topic_name)
        thread_id = forum_topic.message_thread_id

        logger.info(f"ForumTopic object created: {forum_topic}")
        logger.info(f"Thread ID for user {username} ({chat_id}): {thread_id}")

        if not thread_id:
            logger.error(f"Failed to create or retrieve valid thread_id for topic: {topic_name}. ForumTopic response: {forum_topic}")
            await update.message.reply_text("Sorry, I couldn't set up a support channel for you at the moment. Please try again later.")
            return

        save_user_to_db(chat_id, username, thread_id)

        await update.message.reply_text(
            f"Hello {username}👋,\nHow can I assist you today? "
            "I've created a dedicated support topic for you in our admin group. "
            "Just send your messages here."
        )
        # Send an initial message to the newly created topic from the bot
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=f"New support session started for user: {username} (Chat ID: {chat_id}, User ID: {user.id}).\n"
                 f"Please use this topic to communicate with them.",
            message_thread_id=thread_id
        )

    except BadRequest as e:
        logger.error(f"BadRequest while creating forum topic for {username} ({chat_id}): {e}. "
                     f"Ensure the bot is admin in a 'forum' group (topics enabled) and has 'manage_topics' permission.")
        await update.message.reply_text("I encountered an issue setting up your support channel. Please ensure the bot is configured correctly in the support group. You might need to contact an administrator directly.")
    except Exception as e: # Catch any other unexpected errors
        logger.error(f"An unexpected error occurred in start for {username} ({chat_id}): {e}", exc_info=True)
        await update.message.reply_text("An unexpected error occurred while starting our session. Please try again in a few moments.")

# --- Message Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This handler is for messages from users in private chat
    if not update.message: # Can happen with channel_post_updates etc.
        return

    chat_id = update.message.chat_id
    user = update.message.from_user
    username = user.username or user.first_name

    thread_id = get_user_thread(chat_id)

    if not thread_id:
        logger.warning(f"No thread ID found for user {username} ({chat_id}). They might need to /start first.")
        await update.message.reply_text("I don't have an active support session for you. Please use the /start command to begin.")
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
            logger.info(f"Received unhandled message type from {username} ({chat_id})")
            await update.message.reply_text("I can currently only forward text, photos, videos, and documents.")
            return # Don't confirm if not forwarded
        # Optionally, confirm to user message was forwarded (can be spammy)
        # await update.message.reply_text("Your message has been forwarded to the support team.")

    except BadRequest as e:
        logger.error(f"BadRequest: Failed to send message from user {chat_id} to thread {thread_id}: {e}")
        await update.message.reply_text("Sorry, there was an issue sending your message. Please try again.")
    except Exception as e:
        logger.error(f"Error handling message from user {chat_id} to thread {thread_id}: {e}", exc_info=True)
        await update.message.reply_text("An unexpected error occurred. Please try sending your message again.")


async def forward_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This handler is for messages from admins in the group, within a topic
    if not update.message or not update.message.is_topic_message or not update.message.message_thread_id:
        # Ignore messages not in topics or general topic messages if not replies to bot's topic structure
        # logger.debug("Ignoring non-topic message or message without thread_id in group.")
        return

    message = update.message
    admin_user = message.from_user
    admin_username = admin_user.username or admin_user.first_name

    # Check if the message is from a designated admin (optional, good for strictness)
    # if admin_user.id not in ADMIN_USER_IDS:
    #     logger.debug(f"Message in group topic {message.message_thread_id} from non-admin {admin_username} ({admin_user.id}). Ignoring.")
    #     return

    thread_id = message.message_thread_id
    user_chat_id = get_user_chat_id(thread_id)

    if not user_chat_id:
        logger.warning(f"No user found for thread ID: {thread_id} from admin {admin_username}. Possibly an old topic or direct message in topic.")
        # Optionally reply to the admin in the topic:
        # await message.reply_text("Could not find the original user for this topic. Was this topic created by the bot?")
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
            logger.info(f"Admin {admin_username} sent unhandled message type to user for thread {thread_id}")
            await message.reply_text("This message type cannot be forwarded to the user at this time.")


    except BadRequest as e:
        logger.error(f"BadRequest: Failed to send admin message from thread {thread_id} to user {user_chat_id}: {e}")
        await message.reply_text(f"Failed to send your message to the user. Error: {e.message}") # Inform admin
    except Exception as e:
        logger.error(f"Error forwarding admin message from thread {thread_id} to user {user_chat_id}: {e}", exc_info=True)
        await message.reply_text("An unexpected error occurred while trying to send your message to the user.")


# --- Error Handler ---
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


# --- Flask Webserver for Webhook ---
@app.route('/keep_alive', methods=['GET'])
def keep_alive():
    return "Bot is running!", 200

@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook_handler_route():
    json_data = request.get_json()
    if not json_data:
        logger.warning("Received empty JSON in webhook")
        return "Empty request", 400
    # logger.debug(f"Webhook received: {json_data}")
    update = Update.de_json(json_data, application.bot)

    if application.loop:
        # Submit the process_update coroutine to the main PTB event loop
        future = asyncio.run_coroutine_threadsafe(application.process_update(update), application.loop)
        try:
            # It's good practice to see if the task was accepted,
            # but for webhooks, you usually return "OK" quickly.
            # You could add a short timeout if you want to ensure it started processing.
            # future.result(timeout=1) # Example: wait up to 1 second for the task to be scheduled/start
            pass # Returning "OK" quickly is typical for webhooks
        except Exception as e:
            logger.error(f"Error when submitting process_update to event loop: {e}")
            # Consider returning an error to Telegram if submission fails critically
            # return "Error processing update", 500
    else:
        logger.error("PTB application event loop not available for webhook processing. Update may not be handled.")
        # This indicates a potential issue with application initialization or lifecycle.
        return "Internal server error: Bot loop not ready", 500

    return "OK", 200

def run_flask():
    logger.info(f"Starting Flask server on host 0.0.0.0 port {PORT}")
    # Use a production-ready WSGI server like gunicorn or waitress in production
    app.run(host='0.0.0.0', port=PORT, debug=False) # Set debug=False for production


# --- Main Bot Logic ---
# ... (other parts of your code) ...

async def main():
    init_db()

    # Add handlers
    application.add_handler(CommandHandler("start", start))

    # Handler for user messages (private chat, not commands)
    user_message_filters = (
        filters.ChatType.PRIVATE & ~filters.COMMAND &
        (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.VOICE | filters.AUDIO) # Corrected here
    )
    application.add_handler(MessageHandler(user_message_filters, handle_message))

    # Handler for admin messages in the group topic (ensure ADMINS is populated)
    if ADMIN_USER_IDS: # Only add admin handler if ADMINS are defined
        admin_message_filters = (
            filters.Chat(GROUP_ID) & filters.User(user_id=ADMIN_USER_IDS) & ~filters.COMMAND &
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.VOICE | filters.AUDIO) & # Corrected here
            filters.UpdateType.MESSAGE # Ensure it's a new message
        )
        application.add_handler(MessageHandler(admin_message_filters, forward_admin_message))
    else:
        logger.warning("TELEGRAM_ADMINS not defined or empty. Admin reply forwarding will not work.")

    application.add_error_handler(error_handler)

    # Initialize the PTB application (this also initializes application.bot and application.updater)
    await application.initialize()
    logger.info("Telegram Application initialized.")

    # Set the webhook using application.bot
    await set_webhook()

    # Start the Flask app in a separate daemon thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask thread started and running in background.")

    try:
        logger.info("Bot is running. Press Ctrl+C to stop.")
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal (KeyboardInterrupt/SystemExit) received in main loop.")
    finally:
        logger.info("Initiating PTB application shutdown...")
        await application.shutdown()
        logger.info("PTB application shutdown complete.")


if __name__ == '__main__':
    # ... (your existing startup checks) ...
    # (Ensure logger is configured before being used in __main__ if critical errors occur early)
    # logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    # logger = logging.getLogger(__name__)
    
    if not TOKEN:
        # logger might not be configured if this is the first line __main__ and it fails
        print("CRITICAL: TELEGRAM_BOT_TOKEN environment variable not found! Exiting.") 
        exit(1)
    if os.getenv('TELEGRAM_GROUP_ID') is None:
        print("CRITICAL: TELEGRAM_GROUP_ID environment variable not found! Exiting.")
        exit(1)
    if not WEBSITE_URL:
        # logger might not be configured here either
        print("WARNING: WEBSITE_URL environment variable not found! Webhook setup will fail if not already set.")

    try:
        asyncio.run(main())
    except Exception as e: 
        # Use logger if available, otherwise print
        if 'logger' in globals():
            logger.critical(f"Critical error during bot execution: {e}", exc_info=True)
        else:
            print(f"CRITICAL error during bot execution: {e}")
    finally:
        if 'logger' in globals():
            logger.info("Exiting application.")
        else:
            print("Exiting application.")