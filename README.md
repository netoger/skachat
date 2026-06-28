# Telegram bot for downloading videos and chat

This bot accepts links to TikTok, Instagram, and YouTube videos, sends the downloaded file back in Telegram, and also works as a chat assistant through Gemini, Groq, or xAI Grok.

Use it only for videos you own or have permission to download, and make sure your use complies with platform rules and local law.

## Important security note

If you ever shared your bot token in chat, screenshots, or code, revoke it in `@BotFather` and generate a new one before deploying.

## Features

- Accepts a direct link in chat
- Replies to regular text messages as an AI assistant
- Extracts metadata before downloading
- Rejects overly large files before or after download
- Sends the result back as a document
- Stores temporary files in `downloads/`
- Uses a single ready-made media file to avoid requiring `ffmpeg` in the basic setup
- Removes temporary local files after processing and cleans up stale leftovers on startup
- Shows a `Перезапустить` button that clears only the current user's dialog state
- Can use Gemini, Groq, or xAI Grok as the chat provider
- Loads the bot personality from one prompt file
- Supports an optional `instagram_cookies.txt` file for logged-in Instagram downloads

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and set your bot token and one AI provider:

```bash
BOT_TOKEN=your_telegram_bot_token
AI_PROVIDER=auto
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.5-flash
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=openai/gpt-oss-20b
XAI_API_KEY=your_xai_api_key
XAI_MODEL=grok-4.3
MAX_DOWNLOAD_MB=49
CLEANUP_MAX_AGE_HOURS=24
CHAT_HISTORY_LIMIT=12
AI_TIMEOUT_SECONDS=45
GROQ_TIMEOUT_SECONDS=45
```

If `AI_PROVIDER=auto`, the bot prefers Gemini when `GEMINI_API_KEY` is set, then xAI Grok, then Groq.

## Personality configuration

The bot reads its behavior from one place only: `system_prompt.txt`.

On the server, edit:

```bash
nano /opt/skachat/system_prompt.txt
```

Then reload the bot:

```bash
bash /opt/skachat/deploy.sh
```

## Instagram cookies

If Instagram starts returning login or rate-limit errors, place a cookies file at:

```bash
/opt/skachat/instagram_cookies.txt
```

The file must be in Netscape/Mozilla cookies format. The yt-dlp FAQ notes that you can pass a cookies file with `--cookies`, and that the file must be in that format with a `# HTTP Cookie File` or `# Netscape HTTP Cookie File` header: [yt-dlp FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ).

After uploading or updating the cookies file, reload the bot:

```bash
bash /opt/skachat/deploy.sh
```

4. Run the bot:

```bash
python bot.py
```

## Autonomous 24/7 hosting

If you do not want to keep your own computer running, deploy the bot to a server or cloud container.

### Option 1: VPS or cloud VM

This is the simplest and most predictable option.

1. Copy the project to a Linux server.
2. Create `.env` with your bot token.
3. Build the image:

```bash
docker build -t skachat-bot .
```

4. Run the container:

```bash
docker run -d --name skachat-bot --restart unless-stopped --env-file .env skachat-bot
```

With `--restart unless-stopped`, Docker will start the bot automatically after server reboots.

### Option 2: Railway / Render / Fly.io

These platforms can run the same `Dockerfile`. Upload the repository, add `BOT_TOKEN` and `MAX_DOWNLOAD_MB` as environment variables, and set the start command to the default container command.

### Option 3: Without Docker

Run the bot on a VPS with `systemd` so it restarts automatically:

```ini
[Unit]
Description=Telegram downloader bot
After=network.target

[Service]
WorkingDirectory=/opt/skachat
EnvironmentFile=/opt/skachat/.env
ExecStart=/usr/bin/python3 /opt/skachat/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable it:

```bash
sudo systemctl enable --now skachat-bot
```

## Telegram bot token

Create a bot with `@BotFather`, then put the token into `.env`.

## Groq API key

Create a key in the Groq console and store it in `GROQ_API_KEY`.

## Notes

- The default size limit is kept conservative for Telegram Bot API uploads.
- Regular text messages are sent to Groq, while supported video links are handled as downloads.
- The `Перезапустить` button and `/reset` clear only one user's in-memory dialog history.
- Local temporary files are deleted after each request. If the bot crashes mid-download, stale leftovers older than `CLEANUP_MAX_AGE_HOURS` are removed on the next start or next request.
- Some videos may be unavailable because of platform restrictions, private access, region locks, or site changes.
- On YouTube, the bot prefers a ready-made file with audio included, which is simpler to run but may not always be the highest possible quality.
