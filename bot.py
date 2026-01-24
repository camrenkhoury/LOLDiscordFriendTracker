# bot.py
import asyncio
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from analytics import compute_top_duos
from storage import load_data, save_data, upsert_player, now_utc_iso
from records import (
    window_3am_to_3am_local,
    compute_wl_kda,
    compute_top_flex_stacks,
    SEASON_START_LOCAL,
    _game_start_local,
    ARAM_QUEUES,  # must be a set/list of queueIds (e.g., {450, ...})
)
from live import get_live_games, format_live_games
from riot import (
    get_player_profile,
    get_summoner_by_puuid,     # REQUIRED for encryptedSummonerId backfill + addsummoner
    get_match_ids_by_puuid,
    get_match,
    compute_recent_kda,
    solo_top_champs_wl,
    get_top_mastery_by_riot_id,
)
from config import DISCORD_TOKEN, COMMAND_PREFIX, TEST_CHANNEL_ID


# --------------------
# Globals
# --------------------
update_lock = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in Discord Developer Portal
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


# --------------------
# Core incremental update
# --------------------
async def incremental_update_core(ctx=None, notify_channel_id: int | None = None):
    """
    Incremental update:
      - fills missing match JSON for already-indexed match IDs
      - fetches latest match IDs per player and pulls any new match JSON
      - respects a cutoff (daily window start - 6 hours) to avoid deep history on hourly runs
    """
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

            # ---- Step 1: fill any match JSON missing for already-known IDs ----
            for mid in list(known_ids):
                if mid in data.get("matches", {}):
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

            # ---- Step 2: fetch recent match IDs and pull new ones ----
            try:
                match_ids = await asyncio.to_thread(get_match_ids_by_puuid, puuid, 25)
            except Exception as e:
                print("[match ids] failed:", riot_id, e)
                errors += 1
                continue

            for mid in match_ids:
                # already have JSON: just ensure index includes it
                if mid in data.get("matches", {}):
                    if mid not in known_ids:
                        data["player_match_index"][riot_id].append(mid)
                        known_ids.add(mid)
                    continue

                # fetch detail
                try:
                    m = await asyncio.to_thread(get_match, mid)
                except Exception as e:
                    print("[match detail] failed:", riot_id, mid, e)
                    errors += 1
                    continue

                t_local = _game_start_local(m)
                if t_local and t_local < cutoff_local:
                    break  # older than relevant range for hourly updates

                data["matches"][mid] = m
                if mid not in known_ids:
                    data["player_match_index"][riot_id].append(mid)
                    known_ids.add(mid)
                new_matches += 1

            # persist per-player to be resilient
            save_data(data)

        data["last_update_utc"] = now_utc_iso()
        save_data(data)

        if ctx:
            await ctx.send(
                f"✅ Update complete. New: **{new_matches}**, Filled: **{filled_missing}**, Errors: **{errors}**."
            )

        if notify_channel_id:
            ch = bot.get_channel(notify_channel_id)
            if ch:
                await ch.send(
                    f"⏱️ Hourly update complete — new: {new_matches}, filled: {filled_missing}, errors: {errors}"
                )


# --------------------
# Background hourly task
# --------------------
@tasks.loop(hours=1)
async def hourly_update_task():
    await incremental_update_core(notify_channel_id=TEST_CHANNEL_ID)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not hourly_update_task.is_running():
        hourly_update_task.start()


@bot.event
async def on_command_error(ctx, error):
    # Surface issues in logs during development
    print(f"Command error: {repr(error)}")


# --------------------
# Admin / maintenance
# --------------------
@bot.command()
@commands.is_owner()
async def backfill_encrypted_ids(ctx):
    """
    One-time migration:
    Fill encryptedSummonerId ("encrypted_id") for all players in league.json.
    Required for Spectator / LIVE GAMES.
    """
    data = load_data()
    if not data.get("players"):
        await ctx.send("No players in pool.")
        return

    updated = 0
    skipped = 0
    errors = 0

    await ctx.send("🔄 Backfilling encrypted summoner IDs...")

    for riot_id, p in data["players"].items():
        if p.get("encrypted_id"):
            skipped += 1
            continue

        puuid = p.get("puuid")
        if not puuid:
            errors += 1
            continue

        try:
            summ = await asyncio.to_thread(get_summoner_by_puuid, puuid)
            enc = summ.get("id")
            if not enc:
                errors += 1
                continue
            p["encrypted_id"] = enc
            updated += 1
        except Exception as e:
            print("[BACKFILL ERROR]", riot_id, e)
            errors += 1

    save_data(data)
    await ctx.send(f"✅ Backfill complete — Updated: **{updated}**, Skipped: **{skipped}**, Errors: **{errors}**.")


# --------------------
# Player management
# --------------------
@bot.command()
async def addsummoner(ctx, riot_id: str):
    """
    Usage: !addsummoner Name#TAG
    """
    if "#" not in riot_id:
        await ctx.send("❌ Use format: Name#TAG (example: SomeName#NA1)")
        return

    game_name, tag_line = riot_id.split("#", 1)

    try:
        info = await asyncio.to_thread(get_player_profile, game_name, tag_line)
        summ = await asyncio.to_thread(get_summoner_by_puuid, info["puuid"])
        encrypted_id = summ.get("id")
    except Exception as e:
        await ctx.send("❌ Could not verify player with Riot API.")
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
        encrypted_id,
    )
    save_data(data)

    await ctx.send(f"✅ Added: **{riot_key}**")


@bot.command()
async def playerlist(ctx):
    data = load_data()
    players = list(data.get("players", {}).keys())
    if not players:
        await ctx.send("No players added yet. Use `!addsummoner Name#TAG`.")
        return
    msg = "**Player Pool:**\n" + "\n".join(f"- {p}" for p in players)
    await ctx.send(msg[:1900])


# --------------------
# Player info
# --------------------
@bot.command()
async def playerinfo(ctx, riot_id: str):
    """
    Usage: !playerinfo Name#TAG
    """
    if "#" not in riot_id:
        await ctx.send("❌ Use format: Name#TAG (example: SomeName#NA1)")
        return

    await ctx.typing()
    game_name, tag_line = riot_id.split("#", 1)

    try:
        info = await asyncio.to_thread(get_player_profile, game_name, tag_line)
    except Exception as e:
        await ctx.send("❌ Failed to fetch player info. Check name, tag, or API key.")
        print(e)
        return

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

    # Recent KDA
    try:
        kda = await asyncio.to_thread(compute_recent_kda, puuid, 8)
        recent_kda_line = (
            f"Recent KDA (last {kda['games']}): {kda['kda']:.2f} "
            f"({kda['kills']}/{kda['deaths']}/{kda['assists']})"
        )
    except Exception as e:
        recent_kda_line = "Recent KDA: (failed to load)"
        print(e)

    # Top mastery (may be blocked in your environment)
    try:
        mastery = await asyncio.to_thread(get_top_mastery_by_riot_id, game_name, tag_line, 5)
        mastery_lines = "\n".join(
            [f"{i+1}) {m['champion']} — M{m['level']} — {m['points']:,} pts" for i, m in enumerate(mastery)]
        )
    except Exception as e:
        mastery_lines = "(unavailable — mastery requires summoner lookup that is currently forbidden)"
        print("MASTERY ERROR:", repr(e))

    # Top solo champs W-L
    try:
        solo_champs = await asyncio.to_thread(solo_top_champs_wl, puuid, 20, 5, 420)
        solo_champ_lines = "\n".join(
            [
                f"{c['champion']} — {c['wins']}-{c['losses']} ({c['wr']:.1f}%) — {c['games']} games"
                for c in solo_champs
            ]
        ) or "(no Solo/Duo games found)"
    except Exception as e:
        solo_champ_lines = "(failed to load)"
        print(e)

    msg = (
        f"**{info['game_name']}#{info['tag_line']}**\n"
        f"Level: {info['summoner_level']}\n"
        f"{solo_line}\n"
        f"{flex_line}\n\n"
        f"{recent_kda_line}\n\n"
        f"**Top 5 Mastery**\n{mastery_lines}\n\n"
        f"**Top 5 Solo/Duo Champs (last 20 games)**\n{solo_champ_lines}"
    )
    await ctx.send(msg[:1900])


# --------------------
# Updates
# --------------------
@bot.command()
async def updaterecords(ctx):
    await incremental_update_core(ctx=ctx)


@bot.command()
async def updateseason(ctx):
    """
    Season backfill from SEASON_START_LOCAL; safe to re-run.
    """
    if update_lock.locked():
        await ctx.send("⚠️ Update already running.")
        return

    async with update_lock:
        await ctx.send(f"⏳ Season backfill starting (since {SEASON_START_LOCAL:%b %d %I:%M%p} local)...")

        data = load_data()
        if not data.get("players"):
            await ctx.send("No players added yet. Use `!addsummoner Name#TAG` first.")
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

            # Fill missing JSON for indexed IDs
            missing_ids = [mid for mid in data["player_match_index"][riot_id] if mid not in data.get("matches", {})]
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
            page_size = 100  # max allowed by Match-V5

            while True:
                try:
                    ids = await asyncio.to_thread(get_match_ids_by_puuid, puuid, page_size, None, start_idx)
                except Exception as e:
                    print("match id fetch failed:", riot_id, e)
                    errors += 1
                    break

                if not ids:
                    break

                stop = False
                for mid in ids:
                    if mid in data.get("matches", {}):
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
            f"✅ Season backfill complete. New matches stored: **{new_matches}**. "
            f"Filled missing: **{filled_missing}**. Errors: **{errors}**."
        )


# --------------------
# Daily records (with LIVE GAMES)
# --------------------
@bot.command()
async def dailyrecords(ctx):
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

        solo = compute_wl_kda(matches, puuid, queue_id=420, start=start, end=end)
        flex = compute_wl_kda(matches, puuid, queue_id=440, start=start, end=end)

        # ARAM: handle multiple queue IDs. If compute_wl_kda only accepts one queue_id,
        # we sum over all ARAM queues.
        aram_total = {"games": 0, "wins": 0, "losses": 0, "kda": 0.0}
        aram_kda_weight = 0
        for qid in ARAM_QUEUES:
            r = compute_wl_kda(matches, puuid, queue_id=qid, start=start, end=end)
            aram_total["games"] += r["games"]
            aram_total["wins"] += r["wins"]
            aram_total["losses"] += r["losses"]
            aram_total["kda"] += r["kda"] * r["games"]
            aram_kda_weight += r["games"]
        aram_total["kda"] = (aram_total["kda"] / aram_kda_weight) if aram_kda_weight > 0 else 0.0

        total_games = solo["games"] + flex["games"] + aram_total["games"]
        rows.append((total_games, riot_id, solo, flex, aram_total))

    rows.sort(key=lambda x: x[0], reverse=True)

    def wl(x): return f"{x['wins']}-{x['losses']}"
    def kda(x): return f"{x['kda']:.2f}"

    NAME_W = 26
    WL_W = 7
    KDA_W = 5

    def pad(s, w):
        s = str(s)
        if len(s) > w:
            return s[: w - 1] + "…"
        return s + (" " * (w - len(s)))

    # Totals
    solo_w = solo_l = flex_w = flex_l = aram_w = aram_l = 0

    for _, _, solo, flex, aram in rows:
        solo_w += solo["wins"]; solo_l += solo["losses"]
        flex_w += flex["wins"]; flex_l += flex["losses"]
        aram_w += aram["wins"]; aram_l += aram["losses"]

    def weighted_avg_kda(idx):
        total_g = 0
        total_kda = 0.0
        for _, _, solo, flex, aram in rows:
            x = [solo, flex, aram][idx]
            g = x["games"]
            total_g += g
            total_kda += x["kda"] * g
        return (total_kda / total_g) if total_g > 0 else 0.0

    solo_avg = weighted_avg_kda(0)
    flex_avg = weighted_avg_kda(1)
    aram_avg = weighted_avg_kda(2)

    # Build message
    header_title = f"Daily Records ({start:%b %d %I:%M%p} → {end:%b %d %I:%M%p} local)"
    lines = [f"**{header_title}**", "```"]
    lines.append(
        pad("Player", NAME_W) + " | "
        + pad("Solo WL", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("Flex WL", WL_W) + " " + pad("KDA", KDA_W) + " | "
        + pad("ARAM WL", WL_W) + " " + pad("KDA", KDA_W)
    )
    lines.append("-" * (NAME_W + 3 + (WL_W + 1 + KDA_W) * 3 + 6))

    for _, riot_id, solo, flex, aram in rows:
        lines.append(
            pad(riot_id, NAME_W) + " | "
            + pad(wl(solo), WL_W) + " " + pad(kda(solo), KDA_W) + " | "
            + pad(wl(flex), WL_W) + " " + pad(kda(flex), KDA_W) + " | "
            + pad(wl(aram), WL_W) + " " + pad(kda(aram), KDA_W)
        )

    lines.append("-" * (NAME_W + 3 + (WL_W + 1 + KDA_W) * 3 + 6))
    lines.append(
        pad("TOTAL", NAME_W) + " | "
        + pad(f"{solo_w}-{solo_l}", WL_W) + " " + pad(f"{solo_avg:.2f}", KDA_W) + " | "
        + pad(f"{flex_w}-{flex_l}", WL_W) + " " + pad(f"{flex_avg:.2f}", KDA_W) + " | "
        + pad(f"{aram_w}-{aram_l}", WL_W) + " " + pad(f"{aram_avg:.2f}", KDA_W)
    )
    lines.append("```")

    # LIVE GAMES section (separate block so formatting is stable)
    live_games = get_live_games(data)
    if live_games:
        lines.append("**LIVE GAMES**")
        lines.append("```")
        lines.extend(format_live_games(live_games))
        lines.append("```")

    await ctx.send("\n".join(lines)[:1900])


# --------------------
# Analytics commands
# --------------------
@bot.command()
async def topflexstacks(ctx):
    data = load_data()
    if not data.get("players"):
        await ctx.send("No players added yet. Use `!addsummoner Name#TAG` first.")
        return

    try:
        top, unique_count = await asyncio.to_thread(compute_top_flex_stacks, data, 5, SEASON_START_LOCAL)
    except Exception as e:
        await ctx.send("❌ Failed to compute flex stacks.")
        print(e)
        return

    if not top:
        await ctx.send("No qualifying 5-stacks found in Flex since season start.")
        return

    lines = [
        f"**Top 5 Flex 5-Stacks (since {SEASON_START_LOCAL:%b %d})**",
        f"Total number of unique stack combinations: **{unique_count}**",
        "```",
    ]
    for i, r in enumerate(top, 1):
        lines.append(f"{i}) {r['stack']}  {r['wins']}-{r['losses']}  ({r['wr']:.1f}%)  [{r['games']}g]")
    lines.append("```")
    await ctx.send("\n".join(lines)[:1900])


@bot.command()
async def topduos(ctx):
    if update_lock.locked():
        await ctx.send("⚠️ Update in progress. Try again shortly.")
        return

    data = load_data()
    if not data.get("players"):
        await ctx.send("No players added yet.")
        return

    results = await asyncio.to_thread(compute_top_duos, data, 5, 420)
    if not results:
        await ctx.send("No qualifying duos found.")
        return

    lines = ["**Top Duos (Solo/Duo)**", "```"]
    for r in results[:10]:
        lines.append(f"{r['duo']}  {r['wins']}-{r['games'] - r['wins']}  ({r['wr']:.1f}%) [{r['games']}g]")
    lines.append("```")
    await ctx.send("\n".join(lines)[:1900])


# --------------------
# Debug helpers
# --------------------
@bot.command()
async def debugplayer(ctx, riot_id: str):
    data = load_data()
    mids = data.get("player_match_index", {}).get(riot_id, [])
    await ctx.send(f"**{riot_id}** stored match IDs: **{len(mids)}**")


@bot.command()
async def debugqueues(ctx, riot_id: str):
    data = load_data()
    p = data.get("players", {}).get(riot_id)
    if not p:
        await ctx.send("Player not found in pool.")
        return

    mids = data.get("player_match_index", {}).get(riot_id, [])
    counts = {}
    for mid in mids:
        m = data.get("matches", {}).get(mid)
        if not m:
            continue
        q = m.get("info", {}).get("queueId")
        counts[q] = counts.get(q, 0) + 1

    lines = [f"**{riot_id} queueId counts (stored):**"]
    for q, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"{q}: {c}")
    await ctx.send("\n".join(lines)[:1900])


@bot.command()
async def debugpoolqueues(ctx):
    data = load_data()
    counts = {}
    for m in data.get("matches", {}).values():
        q = m.get("info", {}).get("queueId")
        counts[q] = counts.get(q, 0) + 1

    lines = ["**Stored queueId counts (all matches in league.json):**", "```"]
    for q, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"{q}: {c}")
    lines.append("```")
    await ctx.send("\n".join(lines)[:1900])

@bot.command()
async def livedebug(ctx):
    data = load_data()
    live_games = get_live_games(data)
    await ctx.send(f"live_games_found={len(live_games)}")


# --------------------
# Run
# --------------------
bot.run(DISCORD_TOKEN)
