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
    "III": 50,
    "II": 100,
    "I": 150,
}

QUEUE_MAP = {
    "RANKED_SOLO_5x5": "solo",
    "RANKED_FLEX_SR": "flex",
}

# --------------------
# Core helpers
# --------------------

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
    """
    mmr = player.get("mmr", {}).get(queue)
    if not mmr or not mmr["history"]:
        return 0

    start = datetime.fromisoformat(start_iso)
    base = None
    latest = mmr["history"][-1][1]

    for ts, val in mmr["history"]:
        if datetime.fromisoformat(ts) >= start:
            base = val
            break

    if base is None:
        return 0

    return latest - base
