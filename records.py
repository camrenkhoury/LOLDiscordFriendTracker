from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import asyncio
update_lock = asyncio.Lock()

LOCAL_TZ = ZoneInfo("America/New_York")

SOLO_QUEUE = 420
FLEX_QUEUE = 440
ARAM_QUEUE = 450

ARAM_QUEUES = {
    450, 2400  # Standard ARAM
    # add event queues here once confirmed
}

# --------------------
# Time windows (3AM -> 3AM local)
# --------------------
def window_3am_to_3am_local(now_local=None):
    """
    Active daily window that resets at 3:00 AM local.
    Returns (start, end) where end = next 3:00 AM local.
    Window is [start, end).

    Example:
      - If now is 1:15 AM Jan 23: start=Jan 22 3:00 AM, end=Jan 23 3:00 AM
      - If now is 5:00 AM Jan 23: start=Jan 23 3:00 AM, end=Jan 24 3:00 AM
    """
    now_local = now_local or datetime.now(LOCAL_TZ)

    today_3am = now_local.replace(hour=3, minute=0, second=0, microsecond=0)

    if now_local >= today_3am:
        start = today_3am
        end = today_3am + timedelta(days=1)
    else:
        start = today_3am - timedelta(days=1)
        end = today_3am

    return start, end
# --------------------
# Match helpers
# --------------------
def _participant_for_puuid(match, puuid):
    parts = match.get("info", {}).get("participants", [])
    return next((p for p in parts if p.get("puuid") == puuid), None)

def _queue_id(match):
    return match.get("info", {}).get("queueId")

def _game_start_local(m):
    info = m.get("info", {})

    ts = (
        info.get("gameStartTimestamp")
        or info.get("gameCreation")
        or info.get("gameEndTimestamp")
    )

    if not ts:
        # LAST RESORT: allow match ingestion even if timing is unknown
        return None

    # Riot timestamps are ms
    from datetime import datetime
    import pytz

    dt_utc = datetime.utcfromtimestamp(ts / 1000).replace(tzinfo=pytz.UTC)
    return dt_utc.astimezone()

# --------------------
# Core stat computation
# --------------------
def compute_wl_kda(matches, puuid, queue_id=None, start=None, end=None):
    """
    Computes W-L and KDA for a given puuid across matches.
    - If start/end provided, they should be timezone-aware datetimes in LOCAL_TZ (recommended).
    - Window is [start, end).
    """
    wins = losses = 0
    k = d = a = 0
    games = 0
    ARAM_QUEUE = -1  # logical ARAM bucket
    
    for m in matches:
        t = _game_start_local(m)
        if t is None:
            continue

        if start and t < start:
            continue
        if end and t >= end:
            continue
        qid = _queue_id(m)

        if queue_id == ARAM_QUEUE and qid not in ARAM_QUEUES:
            continue
        elif queue_id is not None and queue_id != ARAM_QUEUE and qid != queue_id:
            continue

        me = _participant_for_puuid(m, puuid)
        if not me:
            continue

        games += 1
        if me.get("win"):
            wins += 1
        else:
            losses += 1

        k += int(me.get("kills", 0))
        d += int(me.get("deaths", 0))
        a += int(me.get("assists", 0))

    kda = (k + a) / max(1, d) if games > 0 else 0.0
    return {"games": games, "wins": wins, "losses": losses, "kda": kda}

# --------------------
# Season + Flex 5-stack stats
# --------------------

# Season start: Jan 8 @ 3:00 AM local (adjust year if needed)
SEASON_START_LOCAL = datetime(2026, 1, 8, 3, 0, 0, tzinfo=LOCAL_TZ)

def compute_top_flex_stacks(data, top_n=5, season_start_local=SEASON_START_LOCAL):
    """
    Top flex 5-stacks among your player pool since season_start_local.

    Rules:
    - Only counts Flex queue matches (queueId=440)
    - Only counts matches where EXACTLY 5 players from your pool are on the SAME TEAM
    - Returns top_n stacks sorted by winrate desc, then games desc

    data is your league.json dict with:
      data["players"][riot_id]["puuid"]
      data["matches"][match_id] = match json
    """
    # Map puuid -> riot_id for pool membership checks
    puuid_to_riot = {}
    for riot_id, p in data.get("players", {}).items():
        puuid = p.get("puuid")
        if puuid:
            puuid_to_riot[puuid] = riot_id

    stacks = {}  # "A,B,C,D,E" -> {wins, losses, games}

    for mid, m in data.get("matches", {}).items():
        info = m.get("info", {})
        if info.get("queueId") != FLEX_QUEUE:
            continue

        t_local = _game_start_local(m)
        if not t_local or t_local < season_start_local:
            continue

        parts = info.get("participants", [])
        if not parts:
            continue

        # teamId -> list of riot_ids from your pool on that team
        team_members = {}
        team_win = {}

        for p in parts:
            puuid = p.get("puuid")
            if puuid not in puuid_to_riot:
                continue

            team_id = p.get("teamId")
            if team_id is None:
                continue

            team_members.setdefault(team_id, []).append(puuid_to_riot[puuid])
            if team_id not in team_win:
                team_win[team_id] = bool(p.get("win", False))

        # Count only teams with exactly 5 pool players
        for team_id, riot_ids in team_members.items():
            if len(riot_ids) != 5:
                continue

            stack_key = ",".join(sorted(riot_ids))
            rec = stacks.setdefault(stack_key, {"wins": 0, "losses": 0, "games": 0})
            rec["games"] += 1
            if team_win.get(team_id, False):
                rec["wins"] += 1
            else:
                rec["losses"] += 1

    out = []
    for stack, rec in stacks.items():
        games = rec["games"]
        wins = rec["wins"]
        losses = rec["losses"]
        wr = (wins / games * 100.0) if games else 0.0
        out.append({"stack": stack, "wins": wins, "losses": losses, "games": games, "wr": wr})

    out.sort(key=lambda x: (x["wr"], x["games"]), reverse=True)
    unique_stacks = len(out)

    at_least_3 = [r for r in out if r["games"] >= 3]
    under_3 = [r for r in out if r["games"] < 3]

    # Sort both groups the same way (wr desc, games desc)
    at_least_3.sort(key=lambda x: (x["wr"], x["games"]), reverse=True)
    under_3.sort(key=lambda x: (x["wr"], x["games"]), reverse=True)

    top = at_least_3[:top_n]
    if len(top) < top_n:
        top.extend(under_3[: (top_n - len(top))])

    return top, unique_stacks