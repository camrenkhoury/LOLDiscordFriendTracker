import time
import discord
from discord.ext import commands
from datetime import timedelta

from storage import load_data
from records import (
    compute_wl_kda,
    window_3am_to_3am_local,
    SEASON_START_LOCAL,
    ARAM_QUEUES,
)
from mmrupdate import mmr_delta_since
from live import get_live_games, format_live_games


# --------------------
# LIVE CACHE (prevents 429s)
# --------------------

_LIVE_CACHE = {"ts": 0.0, "data": None}
LIVE_CACHE_TTL = 60


def get_live_games_cached(data):
    now = time.time()
    if _LIVE_CACHE["data"] and now - _LIVE_CACHE["ts"] < LIVE_CACHE_TTL:
        return _LIVE_CACHE["data"]

    live = get_live_games(data)
    _LIVE_CACHE["data"] = live
    _LIVE_CACHE["ts"] = now
    return live


# --------------------
# Helpers
# --------------------

def build_time_window(mode: str):
    if mode == "daily":
        start, end = window_3am_to_3am_local()
    elif mode == "weekly":
        _, end = window_3am_to_3am_local()
        start = end - timedelta(days=7)
    else:
        start = SEASON_START_LOCAL
        _, end = window_3am_to_3am_local()
    return start, end


def winrate_pct(solo, flex, aram):
    wins = solo["wins"] + flex["wins"] + aram["wins"]
    games = solo["games"] + flex["games"] + aram["games"]
    return (wins / games * 100) if games else 0.0


MODE_COLOR = {
    "daily": 0x57F287,
    "weekly": 0x5865F2,
    "season": 0xFAA61A,
}


MODE_TITLE = {
    "daily": "📅 Daily Dashboard",
    "weekly": "📊 Weekly Dashboard",
    "season": "🏆 Season Dashboard",
}


# --------------------
# Embed builder
# --------------------

def build_dashboard_embed(mode: str):
    data = load_data()
    start, end = build_time_window(mode)

    embed = discord.Embed(
        title="League of Legends Group Tracker",
        description=(
            f"**{MODE_TITLE[mode]}**\n"
            f"{start:%b %d %I:%M%p} → {end:%b %d %I:%M%p} local"
        ),
        color=MODE_COLOR[mode],
    )

    rows = []
    for riot_id, p in data.get("players", {}).items():
        puuid = p.get("puuid")
        if not puuid:
            continue

        mids = data.get("player_match_index", {}).get(riot_id, [])
        matches = [data["matches"][m] for m in mids if m in data["matches"]]

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

        mmr = (
            mmr_delta_since(p, "solo", start.isoformat())
            + mmr_delta_since(p, "flex", start.isoformat())
        )

        wr = winrate_pct(solo, flex, aram)
        games = solo["games"] + flex["games"] + aram["games"]

        if games == 0:
            continue

        rows.append((riot_id, solo, flex, aram, mmr, wr, games))

    rows.sort(key=lambda r: (r[5], r[4]), reverse=True)

    embed.add_field(
        name="Leaderboard",
        value="Sorted by **Winrate → MMR**",
        inline=False,
    )

    # Safe: up to 10 players
    for riot_id, solo, flex, aram, mmr, wr, games in rows[:10]:
        embed.add_field(
            name=f"👤 {riot_id}",
            value=(
                f"**WR:** `{wr:.1f}%` • **ΔMMR:** `{mmr:+}` • **Games:** {games}\n"
                f"Solo {solo['wins']}-{solo['losses']} ({solo['kda']:.2f}) • "
                f"Flex {flex['wins']}-{flex['losses']} • "
                f"ARAM {aram['wins']}-{aram['losses']}"
            ),
            inline=False,
        )

    live = get_live_games_cached(data)
    if live:
        embed.add_field(
            name="🟢 LIVE GAMES",
            value="\n".join(format_live_games(live)[:5]),
            inline=False,
        )

    embed.set_footer(text="Daily • Weekly • Season pages | Cached live data")
    return embed


# --------------------
# View (Pages)
# --------------------

class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _update(self, interaction, mode):
        try:
            await interaction.response.edit_message(
                embed=build_dashboard_embed(mode),
                view=self,
            )
        except discord.NotFound:
            pass

    @discord.ui.button(label="Daily", style=discord.ButtonStyle.secondary)
    async def daily(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update(interaction, "daily")

    @discord.ui.button(label="Weekly", style=discord.ButtonStyle.primary)
    async def weekly(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update(interaction, "weekly")

    @discord.ui.button(label="Season", style=discord.ButtonStyle.success)
    async def season(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update(interaction, "season")


# --------------------
# Command
# --------------------

def setup(bot: commands.Bot):
    @bot.command(name="dashboarddemo")
    async def dashboarddemo(ctx):
        await ctx.send(
            embed=build_dashboard_embed("weekly"),
            view=DashboardView(),
        )
