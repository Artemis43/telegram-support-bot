# Telegram Support bot
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
![](https://komarev.com/ghpvc/?username=Artemis43&color=blue&label=View+Count)
## Introduction
Telegram is a vast platform with many users and lots of groups and channels.
Many a times, admins would want to manage the issues of users. 
This Telegram bot enables you to manage these issues in a more convenient, aesthetic and organized way.
- In the current version, bot supports text messages, files, photos and videos.

## Usage

| ENV Variables                      | Description                     |
|:---------------------------------- |:--------------------------------|
|`TELEGRAM_BOT_TOKEN`                | To connect your telegram bot    |
|`TELEGRAM_GROUP_ID`                 | To receive issues in the group  |
|`TELEGRAM_ADMINS`                   | Only admins can reply to issues |
|`PORT`                              | (Optional) default - 8443       |
|`WEBSITE_URL`                       | To set webhook                  |


To get started, you'll need a Telegram API Access Token, and you can get it here [@BotFather](https://t.me/botfather). Now, either define an env variable 'TELEGRAM_BOT_TOKEN' when deploying :
```
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
```
OR replace with your token :
```
TOKEN = "YOUR_BOT_TOKEN"
```

In order to manage the users, first create a (private) group in Telegram and then make the bot the group admin.
- Remember - The bot should have the right to create topics in the Group.
- Find your group ID - [Instructions](https://youtu.be/0XLcHfKjlA0?si=7GUUAewNQ6x0P6zF&t=90) and either define 'TELEGRAM_GROUP_ID' as an env variable :
```
GROUP_ID = int(os.getenv('TELEGRAM_GROUP_ID'))  # Ensure GROUP_ID is an integer
```
OR hard-code your group ID:
```
GROUP_ID = -1002234879626
```

Only the users who are mentioned as Admins can reply to users through the group. These users should be members of the group. Find their Chat ID - [Instructions](https://youtu.be/0XLcHfKjlA0?si=7GUUAewNQ6x0P6zF&t) and either define 'TELEGRAM_ADMINS' as an env variable :
```
ADMINS = list(map(int, os.getenv('TELEGRAM_ADMINS').split(',')))
```
OR hard-code your Admin IDs:
```
ADMINS = [ADMIN_ID1,ADMIN_ID2]
```

Define a port for the Web Service to run on. Else, it has a default port to 8443 (Optional)
```
PORT = int(os.getenv('PORT', 8443))
```

This bot uses Webhooks instead of Polling to avoid multiple instances and proper functioning of the bot. So, The Website URL is required to create a webhook for the bot. The bot automatically sets the webhook upon start.
```
WEBSITE_URL = os.getenv('WEBSITE_URL')
```

## Installation

Clone the repository:

```
git clone https://github.com/Artemis43/telegram-support-bot.git
cd telegram-support-bot-main
```

Install dependencies

```
pip install -r requirements.txt
```
Run
```
python main.py
```
## License

The project is available as open source under the terms of the [MIT License](https://opensource.org/licenses/MIT).