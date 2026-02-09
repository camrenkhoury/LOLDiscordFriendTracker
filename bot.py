# bot.py
import asyncio
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from grieftracker import evaluate_grieftracker

from mmrupdate import (
    update_player_mmr_from_profile,
    mmr_delta_since,
)

from mmrupdate import update_all_mmrs


from analytics import compute_top_duos
from storage import load_data, save_data, upsert_player, now_utc_iso
from records import (
    window_3am_to_3am_local,
    compute_wl_kda,
    compute_top_flex_stacks,
    SEASON_START_LOCAL,
    _game_start_local,
    ARAM_QUEUES,
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

from config import DISCORD_TOKEN, COMMAND_PREFIX, TEST_CHANNEL_ID

# --------------------
# Globals
# --------------------
update_lock = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

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
                    get_match_ids_by_puuid, puuid, 25
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

    win = game["win"]

    # ---- CLASSIFICATION ----
    if win:
        if team_impact < 5 and player_negative <= 5:
            return "CAKE WALK", "🟢"
        if team_impact >= 15 and player_positive > player_negative:
            return "HARD CARRY", "🟡"
        if player_negative > player_positive:
            return "BOOSTED", "🔵"
        return "FAIR WIN", "🟢"


    else:  # LOSS
        if team_impact >= 15 and player_positive > player_negative:
            return "GRIEFED", "🔴"

        if player_negative > player_positive:
            return "INTER", "⚫"

        if team_impact < 10 and player_negative <= 5:
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
    print(f"Command error: {repr(error)}")

# --------------------
# Player management
# --------------------
@bot.command()
async def addsummoner(ctx, *, riot_id: str):
    """
    Usage: !addsummoner Name#TAG
    """
    if "#" not in riot_id:
        await ctx.send("❌ Use format: Name#TAG (example: SomeName#NA1)")
        return

    game_name, tag_line = riot_id.split("#", 1)

    try:
        info = await asyncio.to_thread(get_player_profile, game_name, tag_line)
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
        encrypted_summoner_id=None,
    )

    update_player_mmr_from_profile(
    data["players"][riot_key],
    info
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


@bot.command(name="grieftracker")
async def grieftracker_cmd(ctx, *, riot_id: str):
    await ctx.typing()

    try:
        data = load_data()

        # --------------------
        # Validation
        # --------------------
        if riot_id not in data.get("players", {}):
            await ctx.send("Player not found. Use `!addsummoner Name#TAG` first.")
            return

        player_entry = data["players"][riot_id]
        puuid = player_entry.get("puuid")

        match_ids = data.get("player_match_index", {}).get(riot_id, [])
        matches = [
            data["matches"][mid]
            for mid in match_ids
            if mid in data.get("matches", {})
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
        if summary.get("LOST CAUSE", 0) >= 5:
            lines.append("☠️ **STATISTICAL ANOMALY** — outcomes far outside expected variance")

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
            worst = max(innocent_losses, key=lambda x: x["game_grief_points"])

            lines.append("")
            lines.append("**Most Innocent LOSS:**")
            lines.append(
                f"• **{worst['game_grief_points']} grief points** — "
                f"Team Death/min: {worst['team_dpm']} | "
                f"You: {worst['player_dpm']}"
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
        await ctx.send("❌ Use format: Name#TAG")
        return

    await ctx.typing()
    game_name, tag_line = riot_id.split("#", 1)

    try:
        info = await asyncio.to_thread(get_player_profile, game_name, tag_line)
    except Exception as e:
        await ctx.send("❌ Failed to fetch player info.")
        print(e)
        return
    
    data = load_data()
    riot_key = f"{info['game_name']}#{info['tag_line']}"

    if riot_key in data["players"]:
        update_player_mmr_from_profile(
            data["players"][riot_key],
            info
        )

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
    await ctx.send(msg[:1900])

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

        rows.append((
            total_games,
            riot_id,
            solo,
            flex,
            aram_total,
            mmr_delta,
        ))

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
        f"Daily Records "
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

    live_games = get_live_games(data)
    lines.append("**LIVE GAMES**")
    lines.append("```")
    if live_games:
        lines.extend(format_live_games(live_games))
    else:
        lines.append("No one in the pool is currently in-game.")
    lines.append("```")

    await ctx.send("\n".join(lines)[:1900])

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

    live_games = get_live_games(data)
    lines.append("**LIVE GAMES**")
    lines.append("```")
    if live_games:
        lines.extend(format_live_games(live_games))
    else:
        lines.append("No one in the pool is currently in-game.")
    lines.append("```")

    await ctx.send("\n".join(lines)[:1900])


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
    await ctx.send("\n".join(lines)[:1900])

@bot.command()
async def topduos(ctx):
    if update_lock.locked():
        await ctx.send("⚠️ Update in progress.")
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
        lines.append(
            f"{r['duo']}  {r['wins']}-{r['games'] - r['wins']} "
            f"({r['wr']:.1f}%) [{r['games']}g]"
        )
    lines.append("```")
    await ctx.send("\n".join(lines)[:1900])

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

    await ctx.send("\n".join(lines)[:1900])

# --------------------
# Run
# --------------------
bot.run(DISCORD_TOKEN)
