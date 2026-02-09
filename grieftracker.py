# Computes a cumulative Grief Index over recent ranked solo/duo matches

from statistics import mean

from rank_baselines import (
    VISION_PER_MIN,
    TEAM_DPM,
    OBJ_PARTICIPATION,
    DAMAGE_SHARE,
    CS_PER_MIN,
    SUPPORT_VISION_PER_MIN,
    KILL_PARTICIPATION,
    DEATH_RATE,
    OUTLIER_MULTIPLIER,
)

def wr_bar(wr: float):
    filled = min(10, max(0, int(round(wr / 10))))
    return "▓" * filled + "░" * (10 - filled)

def rank_icon(tier: str | None):
    if not tier:
        return "⚫"

    tier = tier.upper()
    return {
        "IRON": "⬛",
        "BRONZE": "🟤",
        "SILVER": "⚪",
        "GOLD": "🟡",
        "PLATINUM": "🔵",
        "EMERALD": "🟢",
        "DIAMOND": "🔷",
        "MASTER": "🟣",
        "GRANDMASTER": "🔴",
        "CHALLENGER": "⭐",
    }.get(tier, "⚫")





# -----------------------------
# Tunable constants
# -----------------------------

LOSS_AMPLIFIER = 1.25
WIN_AMPLIFIER = 0.75

MIN_GAME_SCORE = -50

EXPECTED_GAME_MINUTES = 30

# Weights
TDB_WEIGHT = 40
TDO_WEIGHT = 25
PRPB_WEIGHT = 30
OD_WEIGHT = 35
BOOSTED_WEIGHT = 45

# Tank detection thresholds
TANK_HP_PER_MIN = 95
TANK_ARMOR_PER_MIN = 3.0

# Vision
VISION_GRIEF_WEIGHT = 8
VISION_SUPPORT_SELF_PENALTY = -10

# Team collapse
TEAM_COLLAPSE_WEIGHT = 70
PLAYER_CLEAN_EARLY_BONUS = 35
TEAM_VS_PLAYER_DEATH_RATIO = 2.5

HARD_CARRY_BONUS = -30

RANKED_SOLO_QUEUE_ID = 420

# AFK penalties
AFK_LATE_PENALTY = 90
AFK_MID_PENALTY = 120
AFK_EARLY_PENALTY = 160

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))

# -----------------------------
# AFK / Leaver detection
# -----------------------------

def is_afk_or_leaver(player, game_duration):
    time_played = player.get("timePlayed", game_duration)

    if player.get("leaverPenalty", False):
        return True, "penalty"
    if player.get("afk", False):
        return True, "afk_flag"
    if time_played < game_duration * 0.65:
        return True, "early"
    if time_played < game_duration * 0.85:
        return True, "mid"

    return False, None


# -----------------------------
# Low-damage grief detection
# -----------------------------

def compute_low_damage_grief(player, team, info):
    if player.get("teamPosition") == "UTILITY":
        return 0

    duration_minutes = info["gameDuration"] / 60

    if player.get("timePlayed", info["gameDuration"]) < info["gameDuration"] * 0.85:
        return 0

    hp_per_min = player.get("totalHeal", 0) / duration_minutes
    armor = player.get("armor", player.get("bonusArmor", 0))
    armor_per_min = armor / duration_minutes

    if hp_per_min >= TANK_HP_PER_MIN and armor_per_min >= TANK_ARMOR_PER_MIN:
        return 0

    minutes_played = max(1, player.get("timePlayed", info["gameDuration"]) / 60)
    player_dpmg = player["totalDamageDealtToChampions"] / minutes_played

    team_dpmg_avg = mean(
        p["totalDamageDealtToChampions"] / max(1, p.get("timePlayed", info["gameDuration"]) / 60)
        for p in team
        if p.get("timePlayed", info["gameDuration"]) >= info["gameDuration"] * 0.85
    )

    if team_dpmg_avg <= 0:
        return 0

    ratio = player_dpmg / team_dpmg_avg

    if ratio >= 0.8:
        return 0
    elif ratio >= 0.6:
        return 5
    elif ratio >= 0.4:
        return 15
    elif ratio >= 0.2:
        return 35
    else:
        return 70


# -----------------------------
# Vision grief (rank-normalized)
# -----------------------------

def compute_vision_grief(player, team, info, prpb, win, tier):
    minutes_played = max(1, player.get("timePlayed", info["gameDuration"]) / 60)

    player_vspm = player.get("visionScore", 0) / minutes_played
    team_vspm_avg = mean(
        p.get("visionScore", 0) / max(1, p.get("timePlayed", info["gameDuration"]) / 60)
        for p in team
    )

    tier = tier or "SILVER"

    if player.get("teamPosition") == "UTILITY":
        expected = SUPPORT_VISION_PER_MIN.get(tier, SUPPORT_VISION_PER_MIN["SILVER"])
        return VISION_SUPPORT_SELF_PENALTY if player_vspm < expected else 0

    if win:
        return 0

    activation = clamp(prpb / 25.0)  # soft PRPB cap
    if activation <= 0:
        return 0

    expected = VISION_PER_MIN.get(tier, VISION_PER_MIN["SILVER"])
    delta = (team_vspm_avg - expected) / expected

    if delta < -0.25:
        return min(
            VISION_GRIEF_WEIGHT,
            abs(delta) * VISION_GRIEF_WEIGHT * activation
        )

    return 0



# -----------------------------
# Team collapse (rank-normalized)
# -----------------------------

def compute_team_collapse(team, player, duration_minutes, tier):
    expected_dpm = TEAM_DPM.get(tier, TEAM_DPM["SILVER"])
    expected_deaths = expected_dpm * duration_minutes

    team_deaths = sum(p["deaths"] for p in team)
    player_deaths = player["deaths"]

    if team_deaths < expected_deaths * 1.3:
        return 0

    if player_deaths <= 2:
        return TEAM_COLLAPSE_WEIGHT

    if team_deaths >= player_deaths * TEAM_VS_PLAYER_DEATH_RATIO:
        return TEAM_COLLAPSE_WEIGHT

    return 0


def compute_hard_carry(player, team, win):
    if not win:
        return 0

    team_deaths = sum(p["deaths"] for p in team)
    team_avg_deaths = mean(p["deaths"] for p in team)

    if team_deaths < 24:
        return 0

    if player["deaths"] <= team_avg_deaths * 0.7:
        return HARD_CARRY_BONUS

    return 0


# -----------------------------
# Public entry point
# -----------------------------

def evaluate_grieftracker(matches, player_puuid, games=10):
    ranked_matches = [m for m in matches if m["info"].get("queueId") == RANKED_SOLO_QUEUE_ID]

    total = 0
    results = []

    for match in ranked_matches[:games]:
        g = evaluate_single_game(match, player_puuid)
        total += g["game_grief_points"]
        results.append(g)

    count = max(1, len(results))

    return {
        "grief_index": round(total / count, 1),
        "raw_grief_index": round(total, 1),
        "games_analyzed": count,
        "queue": "Ranked Solo/Duo",
        "games": results,
    }


# -----------------------------
# Core per-game logic
# -----------------------------

def evaluate_single_game(match, player_puuid):
    info = match["info"]
    duration_minutes = info["gameDuration"] / 60
    
    duration_factor = clamp(duration_minutes / EXPECTED_GAME_MINUTES, 0.75, 1.25)

    participants = info["participants"]
    player = next(p for p in participants if p["puuid"] == player_puuid)
    tier = (player.get("tier") or "SILVER").upper()

    team = [p for p in participants if p["teamId"] == player["teamId"]]
    teammates = [p for p in team if p["puuid"] != player_puuid]

    afk_penalty = 0
    afk_events = []

    for tm in teammates:
        afk, t = is_afk_or_leaver(tm, info["gameDuration"])
        if afk:
            afk_penalty += {
                "early": AFK_EARLY_PENALTY,
                "mid": AFK_MID_PENALTY,
            }.get(t, AFK_LATE_PENALTY)
            afk_events.append({"summonerName": tm.get("summonerName"), "type": t})

    low_damage_grief = compute_low_damage_grief(player, team, info)

    team_deaths = sum(p["deaths"] for p in team)
    team_dpm = team_deaths / duration_minutes
    team_avg_dpm = mean(p["deaths"] / duration_minutes for p in team)
    player_dpm = player["deaths"] / duration_minutes

    rank_expected = TEAM_DPM.get(tier, TEAM_DPM["SILVER"])
    blended_expected = (rank_expected + team_avg_dpm) / 2

    team_death_burden = max(0, team_dpm - blended_expected) * TDB_WEIGHT


    mult = OUTLIER_MULTIPLIER.get(tier, OUTLIER_MULTIPLIER["SILVER"])
    death_outliers = sum(
        max(0, (tm["deaths"] / duration_minutes - team_avg_dpm) * TDO_WEIGHT)
        for tm in teammates
        if (tm["deaths"] / duration_minutes) > team_avg_dpm * mult
    )

    prpb = max(0, (team_avg_dpm - player_dpm) * PRPB_WEIGHT)

    team_collapse = compute_team_collapse(team, player, duration_minutes, tier)

    clean_early_bonus = (
        PLAYER_CLEAN_EARLY_BONUS
        if player["deaths"] <= 2 and team_collapse
        else 0
    )

    player_ops = sum(
        player.get(k, 0)
        for k in (
            "dragonTakedowns",
            "baronTakedowns",
            "turretTakedowns",
            "inhibitorTakedowns",
            "riftHeraldTakedowns",
        )
    )
    team_ops_avg = mean(
        sum(p.get(k, 0) for k in (
            "dragonTakedowns",
            "baronTakedowns",
            "turretTakedowns",
            "inhibitorTakedowns",
            "riftHeraldTakedowns",
        ))
        for p in team
    )

    od = max(0, min((player_ops / team_ops_avg - 1) * OD_WEIGHT, 40)) if team_ops_avg else 0

    win = player["win"]
    amplifier = LOSS_AMPLIFIER if not win else WIN_AMPLIFIER

    vision_grief = compute_vision_grief(player, team, info, prpb, win, tier)

    positive_score = (
        team_death_burden
        + death_outliers
        + team_collapse
        + clean_early_bonus
        + prpb
        + od
        + afk_penalty
        + low_damage_grief
        + vision_grief
    ) * amplifier * duration_factor

    afk_cap = positive_score * 0.6
    afk_penalty = min(afk_penalty, afk_cap)


    boosted_penalty = (
        -((player_dpm - team_avg_dpm) * BOOSTED_WEIGHT)
        if win and player_dpm > team_avg_dpm and player_ops < team_ops_avg
        else 0
    )

    hard_carry_bonus = compute_hard_carry(player, team, win)

    game_grief_points = max(
        positive_score + boosted_penalty + hard_carry_bonus,
        MIN_GAME_SCORE,
    )

    return {
        "game_id": match["metadata"]["matchId"],
        "queue_id": info.get("queueId"),
        "win": win,
        "game_grief_points": round(game_grief_points, 2),
        "champion": player.get("championName"),
        "kills": player.get("kills"),
        "deaths": player.get("deaths"),
        "assists": player.get("assists"),
        "duration_min": round(duration_minutes, 1),
        "start_time_ms": info.get("gameStartTimestamp"),
        "team_dpm": round(team_dpm, 2),
        "team_avg_dpm": round(team_avg_dpm, 2),
        "player_dpm": round(player_dpm, 2),
        "player_ops": player_ops,
        "team_ops_avg": round(team_ops_avg, 2),
        "afk_penalty": afk_penalty,
        "afk_events": afk_events,
        "low_damage_grief": low_damage_grief,
        "vision_grief": round(vision_grief, 2),
        "components": {
            "team_death_burden": round(team_death_burden, 2),
            "death_outliers": round(death_outliers, 2),
            "player_relative_bonus": round(prpb, 2),
            "objective_disparity": round(od, 2),
            "low_damage_grief": low_damage_grief,
            "afk_penalty": afk_penalty,
            "vision_grief": round(vision_grief, 2),
            "boosted_penalty": round(boosted_penalty, 2),
            "team_collapse": team_collapse,
            "clean_early_bonus": clean_early_bonus,
            "hard_carry_bonus": hard_carry_bonus,
        },
    }
