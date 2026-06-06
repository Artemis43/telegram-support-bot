# 🤖 Telegram Support Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-20.8-blue)](https://python-telegram-bot.org/)

A production-ready Telegram support relay bot. Every user who messages your bot gets their own **Forum Topic** in your private admin group — giving your support team a clean, organised inbox like a helpdesk.

```
User DMs Bot  →  Bot creates a Forum Topic in your group
Admin replies in Topic  →  Bot forwards reply back to User
```

---

## ✨ Features

- 💬 **Two-way messaging** — users DM the bot; admin replies from the group topic are forwarded back
- 📁 **Full media support** — text, photos, videos, files, voice, audio, stickers, GIFs, video circles, location, contacts
- 🎫 **Ticket lifecycle** — open, close, reopen sessions
- 🚫 **Ban / Unban** — block spamming users instantly from within the topic
- 📢 **Broadcast** — send an announcement to all active users at once
- 📊 **Stats dashboard** — view session counts and daily activity
- ⚡ **Rate limiting** — prevent message flooding (configurable)
- 👍 **Delivery receipts** — emoji reaction confirms message was forwarded
- 🗃️ **SQLite persistence** — all sessions, bans, and timestamps stored locally
- 🚀 **Webhook-based** — production-ready, no polling

---

## 🗂️ Table of Contents

1. [Prerequisites](#-prerequisites)
2. [Step 1 — Create your Telegram Bot](#-step-1--create-your-telegram-bot)
3. [Step 2 — Set up the Support Group](#-step-2--set-up-the-support-group)
4. [Step 3 — Get your IDs](#-step-3--get-your-ids)
5. [Step 4 — Deploy to Render (recommended)](#-step-4--deploy-to-render-recommended)
6. [Step 5 — Configure Environment Variables](#-step-5--configure-environment-variables)
7. [Local Testing](#-local-testing)
8. [Commands Reference](#-commands-reference)
9. [Troubleshooting](#-troubleshooting)

---

## 📋 Prerequisites

- A free [Telegram](https://telegram.org/) account
- A free [Render](https://render.com/) account (for hosting)
- Python 3.10+ (only needed for local testing)

---

## 📌 Step 1 — Create your Telegram Bot

1. Open Telegram and search for **[@BotFather](https://t.me/botfather)**.
2. Send `/newbot` and follow the prompts:
   - Choose a **name** (e.g. `My Support Bot`)
   - Choose a **username** ending in `bot` (e.g. `mysupport_bot`)
3. BotFather will reply with your **Bot Token** — copy and save it.
   ```
   Example: 7463401204:AAEHXf_PD9evTlH9RMhPqeyDA4KSAmjuDGA
   ```

> [!IMPORTANT]
> Keep your Bot Token secret. Never share it or commit it to Git.

---

## 👥 Step 2 — Set up the Support Group

1. Create a **new private group** in Telegram (e.g. *"My Support Inbox"*).
2. Enable **Topics** in the group:
   - Open group → tap the group name → **Edit** → toggle **Topics** on.
3. Add your bot to the group as an **Administrator**:
   - Go to group → tap the group name → **Administrators** → **Add Admin** → search your bot.
   - Enable the **Manage Topics** permission for it.

---

## 🪪 Step 3 — Get your IDs

You need two IDs: **Group ID** and **your personal Admin User ID**.

### Get your Group ID

1. Forward any message from your support group to **[@userinfobot](https://t.me/userinfobot)**.
2. It will reply with the group details. The **ID** will look like `-1002234879626`.

### Get your User ID

1. Message **[@userinfobot](https://t.me/userinfobot)** directly (no forwarding).
2. It will show your personal **ID** (e.g. `7093051689`).

> If you have multiple admins, collect all their User IDs — you'll enter them comma-separated later.

---

## 🚀 Step 4 — Deploy to Render (Recommended)

Render offers a **free tier** that is perfect for this bot.

### 4.1 Fork / Clone the repository

```bash
git clone https://github.com/Artemis43/telegram-support-bot.git
cd telegram-support-bot
```

Push it to your own GitHub account (Render deploys from GitHub).

### 4.2 Create a new Web Service on Render

1. Go to [render.com](https://render.com/) and log in.
2. Click **New → Web Service**.
3. Connect your GitHub account and select your forked repository.
4. Fill in the following settings:

   | Setting | Value |
   |---|---|
   | **Name** | `telegram-support-bot` (or any name you like) |
   | **Region** | Choose one closest to you |
   | **Branch** | `main` |
   | **Runtime** | `Python 3` |
   | **Build Command** | `pip install -r requirements.txt` |
   | **Start Command** | `gunicorn main:flask_app` |
   | **Instance Type** | `Free` |

5. Click **Create Web Service** — Render will build and deploy the app.
6. Once deployed, copy your app's public URL from the top of the page:
   ```
   https://your-app-name.onrender.com
   ```

### 4.3 Set Environment Variables on Render

In your Render service, go to **Environment** and add the following variables:

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `TELEGRAM_GROUP_ID` | Your group ID (e.g. `-1002234879626`) |
| `TELEGRAM_ADMINS` | Your user ID(s), comma-separated (e.g. `111111,222222`) |
| `WEBSITE_URL` | Your Render app URL (e.g. `https://your-app-name.onrender.com`) |

Click **Save Changes** — Render will automatically redeploy.

> [!NOTE]
> After saving, watch the **Logs** tab. You should see:
> ```
> Webhook set: https://your-app-name.onrender.com/webhook/...
> PTB setup complete. Loop running forever.
> ```
> That means the bot is live!

### 4.4 Test it

Open Telegram, go to your bot and send `/start`. You should get a welcome message, and a new topic should appear in your support group. 🎉

---

## ⚙️ Step 5 — Configure Environment Variables

All configuration is done through environment variables. Here is the full reference:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TELEGRAM_GROUP_ID` | ✅ | — | Your forum-enabled group ID |
| `TELEGRAM_ADMINS` | ✅ | — | Comma-separated admin user IDs |
| `WEBSITE_URL` | ✅ | — | Your public HTTPS deployment URL |
| `PORT` | ❌ | `8443` | Flask server port |
| `DB_PATH` | ❌ | `bot_data.db` | Path to the SQLite database file |
| `RATE_LIMIT_MAX` | ❌ | `5` | Max messages a user can send per window |
| `RATE_LIMIT_WINDOW` | ❌ | `10` | Rate limit time window in seconds |

---

## 🧪 Local Testing

You can test the bot on your local machine without deploying.

### 1. Clone and set up

```bash
git clone https://github.com/Artemis43/telegram-support-bot.git
cd telegram-support-bot

python -m venv venv

# Windows:
.\venv\Scripts\Activate.ps1
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in your values:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_GROUP_ID=-1002234879626
TELEGRAM_ADMINS=your_user_id_here
PORT=8443
WEBSITE_URL=          # fill in after Step 3
```

### 3. Expose your local port with a tunnel

Telegram needs a public HTTPS URL to send updates. Use a free SSH tunnel:

**In a new terminal window, run:**

```bash
# Windows (use 127.0.0.1 explicitly to avoid IPv6 issues):
ssh -R 80:127.0.0.1:8443 localhost.run

# macOS/Linux:
ssh -R 80:localhost:8443 localhost.run
```

It will output a URL like:
```
https://4de96ba117e7a3.lhr.life  tunneled with tls termination
```

Copy that URL and paste it into your `.env`:

```env
WEBSITE_URL=https://4de96ba117e7a3.lhr.life
```

> [!WARNING]
> **Windows users:** Always use `127.0.0.1` instead of `localhost` in the SSH command.
> On Windows, `localhost` often resolves to the IPv6 address `[::1]`, but Flask listens on IPv4 `127.0.0.1`, which causes the tunnel to silently drop all requests.

### 4. Start the bot

```bash
python main.py
```

You should see:
```
Webhook set: https://4de96ba117e7a3.lhr.life/webhook/...
PTB setup complete. Loop running forever.
Starting Flask dev server on port 8443…
```

### 5. Send `/start` to your bot in Telegram!

> Keep both terminal windows open — closing the tunnel will stop webhook delivery.

---

## 📖 Commands Reference

### User commands (in private DM with the bot)

| Command | Description |
|---|---|
| `/start` | Start a new support session (or resume an existing one) |
| `/help` | Show available commands |

### Admin commands — inside a group support topic

Run these from **inside the specific user's forum topic** in your support group:

| Command | Description |
|---|---|
| `/close` | ✅ Mark ticket as resolved — notifies the user and archives the topic |
| `/ban` | 🚫 Ban the user — they can no longer send messages via the bot |
| `/unban` | ✅ Lift the ban — user is notified and can open a new session |

### Admin commands — private DM with the bot

Run these by **DMing the bot directly** (only works if your ID is in `TELEGRAM_ADMINS`):

| Command | Description |
|---|---|
| `/stats` | 📊 Show total sessions, active sessions, banned users, today's activity |
| `/broadcast <message>` | 📢 Send a message to all non-banned users |

---

## 🛠️ Troubleshooting

<details>
<summary><strong>Bot doesn't respond to /start</strong></summary>

1. Check that the webhook is registered: open `https://api.telegram.org/bot<YOUR_TOKEN>/getWebhookInfo` in your browser.
2. Confirm `url` matches your deployment URL and there is no `last_error_message`.
3. For local testing: confirm your tunnel is still running and `WEBSITE_URL` in `.env` matches the tunnel URL.

</details>

<details>
<summary><strong>Forum topic not created in the group</strong></summary>

1. Make sure your group has **Topics enabled** (Group Settings → Topics).
2. Confirm the bot is an **Administrator** with the **Manage Topics** permission.
3. Double-check `TELEGRAM_GROUP_ID` — it must start with `-100...`.

</details>

<details>
<summary><strong>Admin replies are not forwarded to the user</strong></summary>

1. Confirm your Telegram User ID is listed in `TELEGRAM_ADMINS`.
2. Make sure you're replying **inside the correct forum topic** (not the General topic).
3. Do not use `/` commands — just send a plain message or media.

</details>

<details>
<summary><strong>Local tunnel drops connection immediately (Windows)</strong></summary>

Use `127.0.0.1` instead of `localhost` in the SSH command:
```bash
ssh -R 80:127.0.0.1:8443 localhost.run
```

</details>

<details>
<summary><strong>Render free tier goes to sleep</strong></summary>

Render's free tier spins down after 15 minutes of inactivity. You can keep it alive by setting up an external uptime monitor (e.g. [UptimeRobot](https://uptimerobot.com/)) to ping your `/keep_alive` endpoint every 5 minutes:
```
https://your-app-name.onrender.com/keep_alive
```

</details>

<details>
<summary><strong>ImportError: cannot import name 'ReactionTypeEmoji'</strong></summary>

Your installed version of `python-telegram-bot` is older than `20.8`. Run:
```bash
pip install -r requirements.txt --upgrade
```

</details>

---

## 📄 License

MIT — see [LICENSE](LICENSE).