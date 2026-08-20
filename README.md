# Kevin

Kevin is a modular, all-in-one Discord bot with a lightweight Telegram companion.
Discord includes moderation, community, music, games, and utilities; Telegram focuses
on OpenAI conversation and web-backed answers.

## Feature map

- **Moderation:** kick, ban/unban, softban/massban, timeout, purge, slowmode, channel
  locks, nicknames, role management, voice moderation, warnings, and case history.
- **Automod:** spam, invite, link, excessive-caps, mass-mention, and custom blocked-term
  filters, with moderator exemptions and action logging.
- **Community:** welcomes, goodbyes, autoroles, polls, reminders,
  member/server info, timestamps, image-to-GIF conversion, and a safe calculator.
- **Economy:** Kash wallet and bank balances, daily rewards, jobs, payments, admin
  grants, robbery, a collectible shop, inventory, coin flips, dice, interactive blackjack
  (`/blackjack` or `/bj`), interactive craps (`/craps`) with pass/don't pass lines and true
  odds, and adventure-themed jungle slots.
- **Music:** searches and URLs through yt-dlp, per-server queues, pause/resume, skip,
  loop, shuffle, volume, removal, and now-playing information.
- **Support:** private ticket channels, persistent open/close buttons, staff access
  controls, and text transcript export.
- **Engagement:** Twitch, YouTube, and TikTok live alerts, TikTok post alerts,
  button-entry giveaways, reaction roles, starboard highlights, suggestions, reusable
  tags, and AFK notices.
- **Fun:** interactive trivia with stats and leaderboards, 8-ball, dice notation,
  choices, rock-paper-scissors, jokes, compatibility, ratings, mock text, and social actions.
- **AI mentions and memory:** ping K for a short OpenAI answer with optional web search,
  speaker-aware recent channel context, and durable per-member Mem0 memory.

Every command is available as a slash command. Most also support the configurable text
prefix (default: `k`). Both `k help` and `khelp` work. Run `/help` after inviting K
for the live command list.

## Requirements

- Python 3.11 or newer (3.12 recommended)
- FFmpeg available on `PATH` for music
- A Discord application and bot token
- A Telegram bot token from [@BotFather](https://t.me/BotFather) for the Telegram bot
- The **Server Members Intent** and **Message Content Intent** enabled in the Discord
  developer portal

Discord.py's `voice` extra installs both PyNaCl and `davey`, which are required by the
current Discord voice protocol.

## Quick start

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications),
   add a bot, and copy its token.
2. On the bot page, enable **Server Members Intent** and **Message Content Intent**.
3. Copy the environment template and insert the token:

   ```bash
   cp .env.example .env
   ```

4. Install and run with [uv](https://docs.astral.sh/uv/):

   ```bash
   uv sync --extra dev
   uv run k
   ```

   Or use a regular virtual environment:

   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -e '.[dev]'
   python -m kevin
   ```

5. Invite K using the OAuth2 URL Generator with the `bot` and
   `applications.commands` scopes. Start with Administrator while testing, then reduce
   permissions to those your enabled modules need. K can also generate its own
   permission-aware link with `/invite`.

## Telegram AI companion

The Telegram bot shares Kevin's OpenAI prompt, model setting, and automatic web-search
behavior. It searches when useful or explicitly requested and includes clickable source
citations when available. It does not port Discord-only moderation or music commands. In
a private chat, send Kevin any text. In a group, mention
`@YourKevinBot` or reply to one of Kevin's messages. Kevin keeps the 20 most recent
conversation turns per user and chat while the process is running; `/reset` clears them.

1. Message [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the bot token.
2. Add the token to `.env`:

   ```text
   TELEGRAM_BOT_TOKEN=your-telegram-token
   ```

3. To keep the bot private, start once with an empty allowlist, message Kevin `/id`,
   then add the returned numeric ID to `.env` and restart. Multiple IDs are
   comma-separated; leaving it empty allows everyone:

   ```text
   TELEGRAM_ALLOWED_USER_IDS=123456789
   ```

4. Start only Telegram:

   ```bash
   uv run kevin-telegram
   ```

   Or run Discord and Telegram together in one process:

   ```bash
   uv run kevin-both
   ```

The original `uv run k` and `uv run kevin` commands still start only Discord. Telegram
uses long polling, so only one running Kevin process may use a given Telegram token.

For fast command updates during development, set `KEVIN_TEST_GUILD_ID` in `.env` to your
test server ID. Without it, commands sync globally and Discord may take time to display
changes.

### Streaming presence

Set the presence text and a Twitch or YouTube URL in `.env`, then restart K:

```text
KEVIN_STATUS=with the community!
KEVIN_STREAM_URL=https://twitch.tv/yourchannel
```

When `KEVIN_STREAM_URL` is set, Discord shows K with the purple **Streaming** activity
and a **Watch** button that opens that URL. This setting does not need Twitch API
credentials; `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` are only for go-live alerts.

## AI mention replies

Put an OpenAI API key in `.env`, then restart K:

```text
OPENAI_API_KEY=your-key-here
OPENAI_MODEL=gpt-5.6-luna
MEM0_LLM_MODEL=gpt-4.1-mini
MEM0_EMBEDDING_MODEL=text-embedding-3-small
```

Anyone in a server can then ask a question by pinging the bot, for example
`@K what's happening with the weather tomorrow?`. Reply directly to one of K's messages
to ask a follow-up without pinging it again. K uses the Responses API and can search the
web when useful. Replies are intentionally short and include clickable source links when
web search supplies citations. At most three OpenAI requests run at once to keep
shared-key usage under control. The key stays server-side; never paste it into Discord
or Telegram, and never commit `.env`.

### Discord AI memory

AI memory is enabled by default. K observes messages in server channels it can view even
when nobody tags it, including channels exposed through a Member role instead of
`@everyone`. It keeps at most 200 recent messages per channel in local SQLite and sends
only the latest 24 from the current channel when someone talks to it. Every context item
carries the author's immutable Discord user ID and current display name, so two people in
the same conversation stay distinct. Discord reply relationships are retained too, so K
can identify the exact stored message someone replied to instead of guessing. Messages
that look like credentials, email
addresses, or long account/card numbers are not stored; other bots and DMs are excluded.
K's own recent answers are retained with an explicit assistant label so later questions
can refer back to the conversation. Because this includes restricted server channels K
can access, only grant K access where this behavior is appropriate.

Durable personalization uses the self-hosted Mem0 Python library with an on-disk Qdrant
store under `data/mem0` (change it with `MEM0_PATH`). Mem0 is scoped to one Discord user
inside one server, so the same person has separate memory in different servers and no two
members share a profile. It stores evolving facts rather than a fixed number of chat
messages: relevant facts are added, merged, updated, or removed over time. Server-channel
self-disclosures that look like memory candidates are condensed shortly after they are
observed, and `/memory show` flushes any pending observations before reading them. K uses
`MEM0_LLM_MODEL` for condensation and `MEM0_EMBEDDING_MODEL` for retrieval. These are
additional OpenAI API calls, but no hosted Mem0 account or `MEM0_API_KEY` is needed.

K asks Mem0 to retain only explicit, non-sensitive self-disclosures such as preferences,
hobbies, pets, or recurring projects. It does not intentionally keep health, financial,
religious, political, sexual, contact, precise-location, credential, unique-identifier,
or alleged-wrongdoing details. Retrieved memories pass a second sensitive-data filter
before reaching the answer prompt. Memories and observations never cross server
boundaries. Mem0 anonymous telemetry is disabled by default; `MEM0_TELEMETRY=false` is
also included in `.env.example`.

Members control their own memory:

```text
/memory show         Show your Mem0 memories privately
/memory forget       Erase current notes and observations, but keep memory on
/memory off          Erase your data and opt out in this server
/memory on           Opt back in, starting fresh
```

Members with **Manage Server** can use `/memory server enabled:false` to disable memory
and erase all AI notes and observations for the server. Re-enable it with
`/memory server enabled:true`. Deleting a Discord message also removes its stored recent
observation. As with any AI feature, tell members that recent chat from the current
channel may be sent to OpenAI as context when Kevin is invoked.

## First server setup

Recommended setup commands:

```text
/config logs #mod-log
/config welcome #welcome Welcome {user} to {server}! Member #{count}.
/config goodbye #goodbye
/config autorole @Members
/automod enabled true
/automod filter spam true
/automod filter invites true
/ticket panel
/streamalert add twitch.tv/yourstreamer #stream-alerts @Live
/youtubealert add creator:@yourchannel destination:#stream-alerts role:@Live
/tiktokalert add creator:@yourcreator destination:#social-alerts live:true posts:true
```

Use `/config` and `/automod` without subcommands to inspect current settings. The bot's
role must be above any role it assigns or moderates.

## Stream alerts

Twitch alerts require a Twitch developer application. Create one in the
[Twitch developer console](https://dev.twitch.tv/console), then put its credentials in
`.env` as `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`. Restart K and configure alerts:

```text
/streamalert add streamer:yourstreamer channel:#stream-alerts role:@Live
/streamalert list
/streamalert remove streamer:yourstreamer
```

K checks Twitch once per minute and remembers the stream ID, so restarts and repeated
checks do not post duplicate alerts. A custom message can use `{streamer}`, `{title}`,
`{game}`, `{url}`, and `{role}`.

Trivia needs no API key. Use `/trivia play`, `/trivia stats`, and
`/trivia leaderboard`; category and difficulty filters are optional.

### YouTube alerts

Enable YouTube Data API v3 in Google Cloud, create an API key, and set
`YOUTUBE_API_KEY` in `.env`. After restarting K:

```text
/youtubealert add creator:@yourchannel destination:#stream-alerts role:@Live
/youtubealert list
/youtubealert remove creator:@yourchannel
```

K checks the channel feed every three minutes and batches video status lookups to keep
Data API quota usage low. Alerts are sent only while YouTube reports the video as live.

### TikTok alerts

```text
/tiktokalert add creator:@yourcreator destination:#social-alerts role:@Live live:true posts:true
/tiktokalert list
/tiktokalert remove creator:@yourcreator
```

Use `live:false` or `posts:false` when only one alert type is wanted. The first post
check saves the creator's newest post as a baseline and does not announce old content.
TikTok does not provide a general API for arbitrary creator live/post monitoring, so
these checks use yt-dlp against public pages and are best-effort. Private, region-locked,
age-gated, or bot-protected accounts may not work; `YTDLP_COOKIE_FILE` can help where an
authenticated session is required.

## Music notes

Music playback needs FFmpeg. On common platforms:

```bash
brew install ffmpeg                 # macOS
sudo apt install ffmpeg             # Debian/Ubuntu
```

Playback also needs the Opus library used by Discord voice. FFmpeg normally installs
it as a dependency. K searches common Homebrew and system locations automatically; set
`OPUS_LIBRARY` to the full library path if it is installed somewhere else.

Some media providers block datacenter IPs or require authentication. K supports a
Netscape-format cookie file through `YTDLP_COOKIE_FILE`; protect that file like a
password. Respect source terms and copyright rules.

## Data and operations

K stores general state in `data/kevin.sqlite3` and durable Mem0 state in `data/mem0` by
default. SQLite WAL mode is enabled. Back up the database file and its `-wal` companion
while the bot is running, or stop the bot before copying the main file. Stop the bot
before copying the whole Mem0 directory so its Qdrant store is consistent. Change the
locations with `KEVIN_DATABASE` and `MEM0_PATH`.

Never commit `.env`, cookies, the SQLite database, or a Discord token. If a token is
exposed, regenerate it immediately in the developer portal.

Useful checks:

```bash
uv run ruff check .
uv run pytest
uv run python -m compileall -q kevin
```

## Automatic VPS deployment

Every push to `main` runs linting, the test suite, and a compile check in GitHub
Actions. If all checks pass, the exact tested commit is deployed to the VPS and the
`kevin.service` user service is restarted. Deployments are serialized, so two quick
pushes cannot update the bot at the same time. The workflow can also be run manually
from the repository's **Actions** tab.

The VPS keeps runtime state outside Git: `.env`, `data/`, `.venv/`, `bin/`, and
`vendor/` are preserved during deployment. Before each restart, the deploy script uses
SQLite's online backup API to save a consistent database copy under
`data/backups/`; the ten newest backups are retained.
Those automatic backups cover `kevin.sqlite3`; `data/mem0` is preserved during deploys
but should be backed up separately while the bot is stopped.

The workflow expects these GitHub Actions secrets:

```text
VPS_HOST          VPS IP address or hostname
VPS_USER          SSH account that owns the user service
VPS_SSH_KEY       Dedicated private SSH key for deployments
VPS_KNOWN_HOSTS   Pinned SSH host-key entry for the VPS
```

The service is installed at `~/.config/systemd/user/kevin.service` and runs the
Discord and Telegram bots together from `~/apps/kevin`. Useful VPS checks are:

```bash
systemctl --user status kevin.service
journalctl --user -u kevin.service -f
```

## Project layout

```text
kevin/
  bot.py            lifecycle, command loading, logging helpers
  config.py         environment configuration
  database.py       SQLite schema and atomic operations
  cogs/             independent feature modules
  utils/            shared parsing and presentation helpers
tests/              fast unit and database tests
```

## Scaling beyond one process

This version is designed for one bot process and SQLite. For hundreds or thousands of
servers, move member/economy state to PostgreSQL, cooldowns and music coordination to
Redis, run Discord shards, and consider Lavalink for dedicated audio nodes. The cog and
service boundaries are intentionally structured so those replacements do not require a
command rewrite.
