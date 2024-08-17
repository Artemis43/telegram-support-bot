import os
import logging
import sqlite3
import asyncio
import requests
import threading
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import NetworkError, BadRequest  # Ensure the required telegram modules are imported

# Load environment variables
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GROUP_ID = int(os.getenv('TELEGRAM_GROUP_ID'))  # Ensure GROUP_ID is an integer
ADMINS = list(map(int, os.getenv('TELEGRAM_ADMINS').split(',')))
PORT = int(os.getenv('PORT', 8443))
WEBSITE_URL = os.getenv('WEBSITE_URL')

# Initialize Flask app
app = Flask(__name__)

# Logging configuration
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Initialize Telegram bot application
application = Application.builder().token(TOKEN).build()

# Database setup (sqlite3 example)
def init_db():
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users
                      (chat_id INTEGER PRIMARY KEY, username TEXT, thread_id INTEGER)''')
    conn.commit()
    conn.close()

def save_user_to_db(chat_id, username, thread_id):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO users (chat_id, username, thread_id) VALUES (?, ?, ?)',
                   (chat_id, username, thread_id))
    conn.commit()
    conn.close()

def get_user_chat_id(thread_id):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute('SELECT chat_id FROM users WHERE thread_id=?', (thread_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def get_user_thread(chat_id):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute('SELECT thread_id FROM users WHERE chat_id=?', (chat_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

# Automatically set the webhook when the bot starts
async def set_webhook():
    webhook_url = f"{WEBSITE_URL}/webhook/{TOKEN}"
    response = requests.get(f'https://api.telegram.org/bot{TOKEN}/setWebhook?url={webhook_url}')
    if response.status_code == 200:
        logging.info(f"Webhook set successfully: {webhook_url}")
    else:
        logging.error(f"Failed to set webhook: {response.text}")

# Command handler for /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    chat_id = update.message.chat_id
    username = user.username or user.first_name
    topic_name = f'{chat_id}_{username}'

    # Create a new forum topic/thread for the user in the group
    forum_topic = await context.bot.create_forum_topic(chat_id=GROUP_ID, name=topic_name)

    # Send a message in the newly created topic to track it
    initial_message = await context.bot.send_message(chat_id=GROUP_ID, text="Thread created", message_thread_id=forum_topic.message_thread_id)
    thread_id = initial_message.message_thread_id  # This should be used as the thread ID

    # Log for debugging purposes
    logging.info(f"Thread ID for user {username} ({chat_id}): {thread_id}")

    # Save user details and thread id to the database
    save_user_to_db(chat_id, username, thread_id)

    # Send greeting message to the user
    await update.message.reply_text(f"Hello {username}👋,\nHow can I assist you today?")

# Message handler for forwarding user messages to the respective thread in the group
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    # Ensure the message is from a private chat (not the group or bot)
    if chat_id != GROUP_ID:
        # Retrieve the user's associated thread in the group
        thread_id = get_user_thread(chat_id)
        
        if thread_id:
            try:
                # Forward the user's message to the corresponding thread in the support group
                if update.message.text:
                    user_message = update.message.text
                    await context.bot.send_message(chat_id=GROUP_ID, text=user_message, message_thread_id=thread_id)
                elif update.message.photo:
                    photo = update.message.photo[-1].file_id
                    caption = update.message.caption
                    await context.bot.send_photo(chat_id=GROUP_ID, photo=photo, caption=caption, message_thread_id=thread_id)
                elif update.message.document:
                    document = update.message.document.file_id
                    caption = update.message.caption
                    await context.bot.send_document(chat_id=GROUP_ID, document=document, caption=caption, message_thread_id=thread_id)
                elif update.message.video:
                    video = update.message.video.file_id
                    caption = update.message.caption
                    await context.bot.send_video(chat_id=GROUP_ID, video=video, caption=caption, message_thread_id=thread_id)
                # Add support for other media types as needed
            except BadRequest as e:
                logging.error(f"Failed to send message: {e}")
        else:
            # If no thread is found, log an error
            logging.error(f"No thread ID found for user {chat_id}. Cannot forward the message.")
    else:
        logging.error("Message received from the group, ignoring...")

# Message handler for forwarding admin messages in the group back to the user
async def forward_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    # Check if the message is sent by an admin and has a thread ID
    if message.from_user.id in ADMINS and message.chat_id == GROUP_ID and message.message_thread_id:
        thread_id = message.message_thread_id

        # Find which user is associated with this thread
        user_chat_id = get_user_chat_id(thread_id)

        if user_chat_id:
            try:
                # Forward the admin's message to the corresponding user
                if message.text:
                    admin_message = message.text
                    await context.bot.send_message(chat_id=user_chat_id, text=admin_message)
                elif message.photo:
                    photo = message.photo[-1].file_id
                    caption = message.caption
                    await context.bot.send_photo(chat_id=user_chat_id, photo=photo, caption=caption)
                elif message.document:
                    document = message.document.file_id
                    caption = message.caption
                    await context.bot.send_document(chat_id=user_chat_id, document=document, caption=caption)
                elif message.video:
                    video = message.video.file_id
                    caption = message.caption
                    await context.bot.send_video(chat_id=user_chat_id, video=video, caption=caption)
                # Add support for other media types as needed
            except BadRequest as e:
                logging.error(f"Failed to send message: {e}")
        else:
            logging.error(f"No user found for thread ID: {thread_id}")
    else:
        logging.error("Message is not from an admin, not from the group, or lacks a thread ID")

# Admin command to stop the bot
# Didn't find this useful. Moreover gives some bullshit errors.

"""async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id in ADMINS:
        await update.message.reply_text("Stopping the bot...")
        await context.application.stop()
    else:
        await update.message.reply_text("You don't have permission to stop the bot!")"""

# Keep alive endpoint for cron jobs
@app.route('/keep_alive', methods=['GET'])
def keep_alive():
    return "Bot is running!", 200

# Webhook handler for Telegram
@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook_handler():
    update = Update.de_json(request.get_json(), application.bot)  # Access bot from the application
    
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # No running loop in the current thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    loop.run_until_complete(application.process_update(update))
    return "OK", 200

# Error handler to log exceptions
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(msg="Exception while handling an update:", exc_info=context.error)

# This gives me some errors. But helps me run the bot.

"""if __name__ == '__main__':
    # Initialize the bot and webhook
    init_db()
    
    # Add handlers for /start, user message handling, and admin message forwarding
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.Chat(GROUP_ID), handle_message))
    application.add_handler(MessageHandler(filters.ALL & filters.Chat(GROUP_ID) & filters.User(ADMINS), forward_admin_message))
    
    # Register the error handler
    application.add_error_handler(error_handler)

    # Ensure that the event loop is running
    loop = asyncio.get_event_loop()
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.run_until_complete(application.initialize())  # Initialize the application properly
    loop.run_until_complete(set_webhook())  # Set the webhook asynchronously

    # Start the Flask app (bind to 0.0.0.0 to expose the service)
    app.run(host='0.0.0.0', port=PORT)"""

# Function to run the Flask app
def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# Main async function to initialize and run the bot
async def main():
    init_db()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.Chat(GROUP_ID), handle_message))
    application.add_handler(MessageHandler(filters.ALL & filters.Chat(GROUP_ID) & filters.User(ADMINS), forward_admin_message))
    application.add_error_handler(error_handler)

    await application.initialize()
    await set_webhook()

    # Start the Flask app in a separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Start the bot application
    await application.start()

if __name__ == '__main__':
    asyncio.run(main())
