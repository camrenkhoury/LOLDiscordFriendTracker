# mmrupdate.py

from datetime import datetime, timezone
from storage import now_utc_iso

# --------------------
# Rank → MMR mapping
# --------------------

TIER_BASE = {
    "IRON": 800,
    "BRONZE": 950,
    "SILVER": 1100,
    "GOLD": 1250,
    "PLATINUM": 1450,
    "EMERALD": 1600,
    "DIAMOND": 1750,
    "MASTER": 2000,
    "GRANDMASTER": 2150,
    "CHALLENGER": 2300,
}

DIV_OFFSET = {
    "IV": 0,
    "III": 100,
    "II": 200,
    "I": 300,
}

QUEUE_MAP = {
    "RANKED_SOLO_5x5": "solo",
    "RANKED_FLEX_SR": "flex",
}

# --------------------
# Core helpers
# --------------------


from riot import get_player_profile

def update_all_mmrs(data):
    for riot_id, player in data.get("players", {}).items():
        game_name = player.get("game_name")
        tag_line = player.get("tag_line")

        if not game_name or not tag_line:
            continue

        try:
            profile = get_player_profile(game_name, tag_line)
        except Exception as e:
            print("[MMR] profile fetch failed:", riot_id, e)
            continue

        update_player_mmr_from_profile(player, profile)

def estimate_mmr_from_rank(entry):
    tier = entry.get("tier")
    div = entry.get("rank")
    lp = entry.get("leaguePoints", 0)

    if not tier or not div:
        return None

    base = TIER_BASE.get(tier, 1000)
    offset = DIV_OFFSET.get(div, 0)
    return base + offset + lp


def _ensure_mmr_struct(player):
    mmr = player.setdefault("mmr", {})
    for q in ("solo", "flex"):
        mmr.setdefault(q, {"current": None, "history": []})
    return mmr


def record_mmr_snapshot(player, queue, mmr_value):
    mmr = _ensure_mmr_struct(player)
    now = now_utc_iso()

    mmr[queue]["current"] = mmr_value
    mmr[queue]["history"].append([now, mmr_value])

    # Keep history bounded (last 90 days-ish)
    if len(mmr[queue]["history"]) > 500:
        mmr[queue]["history"] = mmr[queue]["history"][-500:]


# --------------------
# Public API
# --------------------

def update_player_mmr_from_profile(player, profile):
    """
    Called after get_player_profile()
    """
    for entry in profile.get("ranked_entries", []):
        q = QUEUE_MAP.get(entry.get("queueType"))
        if not q:
            continue

        mmr = estimate_mmr_from_rank(entry)
        if mmr is not None:
            record_mmr_snapshot(player, q, mmr)


def mmr_delta_since(player, queue, start_iso):
    """
    Returns net MMR change since start_iso.
    Suppresses artificial drops caused by tier promotions.
    """
    mmr = player.get("mmr", {}).get(queue)
    if not mmr or len(mmr["history"]) < 2:
        return 0

    start = datetime.fromisoformat(start_iso)
    history = mmr["history"]

    base_val = None
    latest_val = history[-1][1]

    for ts, val in history:
        if datetime.fromisoformat(ts) >= start:
            base_val = val
            break

    if base_val is None:
        return 0

    delta = latest_val - base_val

    # ---- PROMOTION GUARD ----
    # Promotions cause artificial negative deltas because tier bases jump.
    # A large negative swing on a winning period is invalid → suppress.
    if delta < 0 and abs(delta) >= 100:
        return 0

    return delta

