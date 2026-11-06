# Telegram bot for downloading videos and chat

This bot accepts links to TikTok, Instagram, and YouTube videos, sends the downloaded file back in Telegram, and also works as a chat assistant through DeepSeek.

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
- Chats through DeepSeek (`deepseek-flash`, thinking mode off) with a per-user daily message limit
- Loads the bot personality from one prompt file
- Supports an optional `cookies.txt` file for logged-in downloads and anti-bot pages

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and set your bot token and DeepSeek key:

```bash
BOT_TOKEN=your_telegram_bot_token
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_THINKING=disabled
AI_MAX_TOKENS=1024
AI_DAILY_LIMIT=10
ADMIN_USER_IDS=
CHAT_HISTORY_LIMIT=10
AI_TIMEOUT_SECONDS=60
MAX_DOWNLOAD_MB=49
CLEANUP_MAX_AGE_HOURS=24
REQUIRED_CHANNEL=
REQUIRED_CHANNEL_URL=
SUBSCRIPTION_CACHE_SECONDS=0
CONCURRENT_UPDATES=8
MAX_CONCURRENT_DOWNLOADS=3
```

## DeepSeek

The chat uses the OpenAI-compatible DeepSeek API (`https://api.deepseek.com`).
Create a key at platform.deepseek.com, top up the balance, and put the key into
`DEEPSEEK_API_KEY`.

- `DEEPSEEK_MODEL` — `deepseek-flash` by default (the base model);
  `deepseek-v4-pro` is the larger one.
- `DEEPSEEK_THINKING` — `disabled` by default. Thinking mode is slower and
  bills the reasoning tokens, which a chat bot does not need.
- `AI_DAILY_LIMIT` — messages per user per day (Moscow time), `10` by default,
  `0` disables the limit. CGI counters live in `data/users/USER_ID.usage.json`
  (polling uses `data/ai_usage.json`) and survive
  restarts. A failed request does not count.
- `ADMIN_USER_IDS` — comma-separated Telegram user IDs without the limit.
- `AI_MAX_TOKENS` — cap on the answer length.

Reaching a provider is not a given from every host — some block whole regions
with a bare `403`. Check from the machine that will run the bot before debugging
the key:

```bash
cd ~/skachat && .venv/bin/python check_ai.py
```

For the current Timeweb deployment, run commands from
`~/togeeer/public_html/app`; `~/skachat` is the source clone and retained backup.
See [README-webhook.md](README-webhook.md) before deployment or configuration changes.

It prints the resolved provider, URL and model, makes one real request, and
never prints the key itself.

## Subscription gate

If `REQUIRED_CHANNELS` is set, the bot only replies to users who are subscribed to
**every** channel in the list. Channels are separated by commas; to add or remove one,
edit the line and restart the bot:

```bash
REQUIRED_CHANNELS=https://t.me/kursoviework, https://t.me/siskysindnysuiny
```

Each entry can be `https://t.me/name`, `t.me/name` or `@name`. A private channel is
written as its numeric id and invite link: `-1001234567890|https://t.me/+invite`.
Leave the value empty to disable the gate. The old `REQUIRED_CHANNEL` /
`REQUIRED_CHANNEL_URL` pair still works when `REQUIRED_CHANNELS` is not set.

The bot must be an **administrator** of every channel (any admin right is enough —
`getChatMember` requires it). On startup it checks this and logs an error per channel
it cannot check. A user who is missing some subscriptions gets buttons only for those
channels. If Telegram refuses the check for a channel (bot is not an admin, wrong name)
or the network fails, access stays closed until both subscriptions are confirmed.
This applies to administrators too; `ADMIN_USER_IDS` only removes the AI quota.
`/id` remains available to look up one's Telegram ID. By default verification runs
on every request (`SUBSCRIPTION_CACHE_SECONDS=0`); no old positive cache is used.

## Reliability controls

On Timeweb CGI, Telegram sends up to three requests concurrently. Each user has
an independent lock, history and quota file. A slow download does not hold other
users' state. Filesystem slots limit downloads across all CGI processes to
`MAX_CONCURRENT_DOWNLOADS`. Old dialogs and quota counts migrate automatically
from the previous files; keep those old files for users who have not returned yet.
During link resolution and download, one status message cycles through
`Загрузка`, `Загрузка.`, `Загрузка..`, `Загрузка...`, then back. The animation
stops before the final result or error; upload uses `Отправляю видео…`.

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

Public Instagram Reels use [VXInstagram](https://github.com/Lainmode/InstagramEmbed-vxinstagram)
Open Graph metadata and its MP4 endpoint, with the same embedding User-Agent
for both. The anonymous Instagram embed page is a fallback. This path does not
use yt-dlp or personal cookies. The supplied Reels sample downloaded completely
twice through the actual Timeweb CGI on 2026-11-06: 8,325,643-byte MP4,
in 1.54 and 1.33 seconds. Connection errors are retried twice; private videos are unsupported.
YouTube continues using yt-dlp >=2026.10.21. The supplied Shorts sample downloaded completely:
MP4, 13,370,661 bytes. User delivery through Telegram is a separate check.

### TikTok download without personal cookies

TikTok uses TikWM by default (`TIKTOK_DOWNLOADER=tikwm`). This path does not
read personal cookies or use `TIKTOK_PROXY`. The bot resolves the media URL
once and streams the MP4 with a size limit. Other platforms continue using yt-dlp.
On 2026-11-06 the new module downloaded a complete 2,823,850-byte MP4 from
Timeweb for the supplied public TikTok link. This was a standalone check,
not a deployment or a Telegram delivery check.

`TIKTOK_DOWNLOADER=yt-dlp` restores the old downloader. The experimental
`TIKTOK_DOWNLOADER=ssstik` uses the ssstik.io form: it returned a link during
the hosting check, but its media CDN did not deliver a confirmed file.
SaveFrom returned `invalid_request` without its browser-generated signature;
it is not integrated. Instagram uses its public embed page directly.

Offline checks: `python -m unittest test_external_download`.
These checks use synthetic HTTP responses and no credentials.

The historical shared-hosting daemon commands below and in README-server.md
conflict with the current host restrictions: do not start or restart persistent
processes there. Verify the supported hosting and runtime before deployment.

For platforms using yt-dlp, cookies can help with these failures:

- A site answers `200` with an anti-bot page instead of the video, and yt-dlp
  reports `Unexpected response from webpage request`. TikTok does this to
  datacenter IP ranges — the link itself is fine and opens from a normal
  browser. Upgrading yt-dlp does not help; the request never reaches the
  extractor's happy path.

Place a cookies file next to `bot.py`:

```bash
cookies.txt
```

The Netscape format stores cookies per domain. TikWM and public Instagram
extraction do not read personal cookies. For other yt-dlp paths,
`COOKIES_FILE` overrides the location. The older
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

## DeepSeek API key

Create a key at platform.deepseek.com and store it in `DEEPSEEK_API_KEY`.

## Notes

- The default size limit is kept conservative for Telegram Bot API uploads.
- Regular text messages are sent to DeepSeek, while supported video links are handled as downloads.
- The `Перезапустить` button and `/reset` clear only one user's in-memory dialog history.
- Local temporary files are deleted after each request. If the bot crashes mid-download, stale leftovers older than `CLEANUP_MAX_AGE_HOURS` are removed on the next start or next request.
- Some videos may be unavailable because of platform restrictions, private access, region locks, or site changes.
- On YouTube, the bot prefers a ready-made file with audio included, which is simpler to run but may not always be the highest possible quality.
