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
- Supports an optional `cookies.txt` file for logged-in downloads and anti-bot pages

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
OPENAI_BASE_URL=https://your-gateway.example/v1
OPENAI_API_KEY=your_gateway_api_key
OPENAI_MODEL=model-name-from-the-gateway
MAX_DOWNLOAD_MB=49
CLEANUP_MAX_AGE_HOURS=24
CHAT_HISTORY_LIMIT=12
AI_TIMEOUT_SECONDS=45
GROQ_TIMEOUT_SECONDS=45
REQUIRED_CHANNEL=@your_channel
REQUIRED_CHANNEL_URL=https://t.me/your_channel
SUBSCRIPTION_CACHE_SECONDS=300
CONCURRENT_UPDATES=8
MAX_CONCURRENT_DOWNLOADS=3
```

If `AI_PROVIDER=auto`, the bot prefers an OpenAI-compatible gateway when
`OPENAI_BASE_URL` and `OPENAI_API_KEY` are both set, then Gemini, then xAI Grok, then Groq.

## OpenAI-compatible gateways

Besides the three built-in providers, the bot can talk to any endpoint that
implements the OpenAI `chat/completions` API — model aggregators, self-hosted
proxies, or OpenAI itself. Set `AI_PROVIDER=openai` (or leave `auto`) and fill in:

- `OPENAI_BASE_URL` — either the API root (`https://host/v1`) or the full method
  URL; the bot appends `/chat/completions` when it is missing.
- `OPENAI_API_KEY` — sent as `Authorization: Bearer …`.
- `OPENAI_MODEL` — required, with no default: a gateway serves many models, so
  there is nothing sensible to guess.

Reaching a provider is not a given from every host — some block whole regions
with a bare `403`. Check from the machine that will run the bot before debugging
the key:

```bash
cd ~/skachat && .venv/bin/python check_ai.py
```

It prints the resolved provider, URL and model, makes one real request, and
never prints the key itself.

## Subscription gate

If `REQUIRED_CHANNEL` is set, the bot only replies to users who are subscribed to that
channel. Before it works:

1. Add the bot as an **administrator** of the channel (any admin right is enough — bots
   cannot be plain members of channels, only admins, and `getChatMember` requires that).
2. Set `REQUIRED_CHANNEL` to the channel's `@username` and `REQUIRED_CHANNEL_URL` to its
   public link, so the "Open channel" button works.

On startup the bot checks its own admin status in the channel and logs a warning if it
isn't an admin — check `docker logs` if subscribers report the gate never passes.
Verification results are cached per user for `SUBSCRIPTION_CACHE_SECONDS` to avoid hitting
Telegram's API on every message. Leave `REQUIRED_CHANNEL` empty to disable the gate.

## Reliability controls

- `CONCURRENT_UPDATES` lets the bot process multiple users' messages in parallel instead of
  queuing everyone behind one slow request (download or AI call).
- `MAX_CONCURRENT_DOWNLOADS` caps how many video downloads can run at the same time across
  all users, so a burst of requests can't exhaust disk/CPU on the server. Extra requests are
  queued with a "server is busy" notice instead of failing.
- Each user can only have one download in flight at a time.

## Personality configuration

The bot reads its behavior from one place only: `system_prompt.txt`. The repository ships `system_prompt.example.txt` as a starting point — copy it and make it yours.

On the server, edit:

```bash
nano /opt/skachat/system_prompt.txt
```

Then reload the bot:

```bash
bash /opt/skachat/deploy.sh
```

## Cookies

Two different failures are both fixed by the same file:

- Instagram returns a login or rate-limit error.
- A site answers `200` with an anti-bot page instead of the video, and yt-dlp
  reports `Unexpected response from webpage request`. TikTok does this to
  datacenter IP ranges — the link itself is fine and opens from a normal
  browser. Upgrading yt-dlp does not help; the request never reaches the
  extractor's happy path.

Place a cookies file next to `bot.py`:

```bash
cookies.txt
```

One file covers every site: the Netscape format stores cookies per domain, so
exporting from a logged-in browser session gives Instagram and TikTok cookies
at once. `COOKIES_FILE` overrides the location. The older
`instagram_cookies.txt` name still works and is used when `cookies.txt` is
absent, so existing deployments keep running.

The file must be in Netscape/Mozilla cookies format. The yt-dlp FAQ notes that you can pass a cookies file with `--cookies`, and that the file must be in that format with a `# HTTP Cookie File` or `# Netscape HTTP Cookie File` header: [yt-dlp FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ).

After uploading or updating the cookies file, reload the bot:

```bash
bash /opt/skachat/deploy.sh
```

4. Create your bot personality file from the example:

```bash
cp system_prompt.example.txt system_prompt.txt
```

`system_prompt.txt` is intentionally untracked, so your personal version stays out of git. Edit it to change how the bot talks.

5. Run the bot:

```bash
python bot.py
```

## Autonomous 24/7 hosting

Running on Russian shared hosting is a separate story — no Docker, no systemd,
no SSH cron, and the network to Telegram is filtered. That setup lives in
[README-server.md](README-server.md) together with `run_bot.py`, `net_patch.py`,
`check_ai.py` and `skachatctl.sh`. `bot.py` itself stays platform-agnostic.

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
