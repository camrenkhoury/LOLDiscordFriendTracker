# bot.py
import asyncio
import time
from datetime import timedelta
from datetime import datetime

import discord
from discord.ext import commands, tasks

from grieftracker import evaluate_grieftracker

from mmrupdate import (
    update_player_mmr_from_profile,
    mmr_delta_since,
)


from mmrupdate import update_all_mmrs

from analytics import (
    build_match_card,
    champion_deep_dive,
    compare_players,
    compute_top_champions,
    compute_top_duos,
    group_awards,
    performance_score,
    sorted_player_matches,
    summarize_player,
    tracked_participants_for_match,
)
from storage import (
    cache_health,
    get_player_matches,
    load_data,
    now_utc_iso,
    parse_riot_id,
    repair_cache_indexes,
    resolve_player_key,
    save_data,
    upsert_player,
)
from records import (
    window_3am_to_3am_local,
    compute_wl_kda,
    compute_top_flex_stacks,
    SEASON_START_LOCAL,
    _game_start_local,
    _participant_for_puuid,
    ARAM_QUEUES,
    queue_name,
)
from live import get_live_games, format_live_games
from riot import (
    get_player_profile,
    get_match_ids_by_puuid,
    get_match,
    compute_recent_kda,
    solo_top_champs_wl,
    get_top_mastery_by_riot_id,
)

from config import (
    COMMAND_PREFIX,
    DISCORD_TOKEN,
    ENABLE_SCHEDULED_UPDATES,
    MATCH_FETCH_LIMIT,
    SCHEDULED_UPDATE_INTERVAL_MINUTES,
    TEST_CHANNEL_ID,
    validate_runtime_config,
)

DASHBOARD_CACHE = {
    "daily": None,
    "weekly": None,
    "season": None,
}
LIVE_CACHE = {"timestamp": 0.0, "data": []}

def tier_from_mmr(mmr: int | None) -> str | None:
    if mmr is None:
        return None

    if mmr >= 4000: return "CHALLENGER"
    if mmr >= 3600: return "GRANDMASTER"
    if mmr >= 3200: return "MASTER"
    if mmr >= 2800: return "DIAMOND"
    if mmr >= 2400: return "EMERALD"
    if mmr >= 2000: return "PLATINUM"
    if mmr >= 1600: return "GOLD"
    if mmr >= 1200: return "SILVER"
    if mmr >= 800:  return "BRONZE"
    return "IRON"

# --------------------
# Globals
# --------------------
update_lock = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

DISCORD_MESSAGE_LIMIT = 1900


async def send_long(ctx, content: str, limit: int = DISCORD_MESSAGE_LIMIT):
    """
    Sends content without silently truncating useful output.
    Splits on line boundaries when a command produces a long table.
    """
    if len(content) <= limit:
        await ctx.send(content)
        return

    chunk = []
    chunk_len = 0
    for line in content.splitlines():
        line_len = len(line) + 1
        if chunk and chunk_len + line_len > limit:
            await ctx.send("\n".join(chunk))
            chunk = []
            chunk_len = 0
        chunk.append(line)
        chunk_len += line_len

    if chunk:
        await ctx.send("\n".join(chunk))


def truncate_message(content: str, limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    if len(content) <= limit:
        return content
    suffix = "\n...(truncated; use the specific records command for the full table)"
    return content[: limit - len(suffix)] + suffix


def parse_riot_id_with_optional_count(args: str, default_count: int = 10, max_count: int = 20):
    args = (args or "").strip()
    if not args:
        raise ValueError("missing Riot ID")

    riot_id = args
    count = default_count
    head, sep, tail = args.rpartition(" ")
    if sep and tail.isdigit():
        riot_id = head.strip()
        count = int(tail)

    parse_riot_id(riot_id)
    return riot_id, max(1, min(max_count, count))


def parse_queue_filter(value: str | None):
    key = (value or "all").strip().lower()
    if key in {"all", "any", "*"}:
        return None, "All Queues"
    if key in {"solo", "soloduo", "solo/duo", "ranked", "420"}:
        return {420}, "Solo/Duo"
    if key in {"flex", "440"}:
        return {440}, "Flex"
    if key in {"aram", "450", "2400"}:
        return set(ARAM_QUEUES), "ARAM"
    raise ValueError("queue must be one of: all, solo, flex, aram")


def parse_queue_and_min_games(args: str, default_min_games: int = 3):
    parts = (args or "").split()
    queue = "all"
    min_games = default_min_games

    if parts:
        if parts[0].isdigit():
            min_games = int(parts[0])
        else:
            queue = parts[0]
            if len(parts) > 1 and parts[1].isdigit():
                min_games = int(parts[1])

    queue_ids, label = parse_queue_filter(queue)
    return queue_ids, label, max(1, min(100, min_games))


def parse_period(value: str | None):
    key = (value or "daily").strip().lower()
    if key not in {"daily", "weekly", "season"}:
        raise ValueError("period must be one of: daily, weekly, season")
    start, end = get_time_window(key)
    return key, start, end


def parse_two_riot_ids_and_queue(args: str):
    import re

    match = re.match(r"^\s*(.+?#\S+)\s+(.+?#\S+)(?:\s+(\S+))?\s*$", args or "")
    if not match:
        raise ValueError("Usage: `!compare Name#TAG OtherName#TAG [solo|flex|aram|all]`")
    first, second, queue = match.groups()
    queue_ids, label = parse_queue_filter(queue or "all")
    parse_riot_id(first)
    parse_riot_id(second)
    return first.strip(), second.strip(), queue_ids, label


def parse_champion_args(args: str):
    parts = (args or "").strip().split()
    if len(parts) < 2:
        raise ValueError("Usage: `!champion Name#TAG Champion [solo|flex|aram|all]`")

    queue = "all"
    if parts[-1].lower() in {"all", "solo", "soloduo", "solo/duo", "ranked", "flex", "aram", "420", "440", "450", "2400"}:
        queue = parts.pop()

    joined = " ".join(parts)
    hash_index = joined.find("#")
    if hash_index == -1:
        raise ValueError("Usage: `!champion Name#TAG Champion [solo|flex|aram|all]`")
    after_hash = joined.find(" ", hash_index)
    if after_hash == -1:
        raise ValueError("Provide a champion name after the Riot ID.")

    riot_id = joined[:after_hash].strip()
    champion = joined[after_hash + 1 :].strip()
    if not champion:
        raise ValueError("Provide a champion name after the Riot ID.")

    parse_riot_id(riot_id)
    queue_ids, label = parse_queue_filter(queue)
    return riot_id, champion, queue_ids, label


async def get_live_games_async(data, max_age_seconds: int = 60):
    now = time.monotonic()
    if now - LIVE_CACHE["timestamp"] <= max_age_seconds:
        return LIVE_CACHE["data"]

    live = await asyncio.to_thread(get_live_games, data)
    LIVE_CACHE["timestamp"] = now
    LIVE_CACHE["data"] = live
    return live


def fmt_pct(value):
    return "N/A" if value is None else f"{value:.1f}%"


def fmt_duration(minutes):
    minutes = int(round(minutes))
    return f"{minutes // 60}:{minutes % 60:02d}" if minutes >= 60 else f"{minutes}m"


def relative_time(dt):
    if not dt:
        return "unknown"
    delta = datetime.now(dt.tzinfo) - dt
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def short_list(items, limit=3):
    if not items:
        return "None"
    shown = list(items)[:limit]
    extra = len(items) - len(shown)
    suffix = f" +{extra}" if extra > 0 else ""
    return ", ".join(shown) + suffix


def player_champ_line(champs):
    if not champs:
        return "No cached champs"
    return ", ".join(f"{c['champion']} ({c['games']}g, {c['wr']:.0f}%)" for c in champs)


def wr_bar(wr: float):
    filled = min(10, max(0, int(round(wr / 10))))
    bar = "▓" * filled + "░" * (10 - filled)
    return f"{bar} {wr:5.1f}%"


def rank_icon(tier: str | None):
    if not tier:
        return "⚫"

    tier = tier.upper()
    return {
        "IRON": "⚪",
        "BRONZE": "🟤",
        "SILVER": "🔘",
        "GOLD": "🟡",
        "PLATINUM": "🔵",
        "EMERALD": "🟢",
        "DIAMOND": "🔷",
        "MASTER": "🟣",
        "GRANDMASTER": "🔴",
        "CHALLENGER": "⭐",
    }.get(tier, "⚫")

def resolve_solo_tier(p: dict) -> str | None:
    # Preferred explicit field
    tier = p.get("ranked_solo_tier")
    if isinstance(tier, str):
        return tier.upper()

    # Fallback: ranked_entries (already stored in JSON)
    for e in p.get("ranked_entries", []):
        if e.get("queueType") == "RANKED_SOLO_5x5":
            t = e.get("tier")
            if isinstance(t, str):
                return t.upper()

    return None

def get_time_window(mode: str):
    if mode == "daily":
        return window_3am_to_3am_local()
    if mode == "weekly":
        _, end = window_3am_to_3am_local()
        return end - timedelta(days=7), end
    # season
    _, end = window_3am_to_3am_local()
    return SEASON_START_LOCAL, end

def build_leaderboard_rows(data, start, end):
    rows = []

    for riot_id, p in data.get("players", {}).items():
        puuid = p.get("puuid")
        if not puuid:
            continue

        mids = data.get("player_match_index", {}).get(riot_id, [])
        matches = [data["matches"][m] for m in mids if m in data["matches"]]

        solo = compute_wl_kda(matches, puuid, 420, start, end)
        flex = compute_wl_kda(matches, puuid, 440, start, end)
        solo_mmr = p.get("mmr", {}).get("solo", {}).get("current", 0)


        aram = {"games": 0, "wins": 0, "losses": 0, "kda": 0.0}
        weight = 0
        for q in ARAM_QUEUES:
            r = compute_wl_kda(matches, puuid, q, start, end)
            aram["games"] += r["games"]
            aram["wins"] += r["wins"]
            aram["losses"] += r["losses"]
            aram["kda"] += r["kda"] * r["games"]
            weight += r["games"]

        aram["kda"] = aram["kda"] / weight if weight else 0.0

        total_games = solo["games"] + flex["games"] + aram["games"]
        if total_games == 0:
            continue

        wins = solo["wins"] + flex["wins"] + aram["wins"]
        wr = wins / total_games * 100

        mmr = (
            mmr_delta_since(p, "solo", start.isoformat())
            + mmr_delta_since(p, "flex", start.isoformat())
        )
        solo_mmr = p.get("mmr", {}).get("solo", {}).get("current")

        rows.append((
            solo_mmr,           # r[0] ← rank
            total_games,        # r[1]
            riot_id,            # r[2]
            solo,               # r[3]
            flex,               # r[4]
            aram,               # r[5]
            mmr,                # r[6] ← ΔMMR (unchanged)
            wr                  # r[7]
        ))


    # Sort: WR → MMR → games
    rows.sort(
        key=lambda r: (r[0] if r[0] is not None else -1, r[1]),
        reverse=True
    )
    return rows

def update_player_rank_from_profile(player, info):
    for entry in info.get("ranked_entries", []):
        if entry.get("queueType") == "RANKED_SOLO_5x5":
            player["ranked_solo_tier"] = entry.get("tier")
            player["ranked_solo_div"] = entry.get("rank")
            player["ranked_solo_lp"] = entry.get("leaguePoints")

def render_dashboard(rows, mode, start, end):
    NAME_W = 28
    WL_W = 7
    KDA_W = 5
    MMR_W = 6
    BAR_W = 16

    def pad(s, w):
        s = str(s)
        if len(s) > w:
            return s[:w-1] + "…"
        return s + " " * (w - len(s))

    def wl(x): return f"{x['wins']}-{x['losses']}"
    def kda(x): return f"{x['kda']:.2f}"

    header = (
        f"**{mode.capitalize()} Leaderboard** "
        f"({start:%b %d %I:%M%p} → {end:%b %d %I:%M%p} local)"
    )

    dash_len = (
        NAME_W
        + 3
        + (WL_W + 1 + KDA_W) * 3
        + 3
        + MMR_W
        + 2
        + BAR_W
    )

    lines = [
        header,
        "```",
        pad("Player", NAME_W) + " | "
        + pad("Solo", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("Flex", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("ARAM", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("ΔMMR", MMR_W) + "  WR%",
        "-" * dash_len,
    ]

    for solo_mmr, total_games, riot_id, solo, flex, aram, mmr, wr in rows:
        tier = tier_from_mmr(solo_mmr)
        icon = rank_icon(tier)


        lines.append(
            pad(f"{icon} {riot_id}", NAME_W) + " | "
            + pad(wl(solo), WL_W) + " " + pad(kda(solo), KDA_W) + " | "
            + pad(wl(flex), WL_W) + " " + pad(kda(flex), KDA_W) + " | "
            + pad(wl(aram), WL_W) + " " + pad(kda(aram), KDA_W) + " | "
            + pad(f"{mmr:+}", MMR_W) + "  "
            + wr_bar(wr)
        )

    lines.append("```")


    return "\n".join(lines)


# --------------------
# Core incremental update
# --------------------
async def incremental_update_core(ctx=None, notify_channel_id: int | None = None):
    if update_lock.locked():
        if ctx:
            await ctx.send("⚠️ Update already running.")
        return

    async with update_lock:
        if ctx:
            await ctx.send("⏳ Updating records (fetching new matches)...")

        data = load_data()
        if not data.get("players"):
            if ctx:
                await ctx.send("No players added yet.")
            return

        start_local, _ = window_3am_to_3am_local()
        cutoff_local = start_local - timedelta(hours=6)

        new_matches = 0
        filled_missing = 0
        errors = 0

        for riot_id, p in data["players"].items():
            puuid = p.get("puuid")
            if not puuid:
                errors += 1
                continue

            data["player_match_index"].setdefault(riot_id, [])
            known_ids = set(data["player_match_index"][riot_id])

            # --------------------
            # Fill missing match JSON
            # --------------------
            for mid in list(known_ids):
                if mid in data["matches"]:
                    continue

                try:
                    m = await asyncio.to_thread(get_match, mid)
                except Exception as e:
                    print("[fill missing] failed:", riot_id, mid, e)
                    errors += 1
                    continue

                t_local = _game_start_local(m)
                if t_local and t_local < cutoff_local:
                    continue

                data["matches"][mid] = m
                filled_missing += 1

            # --------------------
            # Fetch recent match IDs
            # --------------------
            try:
                match_ids = await asyncio.to_thread(
                    get_match_ids_by_puuid, puuid, MATCH_FETCH_LIMIT
                )
            except Exception as e:
                print("[match ids] failed:", riot_id, e)
                errors += 1
                continue

            for mid in match_ids:
                if mid in data["matches"]:
                    if mid not in known_ids:
                        data["player_match_index"][riot_id].append(mid)
                        known_ids.add(mid)
                    continue

                try:
                    m = await asyncio.to_thread(get_match, mid)
                except Exception as e:
                    print("[match detail] failed:", riot_id, mid, e)
                    errors += 1
                    continue

                t_local = _game_start_local(m)
                if t_local and t_local < cutoff_local:
                    break

                data["matches"][mid] = m
                data["player_match_index"][riot_id].append(mid)
                known_ids.add(mid)
                new_matches += 1

        # --------------------
        # Update MMR snapshots (CRITICAL FIX)
        # --------------------
        try:
            await asyncio.to_thread(update_all_mmrs, data)
        except Exception as e:
            print("[MMR update failed]", e)

        # --------------------
        # Finalize + save once
        # --------------------
        data["last_update_utc"] = now_utc_iso()
        save_data(data)

        if ctx:
            await ctx.send(
                f"✅ Update complete. New: **{new_matches}**, "
                f"Filled: **{filled_missing}**, Errors: **{errors}**."
            )

        if notify_channel_id:
            ch = bot.get_channel(notify_channel_id)
            if ch:
                await ch.send(
                    f"⏱️ Hourly update complete — "
                    f"new: {new_matches}, filled: {filled_missing}, errors: {errors}"
                )

        for k in DASHBOARD_CACHE:
            DASHBOARD_CACHE[k] = None




def classify_game(game):
    c = game["components"]

    team_impact = (
        c.get("team_death_burden", 0)
        + c.get("death_outliers", 0)
        + c.get("team_collapse", 0)
        + c.get("afk_penalty", 0)
    )

    player_negative = (
        c.get("low_damage_grief", 0)
        + max(0, c.get("vision_grief", 0))
    )

    player_positive = (
        c.get("player_relative_bonus", 0)
        + c.get("clean_early_bonus", 0)
        + c.get("objective_disparity", 0)
        + abs(c.get("hard_carry_bonus", 0))
    )

    player_contribution = (
    c.get("player_relative_bonus", 0)
    + c.get("objective_disparity", 0)
    + abs(c.get("hard_carry_bonus", 0))
    )

    win = game["win"]

    # ---- CLASSIFICATION ----
    if win:
        if team_impact < 5 and player_negative <= 5 and player_contribution < 5:
            return "CAKE WALK", "⚪"

        if team_impact >= 15 and player_positive > player_negative:
            return "HARD CARRY", "🟡"

        if player_negative > player_positive:
            return "BOOSTED", "🔵"

        return "FAIR WIN", "🟢"

    else:  # LOSS
        if team_impact >= 12 and player_positive > player_negative:
            return "GRIEFED", "🔴"

        if player_negative > player_positive:
            return "INTER", "⚫"

        if team_impact < 9 and player_negative <= 6:
            return "FAIR LOSS", "⚪"

        return "LOST CAUSE", "🟠"

def summarize_games(games):
    counts = {
        "CAKE WALK": 0,
        "FAIR WIN": 0,
        "HARD CARRY": 0,
        "FAIR LOSS": 0,
        "GRIEFED": 0,
        "LOST CAUSE": 0,
        "INTER": 0,
        "BOOSTED": 0,
    }

    for g in games:
        label, _ = classify_game(g)
        counts[label] += 1

    return counts

# --------------------
# Background update task. Disabled by default for Riot API safety.
# --------------------
@tasks.loop(minutes=SCHEDULED_UPDATE_INTERVAL_MINUTES)
async def scheduled_update_task():
    await incremental_update_core(notify_channel_id=TEST_CHANNEL_ID)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if ENABLE_SCHEDULED_UPDATES and not scheduled_update_task.is_running():
        scheduled_update_task.start()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        signature = f"{COMMAND_PREFIX}{ctx.command.qualified_name} {ctx.command.signature}".strip()
        await ctx.send(f"Missing argument. Usage: `{signature}`")
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send(f"Bad argument: `{error}`")
        return

    if isinstance(error, commands.CommandInvokeError):
        original = error.original
        await ctx.send(f"Command failed: `{type(original).__name__}`. Check the bot console for details.")
        print(f"Command error in {ctx.command}: {repr(original)}")
        return

    await ctx.send(f"Command failed: `{type(error).__name__}`.")
    print(f"Command error: {repr(error)}")

# --------------------
# Player management
# --------------------
@bot.command()
async def addsummoner(ctx, *, riot_id: str):
    try:
        game_name, tag_line = parse_riot_id(riot_id)
    except ValueError:
        await ctx.send("Use format: `Name#TAG` (example: `SomeName#NA1`)")
        return

    try:
        info = await asyncio.to_thread(get_player_profile, game_name, tag_line)
    except Exception as e:
        await ctx.send("Could not verify player with Riot API.")
        print(e)
        return

    riot_key = f"{info['game_name']}#{info['tag_line']}"
    data = load_data()

    upsert_player(
        data,
        riot_key,
        info["game_name"],
        info["tag_line"],
        info["puuid"],
        encrypted_summoner_id=None,
    )

    update_player_mmr_from_profile(data["players"][riot_key], info)
    for entry in info.get("ranked_entries", []):
        if entry.get("queueType") == "RANKED_SOLO_5x5":
            data["players"][riot_key]["ranked_solo_tier"] = entry.get("tier")



    save_data(data)
    await ctx.send(f"Added: **{riot_key}**")

@bot.command()
async def playerlist(ctx):
    data = load_data()
    players = list(data.get("players", {}).keys())
    if not players:
        await ctx.send("No players added yet. Use `!addsummoner Name#TAG`.")
        return
    msg = "**Player Pool:**\n" + "\n".join(f"- {p}" for p in players)
    await send_long(ctx, msg)


@bot.command(aliases=["removeplayer"])
async def removesummoner(ctx, *, riot_id: str):
    data = load_data()
    resolved_riot_id = resolve_player_key(data, riot_id)
    if not resolved_riot_id:
        await ctx.send("Player not found.")
        return

    player = data.get("players", {}).pop(resolved_riot_id, None)
    data.get("player_match_index", {}).pop(resolved_riot_id, None)
    save_data(data)

    puuid = player.get("puuid") if player else None
    suffix = f" (PUUID {puuid[:8]}...)" if puuid else ""
    await ctx.send(
        f"Removed **{resolved_riot_id}**{suffix}. Stored match payloads were kept for cache reuse."
    )


@bot.command(name="status", aliases=["trackerstatus"])
async def tracker_status(ctx):
    data = load_data()
    health = cache_health(data)

    queue_counts = sorted(
        health["queue_counts"].items(),
        key=lambda item: item[1],
        reverse=True,
    )[:8]
    queue_line = ", ".join(
        f"{queue_name(qid)}: {count}" for qid, count in queue_counts
    ) or "none"

    lines = [
        "**Tracker Status**",
        f"Players: **{health['players']}**",
        f"Stored matches: **{health['matches']}**",
        f"Indexed match refs: **{health['indexed_refs']}**",
        f"Missing match details: **{health['missing_details']}**",
        f"Duplicate index refs: **{health['duplicate_refs']}**",
        f"Players missing PUUID: **{health['orphaned_players']}**",
        f"Last update: `{health['last_update_utc'] or 'never'}`",
        f"Scheduler: **{'enabled' if ENABLE_SCHEDULED_UPDATES else 'disabled'}** "
        f"({SCHEDULED_UPDATE_INTERVAL_MINUTES} min, running: {scheduled_update_task.is_running()})",
        f"Top queues: {queue_line}",
    ]
    await ctx.send("\n".join(lines))


@bot.command()
async def repaircache(ctx, prune_missing: bool = False):
    """
    Usage: !repaircache [true]
    Deduplicates cache indexes. Passing true also removes refs with no match payload.
    """
    data = load_data()
    before = cache_health(data)
    result = repair_cache_indexes(data, prune_missing_details=prune_missing)
    save_data(data)
    after = cache_health(data)

    lines = [
        "**Cache Repair Complete**",
        f"Removed player indexes: **{result['removed_player_indexes']}**",
        f"Removed duplicate refs: **{result['removed_duplicates']}**",
        f"Removed missing-detail refs: **{result['removed_missing_details']}**",
        f"Missing details: **{before['missing_details']}** -> **{after['missing_details']}**",
        "Data file was rewritten using the configured storage format.",
    ]
    await ctx.send("\n".join(lines))


@bot.command()
async def recentgames(ctx, *, args: str = ""):
    """
    Usage: !recentgames Name#TAG [count]
    Shows cached recent games for a tracked player.
    """
    try:
        riot_id, count = parse_riot_id_with_optional_count(args, default_count=10, max_count=20)
    except ValueError:
        await ctx.send("Usage: `!recentgames Name#TAG [count]`")
        return

    data = load_data()
    resolved_riot_id = resolve_player_key(data, riot_id)
    if not resolved_riot_id:
        await ctx.send("Player not found. Use `!addsummoner Name#TAG` first.")
        return

    player = data["players"][resolved_riot_id]
    puuid = player.get("puuid")
    if not puuid:
        await ctx.send("That player is missing a PUUID. Re-add or run the PUUID backfill script.")
        return

    matches = get_player_matches(data, resolved_riot_id)
    matches.sort(
        key=lambda m: m.get("info", {}).get("gameStartTimestamp")
        or m.get("info", {}).get("gameCreation")
        or 0,
        reverse=True,
    )

    rows = []
    for match in matches:
        info = match.get("info", {})
        participant = _participant_for_puuid(match, puuid)
        if not participant:
            continue

        t_local = _game_start_local(match)
        when = t_local.strftime("%b %d %I:%M%p") if t_local else "unknown"
        result = "W" if participant.get("win") else "L"
        champion = str(participant.get("championName", "Unknown"))
        if len(champion) > 12:
            champion = champion[:9] + "..."

        kills = int(participant.get("kills", 0) or 0)
        deaths = int(participant.get("deaths", 0) or 0)
        assists = int(participant.get("assists", 0) or 0)
        kda = (kills + assists) / max(1, deaths)
        cs = int(participant.get("totalMinionsKilled", 0) or 0) + int(
            participant.get("neutralMinionsKilled", 0) or 0
        )
        duration = int(info.get("gameDuration", 0) or 0)
        minutes = duration // 60

        rows.append(
            f"{when:<14} {result:<1} {queue_name(info.get('queueId')):<9} "
            f"{champion:<12} {kills:>2}/{deaths:<2}/{assists:<2} "
            f"{kda:>4.1f} {cs:>4}cs {minutes:>2}m"
        )
        if len(rows) >= count:
            break

    if not rows:
        await ctx.send("No cached games found. Try `!updaterecords` first.")
        return

    lines = [
        f"**Recent Games: {resolved_riot_id}**",
        "```",
        "When           R Queue     Champion      K/D/A  KDA   CS Dur",
        "-" * 63,
        *rows,
        "```",
    ]
    await send_long(ctx, "\n".join(lines))


@bot.command()
async def matchcard(ctx, *, args: str = ""):
    """
    Usage: !matchcard Name#TAG [count]
    Shows a clean embed for the latest cached match. No Riot API fetch.
    """
    try:
        riot_id, count = parse_riot_id_with_optional_count(args, default_count=1, max_count=20)
    except ValueError:
        await ctx.send("Usage: `!matchcard Name#TAG [count]`")
        return

    data = load_data()
    resolved = resolve_player_key(data, riot_id)
    if not resolved:
        await ctx.send("Player not found. Use `!addsummoner Name#TAG` first.")
        return

    card = build_match_card(data, resolved, count=count)
    if not card:
        await ctx.send("Not enough cached match data. Try `!updaterecords` first.")
        return

    m = card["metrics"]
    result = "WIN" if m["win"] else "LOSS"
    color = discord.Color.green() if m["win"] else discord.Color.red()
    title = f"{result} - {resolved} on {m['champion']}"
    if card["award"]:
        title += f" [{card['award']}]"

    embed = discord.Embed(title=title, color=color)
    embed.description = f"{queue_name(m['queue_id'])} - {relative_time(m['start'])} - {fmt_duration(m['duration_min'])}"
    embed.add_field(name="Role", value=m["role"], inline=True)
    embed.add_field(name="K/D/A", value=f"{m['kills']}/{m['deaths']}/{m['assists']} ({m['kda']:.2f})", inline=True)
    embed.add_field(name="CS/min", value=f"{m['cspm']:.1f}", inline=True)
    embed.add_field(name="Kill Participation", value=fmt_pct(m["kill_participation"]), inline=True)
    embed.add_field(name="Damage Share", value=fmt_pct(m["damage_share"]), inline=True)
    embed.add_field(name="Vision", value=str(m["vision"]), inline=True)
    embed.add_field(name="Tracked Teammates", value=short_list(card["teammates"], 6), inline=False)
    embed.set_footer(text=f"Cached match {count} of {len(get_player_matches(data, resolved))} - no API fetch")
    await ctx.send(embed=embed)


@bot.command()
async def streaks(ctx, queue: str = "all"):
    """
    Usage: !streaks [all|solo|flex|aram]
    Shows each player's current cached win/loss streak.
    """
    try:
        queue_ids, label = parse_queue_filter(queue)
    except ValueError as exc:
        await ctx.send(f"Bad queue. `{exc}`")
        return

    data = load_data()
    rows = []

    for riot_id, player in data.get("players", {}).items():
        puuid = player.get("puuid")
        if not puuid:
            continue

        matches = get_player_matches(data, riot_id)
        matches.sort(
            key=lambda m: m.get("info", {}).get("gameStartTimestamp")
            or m.get("info", {}).get("gameCreation")
            or 0,
            reverse=True,
        )

        streak_win = None
        streak_count = 0
        last_champion = "Unknown"
        last_queue = "Unknown"

        for match in matches:
            info = match.get("info", {})
            qid = info.get("queueId")
            if queue_ids is not None and qid not in queue_ids:
                continue

            participant = _participant_for_puuid(match, puuid)
            if not participant:
                continue

            win = bool(participant.get("win", False))
            if streak_win is None:
                streak_win = win
                streak_count = 1
                last_champion = participant.get("championName", "Unknown")
                last_queue = queue_name(qid)
                continue

            if win != streak_win:
                break
            streak_count += 1

        if streak_win is None:
            continue

        marker = "W" if streak_win else "L"
        rows.append((streak_count, marker, riot_id, last_queue, last_champion))

    if not rows:
        await ctx.send("No cached games found for that queue. Try `!updaterecords` first.")
        return

    rows.sort(key=lambda r: (r[0], r[1] == "W", r[2]), reverse=True)

    def pad(text, width):
        text = str(text)
        if len(text) > width:
            return text[: width - 3] + "..."
        return text + " " * (width - len(text))

    lines = [
        f"**Current Streaks - {label}**",
        "```",
        f"{pad('Player', 28)} | Streak | Last Queue | Last Champ",
        "-" * 68,
    ]
    for count, marker, riot_id, last_queue, last_champion in rows:
        lines.append(
            f"{pad(riot_id, 28)} | {marker}{count:<5} | "
            f"{pad(last_queue, 10)} | {last_champion}"
        )
    lines.append("```")
    await send_long(ctx, "\n".join(lines))


@bot.command()
async def compare(ctx, *, args: str = ""):
    """
    Usage: !compare Name#TAG OtherName#TAG [solo|flex|aram|all]
    Compares two tracked players from cached matches only.
    """
    try:
        first, second, queue_ids, label = parse_two_riot_ids_and_queue(args)
    except ValueError as exc:
        await ctx.send(str(exc))
        return

    data = load_data()
    first_key = resolve_player_key(data, first)
    second_key = resolve_player_key(data, second)
    if not first_key or not second_key:
        await ctx.send("Both players must be tracked. Use `!addsummoner Name#TAG` first.")
        return

    result = compare_players(data, first_key, second_key, queue_ids=queue_ids)
    if not result:
        await ctx.send("Not enough cached data to compare those players.")
        return

    def summary_line(s):
        return (
            f"WR {s['wr']:.1f}% ({s['wins']}-{s['losses']})\n"
            f"KDA {s['kda']:.2f} | CS/min {s['cspm']:.1f}\n"
            f"DMG/min {s['damage_per_min']:.0f} | Vision/game {s['vision_per_game']:.1f}\n"
            f"Best queue: {queue_name(s['best_queue'])}\n"
            f"Recent: {s['recent_form']}\n"
            f"Top champs: {player_champ_line(s['top_champs'])}"
        )

    embed = discord.Embed(
        title=f"Smart Compare - {label}",
        description=f"**{first_key}** vs **{second_key}**",
        color=discord.Color.blurple(),
    )
    embed.add_field(name=first_key, value=summary_line(result["a"]), inline=True)
    embed.add_field(name=second_key, value=summary_line(result["b"]), inline=True)
    h2h = result["together_games"]
    h2h_text = "No cached games together"
    if h2h:
        h2h_text = (
            f"Together: {h2h} games\n"
            f"Same team: {result['same_team_games']} games\n"
            f"{first_key} wins in those games: {result['a_h2h_wins']}-{h2h - result['a_h2h_wins']}"
        )
    embed.add_field(name="Head-to-head / together", value=h2h_text, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="champion", aliases=["champ"])
async def champion_cmd(ctx, *, args: str = ""):
    """
    Usage: !champion Name#TAG Champion [solo|flex|aram|all]
    Shows cached champion-specific stats for one player.
    """
    try:
        riot_id, champion, queue_ids, label = parse_champion_args(args)
    except ValueError as exc:
        await ctx.send(str(exc))
        return

    data = load_data()
    resolved = resolve_player_key(data, riot_id)
    if not resolved:
        await ctx.send("Player not found. Use `!addsummoner Name#TAG` first.")
        return

    result = champion_deep_dive(data, resolved, champion, queue_ids=queue_ids)
    if not result:
        await ctx.send(f"Not enough cached data for `{resolved}` on `{champion}` in {label}.")
        return

    def match_line(metrics):
        outcome = "W" if metrics["win"] else "L"
        return (
            f"{outcome} {queue_name(metrics['queue_id'])} "
            f"{metrics['kills']}/{metrics['deaths']}/{metrics['assists']} "
            f"KDA {metrics['kda']:.2f}, {relative_time(metrics['start'])}"
        )

    embed = discord.Embed(
        title=f"{resolved} - {result['champion']} Deep Dive",
        description=f"{label} cached stats",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Record", value=f"{result['wins']}-{result['losses']} ({result['wr']:.1f}%)", inline=True)
    embed.add_field(name="KDA", value=f"{result['kda']:.2f}", inline=True)
    embed.add_field(name="CS/min", value=f"{result['cspm']:.1f}", inline=True)
    embed.add_field(name="Damage/min", value=f"{result['damage_per_min']:.0f}", inline=True)
    embed.add_field(name="Recent trend", value=result["recent_form"], inline=True)
    embed.add_field(name="Games", value=str(result["games"]), inline=True)
    embed.add_field(name="Best Match", value=match_line(result["best"]), inline=False)
    embed.add_field(name="Pain Match", value=match_line(result["worst"]), inline=False)
    mates = [f"{name} ({count})" for name, count in result["teammates"]]
    embed.add_field(name="Most common tracked teammates", value=short_list(mates, 3), inline=False)
    await ctx.send(embed=embed)


def build_awards_embed(data, mode):
    period, start, end = parse_period(mode)
    result = group_awards(data, start=start, end=end)
    embed = discord.Embed(
        title=f"{period.capitalize()} Group Awards",
        description=f"{start:%b %d %I:%M%p} to {end:%b %d %I:%M%p} local\nCached games: **{result['games']}**",
        color=discord.Color.purple(),
    )

    if result["games"] == 0:
        embed.add_field(name="Not enough cached data", value="Run `!updaterecords` or try another period.", inline=False)
        return embed

    for award in result["awards"][:8]:
        rec = award["record"]
        value = f"{award['player']} - {int(rec['games'])} games"
        if award["label"] == "CS Goblin":
            value = f"{award['player']} - {rec['cs'] / max(1, rec['duration']):.1f} CS/min"
        elif award["label"] == "Vision Warden":
            value = f"{award['player']} - {rec['vision'] / max(1, rec['games']):.1f} vision/game"
        elif award["label"] == "Damage Dealer":
            value = f"{award['player']} - {rec['damage'] / max(1, rec['duration']):.0f} dmg/min"
        elif award["label"] == "Survivalist":
            value = f"{award['player']} - {rec['deaths'] / max(1, rec['games']):.1f} deaths/game"
        elif award["label"] == "ARAM Menace":
            value = f"{award['player']} - {int(rec['aram_games'])} ARAM games"
        embed.add_field(name=award["label"], value=value, inline=True)

    if result["best_duo"]:
        rec = result["best_duo"]["record"]
        embed.add_field(
            name="Duo Diff",
            value=f"{result['best_duo']['player']} - {rec['wins']}-{rec['games'] - rec['wins']} ({rec['wins'] / max(1, rec['games']) * 100:.1f}%)",
            inline=False,
        )
    if result["most_played_champ"]:
        champ = result["most_played_champ"]
        embed.add_field(name="Most Played Champ", value=f"{champ['champion']} - {int(champ['games'])} games", inline=True)
    if result["highest_wr_champ"]:
        champ = result["highest_wr_champ"]
        embed.add_field(name="Hot Champ", value=f"{champ['champion']} - {champ['wr']:.1f}% over {int(champ['games'])} games", inline=True)
    return embed


@bot.command()
async def awards(ctx, mode: str = "daily"):
    try:
        embed = build_awards_embed(load_data(), mode)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    await ctx.send(embed=embed)


@bot.command()
async def recap(ctx, mode: str = "daily"):
    try:
        period, start, end = parse_period(mode)
    except ValueError as exc:
        await ctx.send(str(exc))
        return

    data = load_data()
    awards_result = group_awards(data, start=start, end=end)
    embed = discord.Embed(
        title=f"{period.capitalize()} Night Recap",
        description=f"{start:%b %d %I:%M%p} to {end:%b %d %I:%M%p} local\nCached games: **{awards_result['games']}**",
        color=discord.Color.teal(),
    )
    if awards_result["games"] == 0:
        embed.add_field(name="No recap yet", value="Not enough cached games in this window.", inline=False)
        await ctx.send(embed=embed)
        return

    carry = next((a for a in awards_result["awards"] if a["label"] == "Carry King"), None)
    pain = None
    biggest = None
    for riot_id in data.get("players", {}):
        rows = sorted_player_matches(data, riot_id, start=start, end=end)
        for _, _, metrics in rows:
            score = performance_score(metrics)
            if metrics["win"]:
                if biggest is None or score > biggest[0]:
                    biggest = (score, riot_id, metrics)
            else:
                if pain is None or score < pain[0]:
                    pain = (score, riot_id, metrics)

    if carry:
        embed.add_field(name="Best Performance", value=carry["player"], inline=True)
    if biggest:
        _, player, m = biggest
        embed.add_field(name="Biggest Carry", value=f"{player} on {m['champion']} ({m['kills']}/{m['deaths']}/{m['assists']})", inline=True)
    if pain:
        _, player, m = pain
        embed.add_field(name="Worst Pain Game", value=f"{player} had a rough {m['champion']} game. We queue again.", inline=True)
    if awards_result["best_duo"]:
        rec = awards_result["best_duo"]["record"]
        embed.add_field(name="Best Duo", value=f"{awards_result['best_duo']['player']} ({rec['wins']}-{rec['games'] - rec['wins']})", inline=True)
    if awards_result["most_played_champ"]:
        champ = awards_result["most_played_champ"]
        embed.add_field(name="Most Played Champ", value=f"{champ['champion']} ({int(champ['games'])} games)", inline=True)
    if awards_result["highest_wr_champ"]:
        champ = awards_result["highest_wr_champ"]
        embed.add_field(name="Highest WR Champ", value=f"{champ['champion']} ({champ['wr']:.1f}%)", inline=True)

    fun_lines = []
    for label in ("CS Goblin", "Vision Warden", "ARAM Menace", "Duo Diff"):
        item = next((a for a in awards_result["awards"] if a["label"] == label), None)
        if item:
            fun_lines.append(f"**{label}:** {item['player']}")
    if awards_result["best_duo"]:
        fun_lines.append(f"**Duo Diff:** {awards_result['best_duo']['player']}")
    embed.add_field(name="Fun Awards", value="\n".join(fun_lines) or "No awards yet", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="commands", aliases=["commandlist"])
async def command_list(ctx):
    embed = discord.Embed(title="LOL Tracker Commands", color=discord.Color.blurple())
    embed.add_field(
        name="Players",
        value=(
            "`!addsummoner Name#TAG`\n"
            "`!removesummoner Name#TAG`\n"
            "`!playerlist`\n"
            "`!playerinfo Name#TAG`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Match History",
        value=(
            "`!recentgames Name#TAG [count]`\n"
            "`!matchcard Name#TAG [count]`\n"
            "`!champion Name#TAG Champion [queue]`\n"
            "`!compare Name#TAG Other#TAG [queue]`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Group Reports",
        value=(
            "`!dailyrecords` `!weeklyrecords` `!seasonrecords`\n"
            "`!dashboard` `!recap [daily|weekly|season]`\n"
            "`!awards [daily|weekly|season]`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Analytics",
        value=(
            "`!streaks [all|solo|flex|aram]`\n"
            "`!topduos [min_games]`\n"
            "`!topchamps [queue] [min_games]`\n"
            "`!topflexstacks` `!grieftracker Name#TAG`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Ops",
        value=(
            "`!live`\n"
            "`!updaterecords` `!updateseason`\n"
            "`!status` `!repaircache [true]`\n"
            "`!debugrecentqueues`"
        ),
        inline=False,
    )
    embed.set_footer(text="Queues: all, solo, flex, aram. Riot IDs with spaces work best as Name#TAG followed by args.")
    await ctx.send(embed=embed)


@bot.command(name="grieftracker")
async def grieftracker_cmd(ctx, *, riot_id: str):
    await ctx.typing()

    try:
        data = load_data()

        # --------------------
        # Validation
        # --------------------
        resolved_riot_id = resolve_player_key(data, riot_id)
        if not resolved_riot_id:
            await ctx.send("Player not found. Use `!addsummoner Name#TAG` first.")
            return

        riot_id = resolved_riot_id
        player_entry = data["players"][riot_id]
        puuid = player_entry.get("puuid")

        match_ids = data.get("player_match_index", {}).get(riot_id, [])
        matches = [
            data["matches"][mid]
            for mid in reversed(match_ids)
            if mid in data.get("matches", {})
            and data["matches"][mid]["info"].get("queueId") == 420
        ]
        if not matches:
            await ctx.send("No stored matches found. Try `!updaterecords`.")
            return

        result = evaluate_grieftracker(matches, puuid, games=10)

        if result["games_analyzed"] == 0:
            await ctx.send("No ranked solo/duo games found.")
            return

        games = result["games"]

        # --------------------
        # Classification
        # --------------------
        summary = summarize_games(games)

        # --------------------
        # Message construction
        # --------------------
        lines = []
        lines.append("**Grief Tracker — Ranked Solo/Duo (Last 10 Games)**")
        lines.append(f"Player: `{riot_id}`")
        lines.append("")

        # Average grief score (loss-weighted)
        loss_games = [g for g in games if not g["win"]]
        avg_grief = round(
            sum(g["game_grief_points"] for g in loss_games) / max(1, len(loss_games)),
            1
        )

        lines.append(f"Grief Score: **{avg_grief}** (avg per loss)")
        lines.append(
            "_Meaning:_ This represents how much **grief was inflicted on you by teammates**, "
            "not how much you griefed others."
        )
        lines.append("")

        if avg_grief >= 110:
            lines.append("🧠 Interpretation: Losses were **largely unplayable** due to extreme teammate impact.")
        elif avg_grief >= 85:
            lines.append("🧠 Interpretation: You were **heavily griefed** in most losses.")
        elif avg_grief >= 65:
            lines.append("🧠 Interpretation: Teammates **frequently compromised** otherwise winnable games.")
        elif avg_grief >= 45:
            lines.append("🧠 Interpretation: Losses reflect **normal variance with some team issues**.")
        else:
            lines.append("🧠 Interpretation: Losses were **mostly fair** with limited external grief.")

        lines.append("")
        lines.append("**Outcome Breakdown:**")

        def add(label, emoji):
            count = summary.get(label, 0)
            if count:
                lines.append(f"{emoji} **{label}**: {count}")

        ORDERED_OUTCOMES = [
            ("CAKE WALK", "⚪"),
            ("FAIR WIN", "🟢"),
            ("HARD CARRY", "🟡"),
            ("FAIR LOSS", "⚪"),
            ("GRIEFED", "🔴"),
            ("LOST CAUSE", "🟠"),
            ("INTER", "⚫"),
            ("BOOSTED", "🔵"),
        ]


        for label, emoji in ORDERED_OUTCOMES:
            count = summary.get(label, 0)
            if count:
                lines.append(f"{emoji} **{label}**: {count}")

        # --------------------
        # Statistical anomaly tier
        # --------------------
        unplayable = (
            summary.get("LOST CAUSE", 0)
            + summary.get("GRIEFED", 0)
        )

        if unplayable >= 7:
            lines.append(
                "☠️ **ABSOLUTELY COOKED** — majority of games were effectively unplayable"
            )
        elif summary.get("LOST CAUSE", 0) >= 5:
            lines.append(
                "☠️ **STATISTICAL ANOMALY** — outcomes far outside expected variance"
            )

        lines.append("")
        lines.append(
            "**How to read this:**\n"
            "• **CAKE WALK** → won with minimal resistance\n"
            "• **FAIR WIN** → standard competitive win\n"
            "• **FAIR LOSS** → close, competitive loss with no clear blame\n"
            "• **HARD CARRY** → won despite team grief\n"
            "• **GRIEFED** → lost despite playing well\n"
            "• **INTER** → losses driven primarily by own play\n"
            "• **LOST CAUSE** → games were statistically unwinnable\n\n"
            "This analysis reflects *patterns across the last 10 games*, "
            "not a single match."
        )

        # --------------------
        # Worst innocent game (griefed but not inting)
        # --------------------
        innocent_losses = []

        for g in games:
            label, _ = classify_game(g)
            if not g["win"] and label in ("GRIEFED", "LOST CAUSE"):
                neg = (
                    g["components"].get("low_damage_grief", 0)
                    + max(0, g["components"].get("vision_grief", 0))
                )
                if neg <= 5:
                    innocent_losses.append(g)

        if innocent_losses:
            innocent_losses.sort(key=lambda g: g["game_grief_points"])
            worst = innocent_losses[int(len(innocent_losses) * 0.8)]


            champ = worst.get("champion", "Unknown")
            k = worst.get("kills", "?")
            d = worst.get("deaths", "?")
            a = worst.get("assists", "?")
            duration = worst.get("duration_min", "?")
            when = worst.get("start_time_local", "Unknown date")

            ts = worst.get("start_time")
            if ts:
                dt = datetime.fromtimestamp(ts / 1000)
                when = dt.strftime("%b %d, %I:%M %p")
            else:
                when = "Unknown date"

            lines.append("")
            lines.append("**Most Innocent LOSS:**")

            avg_tier = worst.get("avg_teammate_tier")
            avg_wr = worst.get("avg_teammate_wr")

            extra_context = ""
            if avg_tier or avg_wr:
                extra_context = "• Teammates: "
                if avg_tier:
                    extra_context += f"Avg Rank {avg_tier}"
                if avg_wr is not None:
                    if avg_tier:
                        extra_context += " | "
                    extra_context += f"Avg WR {avg_wr:.1f}%"
                extra_context += "\n"

            lines.append(
                f"• {worst['champion']} — **{worst['kills']}/{worst['deaths']}/{worst['assists']}** "
                f"in {worst['duration_min']} min\n"
                f"• Played on: {when}\n"
                f"{extra_context}"
                f"• Grief Points: **{worst['game_grief_points']}** | "
                f"Team death/min: {worst['team_dpm']} | You: {worst['player_dpm']}"
            )


        await ctx.send("\n".join(lines))


    except Exception as e:
        await ctx.send(f"Error running grief tracker: `{type(e).__name__}: {e}`")
        raise


# --------------------
# Player info
# --------------------
@bot.command()
async def playerinfo(ctx, *, riot_id: str):
    if "#" not in riot_id:
        await ctx.send("Use format: `Name#TAG`")
        return

    await ctx.typing()
    game_name, tag_line = riot_id.split("#", 1)

    try:
        info = await asyncio.to_thread(get_player_profile, game_name, tag_line)
    except Exception as e:
        await ctx.send("Failed to fetch player info.")
        print(e)
        return
    
    data = load_data()
    riot_key = f"{info['game_name']}#{info['tag_line']}"

    if riot_key in data["players"]:
        # purely optional, informational only
        update_player_rank_from_profile(data["players"][riot_key], info)
        save_data(data)

    solo_line = "Solo/Duo: Unranked"
    flex_line = "Flex: Unranked"

    for entry in info.get("ranked_entries", []):
        q = entry.get("queueType")
        tier = entry.get("tier")
        div = entry.get("rank")
        lp = entry.get("leaguePoints", 0)
        wins = entry.get("wins", 0)
        losses = entry.get("losses", 0)
        games = wins + losses
        wr = (wins / games * 100.0) if games > 0 else 0.0
        line = f"{tier} {div} ({lp} LP) — {wins}-{losses} ({wr:.1f}%)"

        if q == "RANKED_SOLO_5x5":
            solo_line = f"Solo/Duo: {line}"
        elif q == "RANKED_FLEX_SR":
            flex_line = f"Flex: {line}"

    puuid = info["puuid"]

    try:
        kda = await asyncio.to_thread(compute_recent_kda, puuid, 8)
        recent_kda_line = (
            f"Recent KDA (last {kda['games']}): {kda['kda']:.2f} "
            f"({kda['kills']}/{kda['deaths']}/{kda['assists']})"
        )
    except Exception:
        recent_kda_line = "Recent KDA: (failed to load)"

    try:
        mastery = await asyncio.to_thread(get_top_mastery_by_riot_id, game_name, tag_line, 10)
        mastery_lines = "\n".join(
            f"{i+1}) {m['champion']} — M{m['level']} — {m['points']:,} pts"
            for i, m in enumerate(mastery)
        )
    except Exception:
        mastery_lines = "(unavailable)"

    try:
        solo_champs = await asyncio.to_thread(solo_top_champs_wl, puuid, 30, 5, 420)
        solo_champ_lines = "\n".join(
            f"{c['champion']} — {c['wins']}-{c['losses']} ({c['wr']:.1f}%) — {c['games']} games"
            for c in solo_champs
        ) or "(no Solo/Duo games found)"
    except Exception:
        solo_champ_lines = "(failed to load)"

    msg = (
        f"**{info['game_name']}#{info['tag_line']}**\n"
        f"Level: {info['summoner_level']}\n"
        f"{solo_line}\n"
        f"{flex_line}\n\n"
        f"{recent_kda_line}\n\n"
        f"**Top 10 Mastery**\n{mastery_lines}\n\n"
        f"**Top 5 Solo/Duo Champs (last 30 games)**\n{solo_champ_lines}"
    )
    await send_long(ctx, msg)

# --------------------
# Updates
# --------------------
@bot.command()
async def updaterecords(ctx):
    await incremental_update_core(ctx=ctx)

@bot.command()
async def updateseason(ctx):
    if update_lock.locked():
        await ctx.send("⚠️ Update already running.")
        return

    async with update_lock:
        await ctx.send(f"⏳ Season backfill starting (since {SEASON_START_LOCAL:%b %d %I:%M%p} local)...")

        data = load_data()
        if not data.get("players"):
            await ctx.send("No players added yet.")
            return

        new_matches = 0
        filled_missing = 0
        errors = 0

        for riot_id, p in data["players"].items():
            puuid = p.get("puuid")
            if not puuid:
                errors += 1
                continue

            data["player_match_index"].setdefault(riot_id, [])
            known = set(data["player_match_index"][riot_id])

            missing_ids = [
                mid for mid in data["player_match_index"][riot_id]
                if mid not in data["matches"]
            ]

            for mid in missing_ids:
                try:
                    m = await asyncio.to_thread(get_match, mid)
                except Exception as e:
                    print("fill missing match detail failed:", mid, e)
                    errors += 1
                    continue

                t_local = _game_start_local(m)
                if t_local and t_local < SEASON_START_LOCAL:
                    continue

                data["matches"][mid] = m
                filled_missing += 1

            start_idx = 0
            page_size = 100

            while True:
                try:
                    ids = await asyncio.to_thread(
                        get_match_ids_by_puuid, puuid, page_size, None, start_idx
                    )
                except Exception as e:
                    print("match id fetch failed:", riot_id, e)
                    errors += 1
                    break

                if not ids:
                    break

                stop = False
                for mid in ids:
                    if mid in data["matches"]:
                        if mid not in known:
                            data["player_match_index"][riot_id].append(mid)
                            known.add(mid)
                        continue

                    try:
                        m = await asyncio.to_thread(get_match, mid)
                    except Exception as e:
                        print("match detail failed:", mid, e)
                        errors += 1
                        continue

                    t_local = _game_start_local(m)
                    if t_local and t_local < SEASON_START_LOCAL:
                        stop = True
                        break

                    data["matches"][mid] = m
                    if mid not in known:
                        data["player_match_index"][riot_id].append(mid)
                        known.add(mid)
                    new_matches += 1

                if stop:
                    break

                start_idx += page_size

            save_data(data)

        data["last_update_utc"] = now_utc_iso()
        save_data(data)

        await ctx.send(
            f"✅ Season backfill complete. "
            f"New: **{new_matches}**, Filled: **{filled_missing}**, Errors: **{errors}**."
        )

# --------------------
# Daily records
# --------------------

@bot.command()
async def dailyrecords(ctx):
    await incremental_update_core(ctx=None)
    data = load_data()
    if not data.get("players"):
        await ctx.send("No players added yet. Use `!addsummoner Name#TAG` first.")
        return

    start, end = window_3am_to_3am_local()

    rows = []
    for riot_id, p in data["players"].items():
        puuid = p.get("puuid")
        if not puuid:
            continue

        mids = data.get("player_match_index", {}).get(riot_id, [])
        matches = [data["matches"][mid] for mid in mids if mid in data.get("matches", {})]

        solo = compute_wl_kda(matches, puuid, 420, start, end)
        flex = compute_wl_kda(matches, puuid, 440, start, end)

        aram = {"games": 0, "wins": 0, "losses": 0, "kda": 0.0}
        weight = 0
        for q in ARAM_QUEUES:
            r = compute_wl_kda(matches, puuid, q, start, end)
            aram["games"] += r["games"]
            aram["wins"] += r["wins"]
            aram["losses"] += r["losses"]
            aram["kda"] += r["kda"] * r["games"]
            weight += r["games"]
        aram["kda"] = aram["kda"] / weight if weight else 0.0

        total_games = solo["games"] + flex["games"] + aram["games"]

        player = data["players"][riot_id]
        mmr_delta = (
            mmr_delta_since(player, "solo", start.isoformat())
            + mmr_delta_since(player, "flex", start.isoformat())
        )

        tier = player.get("ranked_solo_tier")

        wins = solo["wins"] + flex["wins"] + aram["wins"]
        games = max(1, total_games)
        wr = wins / games * 100

        rows.append((
            total_games,
            riot_id,
            solo,
            flex,
            aram,
            mmr_delta,
            tier,
            wr
        ))

    rows.sort(
        key=lambda r: (r[0] if r[0] is not None else -1, r[1]),
        reverse=True
    )


    def wl(x): return f"{x['wins']}-{x['losses']}"
    def kda(x): return f"{x['kda']:.2f}"

    NAME_W = 28
    WL_W = 7
    KDA_W = 5
    MMR_W = 6
    BAR_W = 10

    def pad(s, w):
        s = str(s)
        if len(s) > w:
            return s[:w-1] + "…"
        return s + " " * (w - len(s))

    # Totals
    solo_w = solo_l = flex_w = flex_l = aram_w = aram_l = 0
    for _, _, solo, flex, aram, _, _, _ in rows:
        solo_w += solo["wins"]; solo_l += solo["losses"]
        flex_w += flex["wins"]; flex_l += flex["losses"]
        aram_w += aram["wins"]; aram_l += aram["losses"]

    header = (
        f"**Daily Records** "
        f"({start:%b %d %I:%M%p} → {end:%b %d %I:%M%p} local)"
    )

    dash_len = (
        NAME_W
        + 3
        + (WL_W + 1 + KDA_W) * 3
        + 3
        + MMR_W
        + 2
        + BAR_W
    )

    lines = [
        header,
        "```",
        pad("Player", NAME_W) + " | "
        + pad("Solo", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("Flex", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("ARAM", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("ΔMMR", MMR_W) + "  WR",
        "-" * dash_len,
    ]

    for _, riot_id, solo, flex, aram, mmr, tier, wr in rows:
        icon = rank_icon(tier)
        lines.append(
            pad(f"{icon} {riot_id}", NAME_W) + " | "
            + pad(wl(solo), WL_W) + " " + pad(kda(solo), KDA_W) + " | "
            + pad(wl(flex), WL_W) + " " + pad(kda(flex), KDA_W) + " | "
            + pad(wl(aram), WL_W) + " " + pad(kda(aram), KDA_W) + " | "
            + pad(f"{mmr:+}", MMR_W) + "  "
            + wr_bar(wr)
        )

    lines.append("-" * dash_len)
    lines.append(
        pad("TOTAL", NAME_W) + " | "
        + pad(f"{solo_w}-{solo_l}", WL_W) + "     | "
        + pad(f"{flex_w}-{flex_l}", WL_W) + "     | "
        + pad(f"{aram_w}-{aram_l}", WL_W) + "     | "
        + pad("—", MMR_W)
    )
    lines.append("```")

    live_games = await get_live_games_async(data)
    lines.append("**LIVE GAMES**")
    lines.append("```")
    if live_games:
        lines.extend(format_live_games(live_games))
    else:
        lines.append("No one in the pool is currently in-game.")
    lines.append("```")

    lines.append(
        "Legend: ⭐ Challenger 🔴 GM 🟣 Master 🔷 Diamond 🟢 Emerald "
        "🔵 Plat 🟡 Gold ⚪ Silver 🟤 Bronze ⬛ Iron"
    )

    await send_long(ctx, "\n".join(lines))

# --------------------
# Weekly records (with LIVE GAMES)
# --------------------
@bot.command()
async def weeklyrecords(ctx):
    data = load_data()
    if not data.get("players"):
        await ctx.send("No players added yet. Use `!addsummoner Name#TAG` first.")
        return

    # Weekly window: 7 days ago -> now (local)
    _, end = window_3am_to_3am_local()
    start = end - timedelta(days=7)

    rows = []
    for riot_id, p in data["players"].items():
        puuid = p.get("puuid")
        if not puuid:
            continue

        mids = data.get("player_match_index", {}).get(riot_id, [])
        matches = [data["matches"][mid] for mid in mids if mid in data.get("matches", {})]

        solo = compute_wl_kda(matches, puuid, queue_id=420, start=start, end=end)
        flex = compute_wl_kda(matches, puuid, queue_id=440, start=start, end=end)

        aram_total = {"games": 0, "wins": 0, "losses": 0, "kda": 0.0}
        aram_kda_weight = 0
        for qid in ARAM_QUEUES:
            r = compute_wl_kda(matches, puuid, queue_id=qid, start=start, end=end)
            aram_total["games"] += r["games"]
            aram_total["wins"] += r["wins"]
            aram_total["losses"] += r["losses"]
            aram_total["kda"] += r["kda"] * r["games"]
            aram_kda_weight += r["games"]

        aram_total["kda"] = (
            aram_total["kda"] / aram_kda_weight
            if aram_kda_weight > 0 else 0.0
        )

        total_games = solo["games"] + flex["games"] + aram_total["games"]

        player = data["players"][riot_id]
        solo_delta = mmr_delta_since(player, "solo", start.isoformat())
        flex_delta = mmr_delta_since(player, "flex", start.isoformat())
        mmr_delta = solo_delta + flex_delta

        rows.append((total_games, riot_id, solo, flex, aram_total, mmr_delta))

    rows.sort(key=lambda x: x[0], reverse=True)

    def wl(x): return f"{x['wins']}-{x['losses']}"
    def kda(x): return f"{x['kda']:.2f}"

    NAME_W = 26
    WL_W = 7
    KDA_W = 5
    MMR_W = 6

    def pad(s, w):
        s = str(s)
        if len(s) > w:
            return s[: w - 1] + "…"
        return s + (" " * (w - len(s)))

    # Totals
    solo_w = solo_l = flex_w = flex_l = aram_w = aram_l = 0
    for _, _, solo, flex, aram, _ in rows:
        solo_w += solo["wins"]; solo_l += solo["losses"]
        flex_w += flex["wins"]; flex_l += flex["losses"]
        aram_w += aram["wins"]; aram_l += aram["losses"]

    def weighted_avg_kda(idx):
        total_g = 0
        total_kda = 0.0
        for _, _, solo, flex, aram, _ in rows:
            x = [solo, flex, aram][idx]
            g = x["games"]
            total_g += g
            total_kda += x["kda"] * g
        return (total_kda / total_g) if total_g > 0 else 0.0

    solo_avg = weighted_avg_kda(0)
    flex_avg = weighted_avg_kda(1)
    aram_avg = weighted_avg_kda(2)

    header_title = (
        f"Weekly Records "
        f"({start:%b %d %I:%M%p} → {end:%b %d %I:%M%p} local)"
    )

    dash_len = (
        NAME_W
        + 3
        + (WL_W + 1 + KDA_W) * 3
        + 3
        + MMR_W
    )

    lines = [
        f"**{header_title}**",
        "```",
        pad("Player", NAME_W) + " | "
        + pad("Solo WL", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("Flex WL", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("ARAM WL", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("ΔMMR", MMR_W),
        "-" * dash_len,
    ]

    for _, riot_id, solo, flex, aram, mmr_delta in rows:
        lines.append(
            pad(riot_id, NAME_W) + " | "
            + pad(wl(solo), WL_W) + " " + pad(kda(solo), KDA_W) + " | "
            + pad(wl(flex), WL_W) + " " + pad(kda(flex), KDA_W) + " | "
            + pad(wl(aram), WL_W) + " " + pad(kda(aram), KDA_W) + " | "
            + pad(f"{mmr_delta:+}", MMR_W)
        )

    lines.append("-" * dash_len)
    lines.append(
        pad("TOTAL", NAME_W) + " | "
        + pad(f"{solo_w}-{solo_l}", WL_W) + " " + pad(f"{solo_avg:.2f}", KDA_W) + " | "
        + pad(f"{flex_w}-{flex_l}", WL_W) + " " + pad(f"{flex_avg:.2f}", KDA_W) + " | "
        + pad(f"{aram_w}-{aram_l}", WL_W) + " " + pad(f"{aram_avg:.2f}", KDA_W) + " | "
        + pad("—", MMR_W)
    )
    lines.append("```")

    live_games = await get_live_games_async(data)
    lines.append("**LIVE GAMES**")
    lines.append("```")
    if live_games:
        lines.extend(format_live_games(live_games))
    else:
        lines.append("No one in the pool is currently in-game.")
    lines.append("```")

    await send_long(ctx, "\n".join(lines))


# --------------------
# Analytics
# --------------------
@bot.command()
async def topflexstacks(ctx):
    data = load_data()
    if not data.get("players"):
        await ctx.send("No players added yet.")
        return

    top, unique_count = await asyncio.to_thread(
        compute_top_flex_stacks, data, 5, SEASON_START_LOCAL
    )

    if not top:
        await ctx.send("No qualifying 5-stacks found.")
        return

    lines = [
        f"**Top 5 Flex 5-Stacks**",
        f"Unique stacks: **{unique_count}**",
        "```",
    ]
    for i, r in enumerate(top, 1):
        lines.append(
            f"{i}) {r['stack']}  {r['wins']}-{r['losses']} "
            f"({r['wr']:.1f}%) [{r['games']}g]"
        )
    lines.append("```")
    await send_long(ctx, "\n".join(lines))

@bot.command()
async def topduos(ctx, min_games: int = 3):
    if update_lock.locked():
        await ctx.send("⚠️ Update in progress.")
        return

    data = load_data()
    if not data.get("players"):
        await ctx.send("No players added yet.")
        return

    min_games = max(1, min(100, min_games))
    results = await asyncio.to_thread(
        compute_top_duos,
        data,
        queue_id=420,
        min_games=min_games,
        limit=10,
    )
    if not results:
        await ctx.send(f"No qualifying duos found with at least {min_games} games.")
        return

    lines = [f"**Top Duos (Solo/Duo, min {min_games} games)**", "```"]
    for r in results[:10]:
        lines.append(
            f"{r['duo']}  {r['wins']}-{r['losses']} "
            f"({r['wr']:.1f}%) [{r['games']}g]"
        )
    lines.append("```")
    await send_long(ctx, "\n".join(lines))


@bot.command()
async def topchamps(ctx, *, args: str = ""):
    """
    Usage: !topchamps [all|solo|flex|aram] [min_games]
    Shows group champion performance from cached tracked-player games.
    """
    try:
        queue_ids, label, min_games = parse_queue_and_min_games(args, default_min_games=3)
    except ValueError as exc:
        await ctx.send(f"Bad queue. `{exc}`")
        return

    data = load_data()
    if not data.get("players"):
        await ctx.send("No players added yet.")
        return

    results = await asyncio.to_thread(
        compute_top_champions,
        data,
        queue_ids=queue_ids,
        min_games=min_games,
        limit=12,
    )
    if not results:
        await ctx.send(f"No champion stats found for {label} with at least {min_games} games.")
        return

    def pad(text, width):
        text = str(text)
        if len(text) > width:
            return text[: width - 3] + "..."
        return text + " " * (width - len(text))

    lines = [
        f"**Top Champions - {label} (min {min_games} games)**",
        "```",
        f"{pad('Champion', 14)} | W-L   | WR%   | KDA  | Games",
        "-" * 52,
    ]
    for row in results:
        lines.append(
            f"{pad(row['champion'], 14)} | "
            f"{row['wins']}-{row['losses']:<3} | "
            f"{row['wr']:>5.1f} | "
            f"{row['kda']:>4.1f} | "
            f"{row['games']:>5}"
        )
    lines.append("```")
    await send_long(ctx, "\n".join(lines))


@bot.command()
async def debugrecentqueues(ctx, limit: int = 30):
    data = load_data()
    seen = {}

    for m in data.get("matches", {}).values():
        info = m.get("info", {})
        q = info.get("queueId")
        seen[q] = seen.get(q, 0) + 1

    lines = ["**Queue IDs currently stored:**"]
    for q, c in sorted(seen.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"{q}: {c}")

    await send_long(ctx, "\n".join(lines))


@bot.command(name="live", aliases=["livegames"])
async def live_cmd(ctx):
    data = load_data()
    live_games = await get_live_games_async(data)
    if not live_games:
        embed = discord.Embed(
            title="Live Games",
            description="No tracked players are live right now.",
            color=discord.Color.dark_grey(),
        )
        embed.set_footer(text="Live checks are cached briefly to avoid Riot API spam.")
        await ctx.send(embed=embed)
        return

    for entry in live_games:
        game = entry.get("game", {})
        tracked_names = sorted(entry.get("players", []))
        qid = int(game.get("gameQueueConfigId", 0) or 0)
        length = int(game.get("gameLength", 0) or 0)
        participants = game.get("participants", [])
        tracked_rows = []

        for tracked_name in tracked_names:
            stored = data.get("players", {}).get(tracked_name, {})
            puuid = stored.get("puuid")
            live_participant = next((p for p in participants if p.get("puuid") == puuid), None)
            if live_participant:
                champ = live_participant.get("championName") or f"Champion ID {live_participant.get('championId', '?')}"
                tracked_rows.append(f"**{tracked_name}** - {champ}")
            else:
                tracked_rows.append(f"**{tracked_name}** - champion unknown")

        embed = discord.Embed(
            title=f"Live Game - {queue_name(qid)}",
            description="\n".join(tracked_rows) or "Tracked players found, details unavailable.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Game Time", value=f"{length // 60}:{length % 60:02d}", inline=True)
        embed.add_field(name="Tracked Players", value=str(len(tracked_names)), inline=True)
        embed.set_footer(text="Cached live lookup; no repeated API spam.")
        await ctx.send(embed=embed)


# --------------------
# Season records (with LIVE GAMES)
# --------------------
@bot.command()
async def seasonrecords(ctx):
    data = load_data()
    if not data.get("players"):
        await ctx.send("No players added yet. Use `!addsummoner Name#TAG` first.")
        return

    start = SEASON_START_LOCAL
    _, end = window_3am_to_3am_local()

    rows = []
    for riot_id, p in data["players"].items():
        puuid = p.get("puuid")
        if not puuid:
            continue

        mids = data.get("player_match_index", {}).get(riot_id, [])
        matches = [data["matches"][mid] for mid in mids if mid in data.get("matches", {})]

        solo = compute_wl_kda(matches, puuid, queue_id=420, start=start, end=end)
        flex = compute_wl_kda(matches, puuid, queue_id=440, start=start, end=end)

        aram_total = {"games": 0, "wins": 0, "losses": 0, "kda": 0.0}
        aram_kda_weight = 0
        for qid in ARAM_QUEUES:
            r = compute_wl_kda(matches, puuid, queue_id=qid, start=start, end=end)
            aram_total["games"] += r["games"]
            aram_total["wins"] += r["wins"]
            aram_total["losses"] += r["losses"]
            aram_total["kda"] += r["kda"] * r["games"]
            aram_kda_weight += r["games"]

        aram_total["kda"] = (
            aram_total["kda"] / aram_kda_weight
            if aram_kda_weight > 0 else 0.0
        )

        total_games = solo["games"] + flex["games"] + aram_total["games"]

        player = data["players"][riot_id]
        solo_delta = mmr_delta_since(player, "solo", start.isoformat())
        flex_delta = mmr_delta_since(player, "flex", start.isoformat())
        mmr_delta = solo_delta + flex_delta

        rows.append((total_games, riot_id, solo, flex, aram_total, mmr_delta))

    rows.sort(key=lambda x: x[0], reverse=True)

    def wl(x): return f"{x['wins']}-{x['losses']}"
    def kda(x): return f"{x['kda']:.2f}"

    NAME_W = 26
    WL_W = 7
    KDA_W = 5
    MMR_W = 6

    def pad(s, w):
        s = str(s)
        if len(s) > w:
            return s[: w - 1] + "…"
        return s + (" " * (w - len(s)))

    # Totals
    solo_w = solo_l = flex_w = flex_l = aram_w = aram_l = 0
    for _, _, solo, flex, aram, _ in rows:
        solo_w += solo["wins"]; solo_l += solo["losses"]
        flex_w += flex["wins"]; flex_l += flex["losses"]
        aram_w += aram["wins"]; aram_l += aram["losses"]

    def weighted_avg_kda(idx):
        total_g = 0
        total_kda = 0.0
        for _, _, solo, flex, aram, _ in rows:
            x = [solo, flex, aram][idx]
            g = x["games"]
            total_g += g
            total_kda += x["kda"] * g
        return (total_kda / total_g) if total_g > 0 else 0.0

    solo_avg = weighted_avg_kda(0)
    flex_avg = weighted_avg_kda(1)
    aram_avg = weighted_avg_kda(2)

    header_title = (
        f"Season Records "
        f"({start:%b %d %I:%M%p} → {end:%b %d %I:%M%p} local)"
    )

    dash_len = (
        NAME_W
        + 3
        + (WL_W + 1 + KDA_W) * 3
        + 3
        + MMR_W
    )

    lines = [
        f"**{header_title}**",
        "```",
        pad("Player", NAME_W) + " | "
        + pad("Solo WL", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("Flex WL", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("ARAM WL", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("ΔMMR", MMR_W),
        "-" * dash_len,
    ]

    for _, riot_id, solo, flex, aram, mmr_delta in rows:
        lines.append(
            pad(riot_id, NAME_W) + " | "
            + pad(wl(solo), WL_W) + " " + pad(kda(solo), KDA_W) + " | "
            + pad(wl(flex), WL_W) + " " + pad(kda(flex), KDA_W) + " | "
            + pad(wl(aram), WL_W) + " " + pad(kda(aram), KDA_W) + " | "
            + pad(f"{mmr_delta:+}", MMR_W)
        )

    lines.append("-" * dash_len)
    lines.append(
        pad("TOTAL", NAME_W) + " | "
        + pad(f"{solo_w}-{solo_l}", WL_W) + " " + pad(f"{solo_avg:.2f}", KDA_W) + " | "
        + pad(f"{flex_w}-{flex_l}", WL_W) + " " + pad(f"{flex_avg:.2f}", KDA_W) + " | "
        + pad(f"{aram_w}-{aram_l}", WL_W) + " " + pad(f"{aram_avg:.2f}", KDA_W) + " | "
        + pad("—", MMR_W)
    )
    lines.append("```")

    live_games = await get_live_games_async(data)
    lines.append("**LIVE GAMES**")
    lines.append("```")
    if live_games:
        lines.extend(format_live_games(live_games))
    else:
        lines.append("No one in the pool is currently in-game.")
    lines.append("```")

    await send_long(ctx, "\n".join(lines))

class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    async def _update(self, interaction, mode):
        await interaction.response.defer()

        if DASHBOARD_CACHE[mode] is None:
            data = load_data()
            start, end = get_time_window(mode)
            rows = build_leaderboard_rows(data, start, end)
            DASHBOARD_CACHE[mode] = render_dashboard(rows, mode, start, end)

        content = truncate_message(DASHBOARD_CACHE[mode])

        await interaction.edit_original_response(content=content, view=self)



    @discord.ui.button(label="Daily", style=discord.ButtonStyle.secondary)
    async def daily(self, interaction, _):
        await self._update(interaction, "daily")

    @discord.ui.button(label="Weekly", style=discord.ButtonStyle.primary)
    async def weekly(self, interaction, _):
        await self._update(interaction, "weekly")

    @discord.ui.button(label="Season", style=discord.ButtonStyle.success)
    async def season(self, interaction, _):
        await self._update(interaction, "season")

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.gray, emoji="🔄")
    async def refresh(self, interaction, _):
        await self._update(interaction, "weekly")


@bot.command()
async def dashboard(ctx):
    data = load_data()
    start, end = get_time_window("season")
    rows = build_leaderboard_rows(data, start, end)
    content = render_dashboard(rows, "season", start, end)

    live = await get_live_games_async(data)
    if live:
        content += "\n**LIVE GAMES**\n```" + "\n".join(format_live_games(live)) + "```"

    await ctx.send(truncate_message(content), view=DashboardView())

# --------------------
# Run
# --------------------
def main():
    validate_runtime_config()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
