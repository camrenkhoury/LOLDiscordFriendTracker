# LOLDiscordFriendTracker (AI ASSISTED DEVELOPMENT)

League of Legends Group Tracker (Discord Bot)

A Discord bot that tracks League of Legends statistics for a private group using the Riot API.
The bot focuses on daily records, season-long stats, and group analytics, with heavy use of local caching to stay within Riot API rate limits.

------------------------------------------------------------
FEATURES
------------------------------------------------------------

PLAYER MANAGEMENT

!addplayer Name#TAG
Adds a Riot ID to the tracked player pool.

!playerlist
Displays all tracked players.

------------------------------------------------------------
PLAYER LOOKUP
------------------------------------------------------------

!playerinfo Name#TAG
Displays:
- Account level
- Ranked Solo/Duo and Flex ranks
- Win–loss record and win rate
- Recent KDA (last N matches)
- Most-played Solo/Duo champions

Note:
Champion mastery is not available due to Riot API restrictions.

------------------------------------------------------------
DAILY RECORDS (3:00 AM RESET)
------------------------------------------------------------

The “day” runs from 3:00 AM → 3:00 AM local time (America/New_York).

!updaterecords
Fetches and stores new matches since the last update.

!dailyrecords
Displays a formatted table with:
- Solo/Duo, Flex, and ARAM win–loss records
- KDA per queue
- Players sorted by games played
- A TOTAL row aggregating all players

Example format:

Player                     | Solo WL KDA   | Flex WL KDA   | ARAM WL KDA
--------------------------------------------------------------------------
PlayerA#TAG                | 3-2   2.45    | 0-0   0.00    | 0-0   0.00
PlayerB#TAG                | 1-1   1.80    | 0-0   0.00    | 0-0   0.00
--------------------------------------------------------------------------
TOTAL                      | 4-3   2.10    | 0-0   0.00    | 0-0   0.00

------------------------------------------------------------
SEASON TRACKING
------------------------------------------------------------

The season start date is fixed (for example: January 8, 3:00 AM local).

!updateseason performs a season-long backfill:
- Fetches historical matches since season start
- Stops automatically once the boundary is reached
- Safe to re-run (no duplicate matches stored)
- Uses pagination and incremental saves

------------------------------------------------------------
GROUP ANALYTICS
------------------------------------------------------------

DUO ANALYTICS

!topduos [min_games]
- Displays the top-performing duo pairs in Solo/Duo
- Sorted by win rate, then games played
- Default minimum games: 3
- Fallback enabled if not enough duos meet the threshold

------------------------------------------------------------

FLEX STACK ANALYTICS

!topflexstacks
- Displays top 5-player Flex stacks since season start
- Only matches where all five players are tracked
- Shows win–loss record and win rate
- Enforces minimum game thresholds
- Displays total number of unique stack combinations

------------------------------------------------------------
DATA & CACHING
------------------------------------------------------------

All data is cached locally in a single file:

league.json

Stored data includes:
- Tracked players (Riot ID → PUUID)
- Full Match-V5 payloads
- Player → match index
- Last update timestamps

Why caching matters:
- Prevents duplicate API calls
- Respects Riot rate limits
- Enables large-scale analytics without refetching data
- Large file sizes (1M+ lines) are expected over time

------------------------------------------------------------
RATE LIMITING & RELIABILITY
------------------------------------------------------------

Riot API limits (per routing region):
- ~20 requests per second
- ~100 requests per 2 minutes

The bot:
- Uses incremental updates
- Handles 429 responses safely
- Defers processing of stored matches when rate-limited
- Uses a global update lock to prevent overlapping updates

------------------------------------------------------------
PROJECT STRUCTURE (RECOMMENDED)
------------------------------------------------------------

lolbot/
├── bot.py              # Discord command definitions
├── riot.py             # Riot API access
├── records.py          # Time windows & stat helpers
├── analytics.py        # Group analytics (duos, stacks, etc.)
├── storage.py          # JSON load/save helpers
├── config.py           # Tokens & configuration
├── league.json         # Cached data (generated)
└── README.txt

------------------------------------------------------------
SETUP
------------------------------------------------------------

Create config.py:

DISCORD_TOKEN = "YOUR_DISCORD_BOT_TOKEN"
RIOT_API_KEY = "YOUR_RIOT_API_KEY"
REGION = "americas"
PLATFORM = "na1"
COMMAND_PREFIX = "!"

------------------------------------------------------------

Install dependencies:

pip install discord.py requests

------------------------------------------------------------

Run the bot:

python bot.py

------------------------------------------------------------
IMPORTANT NOTES
------------------------------------------------------------

- Champion mastery endpoints are restricted and may return 403 responses.
- Third-party stat sites may rely on non-public or privileged Riot endpoints.
- All analytics are computed from locally cached match data, not live lookups.

------------------------------------------------------------
FUTURE IDEAS
------------------------------------------------------------

- Best teammate per player
- Performance after midnight
- Tilt detection (post-loss stats)
- Champion synergy metrics
- Weekly summaries
- Database backend (SQLite/Postgres) instead of JSON

------------------------------------------------------------

Optional extensions:
- Formal schema for league.json
- Performance optimizations for large cache files
- Migration from JSON storage to a database
- Detailed documentation for analytics modules
