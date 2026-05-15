# LOLDiscordFriendTracker

A private Discord bot for tracking a League of Legends friend group with Riot API data.

The bot stores Riot match data locally, then builds daily, weekly, season, duo, flex-stack, live-game, streak, and grief-impact summaries from the cache. It is designed to avoid refetching the same matches and to stay friendly to Riot rate limits.

## What It Tracks

- Tracked players by Riot ID (`Name#TAG`)
- Ranked Solo/Duo, Flex, and ARAM win/loss/KDA summaries
- Daily windows using a 3:00 AM America/New_York reset
- Weekly and season-long leaderboards
- Current group win/loss streaks
- Recent cached games for any tracked player
- Group champion win rates and KDA
- Top same-team Solo/Duo pairs
- Top tracked 5-player Flex stacks
- Live games for tracked players
- Approximate MMR snapshots from ranked entries
- Grief Tracker analysis for recent Solo/Duo games

## Security Note

Do not hard-code Discord or Riot tokens in source files. This project now loads secrets from environment variables or a local `.env` file.

If real tokens were ever committed or shared, rotate them in the Discord Developer Portal and Riot Developer Portal.

## Setup

1. Install Python 3.11 or newer.

2. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

On Raspberry Pi OS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

3. Create a local environment file:

```powershell
Copy-Item .env.example .env
```

On Raspberry Pi OS/Linux:

```bash
cp .env.example .env
```

4. Edit `.env`:

```env
DISCORD_TOKEN=your_discord_bot_token_here
RIOT_API_KEY=RGAPI-your_riot_api_key_here
COMMAND_PREFIX=!
RIOT_REGION=americas
RIOT_PLATFORM=na1
TEST_CHANNEL_ID=
DAILY_RESET_HOUR=3
MATCH_FETCH_LIMIT=50
DEBUG_LOGGING=true
STORAGE_PRETTY_JSON=false
```

5. Run the bot:

```powershell
python bot.py
```

On Raspberry Pi OS/Linux:

```bash
source .venv/bin/activate
python bot.py
```

## Required Discord Settings

In the Discord Developer Portal:

- Enable the bot user.
- Enable the `MESSAGE CONTENT INTENT`.
- Invite the bot with permission to read messages and send messages in the target channel.

## Command Reference

### Player Management

`!addsummoner Name#TAG`

Adds or refreshes a tracked player. The bot verifies the Riot ID, stores the PUUID, and records ranked MMR snapshots when available.

`!playerlist`

Lists all tracked players.

`!removesummoner Name#TAG`

Removes a player from the tracked pool while keeping stored match payloads for cache reuse.

`!playerinfo Name#TAG`

Shows level, ranked Solo/Flex entries, recent KDA, top mastery champions, and top recent Solo/Duo champions.

### Updates And Health

`!updaterecords`

Fetches recent match IDs and missing match details for all tracked players.

`!updateseason`

Backfills matches since the configured season start. This can take a while and is safe to re-run because stored match IDs are reused.

`!status`

Shows cache health: player count, stored matches, indexed references, missing match details, duplicate index references, last update time, and top stored queues.

`!repaircache [true]`

Deduplicates player match indexes and removes stale indexes for deleted players. Passing `true` also removes index references where the match payload is missing. It rewrites `league.json`, which also compacts the file when `STORAGE_PRETTY_JSON=false`.

`!commands`

Shows the command list in Discord.

### Leaderboards

`!dailyrecords`

Shows group records for the active 3:00 AM to 3:00 AM local day.

`!weeklyrecords`

Shows group records for the last 7-day window.

`!seasonrecords`

Shows group records since season start.

`!dashboard`

Shows an interactive Discord view with Daily, Weekly, Season, and Refresh buttons.

### Analytics

`!recentgames Name#TAG [count]`

Shows the player's most recent cached games with queue, champion, result, KDA, CS, and duration. The count defaults to 10 and caps at 20.

`!streaks [all|solo|flex|aram]`

Shows each tracked player's current cached win/loss streak.

`!topduos [min_games]`

Shows the best same-team tracked Solo/Duo pairs. The minimum game threshold defaults to 3.

`!topchamps [all|solo|flex|aram] [min_games]`

Shows group champion performance from cached tracked-player games. Example: `!topchamps solo 5`.

`!topflexstacks`

Shows the best tracked 5-player Flex stacks since season start.

`!grieftracker Name#TAG`

Analyzes recent cached Solo/Duo games for teammate-impact patterns.

`!debugrecentqueues`

Lists queue IDs currently stored in the cache.

## Data Files

`league.json` is the local cache. It stores:

- `players`: Riot IDs, PUUIDs, profile metadata, and MMR history
- `matches`: full Match-V5 payloads keyed by match ID
- `player_match_index`: player to match-ID references
- `last_update_utc`: last successful update timestamp

The file can become large. That is expected because cached match payloads prevent duplicate API calls and enable analytics without refetching.

## Project Structure

```text
bot.py                  Discord commands and scheduled updates
riot.py                 Riot API client and retry logic
records.py              Time windows, queue names, and stat helpers
analytics.py            Duo analytics
grieftracker.py         Grief Tracker scoring
mmrupdate.py            Ranked-entry to MMR snapshots
live.py                 Live-game grouping and formatting
storage.py              Cache loading, saving, health, and player lookup helpers
rank_baselines.py       Rank-normalized baselines for grief scoring
tests/test_core_logic.py Lightweight tests for pure logic
```

## Maintenance Workflow

Use this when setting up or repairing the cache:

1. `!addsummoner Name#TAG` for each player.
2. `!updateseason` once to backfill from season start.
3. `!status` to confirm the cache is healthy.
4. `!dailyrecords`, `!weeklyrecords`, or `!dashboard` for normal use.

The bot also runs an hourly background update after it connects.

## Raspberry Pi 4 Notes

This project is still JSON-backed, so storage choices matter on a Pi 4.

- Keep `STORAGE_PRETTY_JSON=false` unless you are actively inspecting `league.json`. Compact JSON reduces disk writes and file size.
- Use `!repaircache` after major player-list changes to deduplicate indexes and compact the cache.
- Keep `MATCH_FETCH_LIMIT` conservative, such as `25` or `50`, if your Riot key is rate-limited or the Pi is on slow storage.
- Prefer `!dashboard` for quick checks and the specific records commands when you need full output.
- Live-game checks are cached briefly and run off the Discord event loop so commands stay responsive.

### Optional systemd Service

Create `/etc/systemd/system/lol-discord-friend-tracker.service`:

```ini
[Unit]
Description=LOL Discord Friend Tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/pi/LOLDiscordFriendTracker
EnvironmentFile=/home/pi/LOLDiscordFriendTracker/.env
ExecStart=/home/pi/LOLDiscordFriendTracker/.venv/bin/python /home/pi/LOLDiscordFriendTracker/bot.py
Restart=always
RestartSec=15
User=pi

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lol-discord-friend-tracker
sudo journalctl -u lol-discord-friend-tracker -f
```

## Local Verification

Syntax check:

```powershell
python -m py_compile bot.py riot.py records.py analytics.py storage.py grieftracker.py mmrupdate.py live.py rank_baselines.py config.py
```

Unit tests:

```powershell
python -m unittest discover -s tests
```

Import command registry without starting the bot:

```powershell
@'
import bot
print("\n".join(sorted(c.name for c in bot.bot.commands)))
'@ | python -
```

## Troubleshooting

`Missing required environment setting(s)`

Create `.env` from `.env.example` and fill in `DISCORD_TOKEN` and `RIOT_API_KEY`.

`ZoneInfoNotFoundError: America/New_York`

Install dependencies from `requirements.txt`. Windows needs the `tzdata` package for IANA time zones.

`HTTP 401` or `HTTP 403` from Riot

Check that `RIOT_API_KEY` is current. Development keys expire regularly.

`HTTP 429` from Riot

The Riot client retries rate limits automatically. If this happens often, lower `MATCH_FETCH_LIMIT` in `.env`.

No games show up in leaderboards

Run `!updaterecords` or `!updateseason`, then check `!status` for missing match details.
