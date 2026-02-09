# grieftracker.py
# Computes a cumulative Grief Index over recent ranked solo/duo matches

from statistics import mean

# -----------------------------
# Tunable constants
# -----------------------------

BASELINE_TEAM_DPM = 0.82   # can later be computed dynamically
LOSS_AMPLIFIER = 1.25
WIN_AMPLIFIER = 0.75

MIN_GAME_SCORE = -50

# Weights
TDB_WEIGHT = 40
TDO_WEIGHT = 25
PRPB_WEIGHT = 30
OD_WEIGHT = 35
BOOSTED_WEIGHT = 45

# Tank detection thresholds (stat-based)
TANK_HP_PER_MIN = 95
TANK_ARMOR_PER_MIN = 3.0

# -----------------------------
# Vision / Warding settings
# -----------------------------

VISION_EXPECTED_PER_MIN = 0.9
VISION_GRIEF_WEIGHT = 8
VISION_SUPPORT_SELF_PENALTY = -10

# -----------------------------
# Early collapse / team impact
# -----------------------------

# -----------------------------
# Team collapse / agency loss
# -----------------------------

TEAM_COLLAPSE_WEIGHT = 70
PLAYER_CLEAN_EARLY_BONUS = 35

TEAM_DEATH_COLLAPSE_MIN = 12
TEAM_VS_PLAYER_DEATH_RATIO = 2.5


HARD_CARRY_BONUS = -30


# -----------------------------
# Ranked-only settings
# -----------------------------

RANKED_SOLO_QUEUE_ID = 420

# -----------------------------
# AFK / Leaver penalties (Tier 0)
# -----------------------------

AFK_LATE_PENALTY = 90
AFK_MID_PENALTY = 120
AFK_EARLY_PENALTY = 160


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
    # Support is always exempt
    if player.get("teamPosition") == "UTILITY":
        return 0

    duration_minutes = info["gameDuration"] / 60

    # If player AFKed, damage is irrelevant (handled elsewhere)
    if player.get("timePlayed", info["gameDuration"]) < info["gameDuration"] * 0.85:
        return 0

    # Durability-based tank detection
    hp_per_min = player.get("totalHeal", 0) / duration_minutes
    armor = player.get("armor", player.get("bonusArmor", 0))
    armor_per_min = armor / duration_minutes

    if hp_per_min >= TANK_HP_PER_MIN and armor_per_min >= TANK_ARMOR_PER_MIN:
        return 0

    # Damage per minute (based on time played)
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

    # Nonlinear punishment
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


def compute_vision_grief(player, team, info, prpb, win):
    duration_minutes = info["gameDuration"] / 60
    minutes_played = max(1, player.get("timePlayed", info["gameDuration"]) / 60)

    # Vision per minute
    player_vspm = player.get("visionScore", 0) / minutes_played
    team_vspm_avg = mean(
        p.get("visionScore", 0) / max(1, p.get("timePlayed", info["gameDuration"]) / 60)
        for p in team
    )

    # Case A: You are support → low vision is your responsibility
    if player.get("teamPosition") == "UTILITY":
        if player_vspm < VISION_EXPECTED_PER_MIN:
            return VISION_SUPPORT_SELF_PENALTY
        return 0

    # Case B: Non-support → only matters if YOU played well
    if prpb <= 0:
        return 0

    # Team vision below expectation
    if team_vspm_avg < VISION_EXPECTED_PER_MIN and not win:
        deficit = VISION_EXPECTED_PER_MIN - team_vspm_avg
        return min(VISION_GRIEF_WEIGHT, deficit * VISION_GRIEF_WEIGHT)

    return 0

def compute_team_collapse(team, player):
    """
    Detects games where the team lost the game before the player had agency.
    """

    team_deaths = sum(p["deaths"] for p in team)
    player_deaths = player["deaths"]

    # Not enough deaths to matter
    if team_deaths < TEAM_DEATH_COLLAPSE_MIN:
        return 0

    # Player is not the problem
    if player_deaths <= 2:
        return TEAM_COLLAPSE_WEIGHT

    # Team massively out-died player
    if team_deaths >= player_deaths * TEAM_VS_PLAYER_DEATH_RATIO:
        return TEAM_COLLAPSE_WEIGHT

    return 0



def compute_hard_carry(player, team, win):
    """
    Detects wins where the player carried through a griefed team.
    """

    if not win:
        return 0

    team_deaths = sum(p["deaths"] for p in team)
    team_avg_deaths = mean(p["deaths"] for p in team)

    # Messy game required
    if team_deaths < 24:
        return 0

    # Player survived meaningfully better than team
    if player["deaths"] <= team_avg_deaths * 0.7:
        return HARD_CARRY_BONUS

    return 0

# -----------------------------
# Public entry point
# -----------------------------

def evaluate_grieftracker(matches, player_puuid, games=10):
    ranked_matches = [
        m for m in matches
        if m["info"].get("queueId") == RANKED_SOLO_QUEUE_ID
    ]

    total_grief_index = 0
    per_game_breakdown = []

    for match in ranked_matches[:games]:
        game_result = evaluate_single_game(match, player_puuid)
        total_grief_index += game_result["game_grief_points"]
        per_game_breakdown.append(game_result)

    games_count = max(1, len(per_game_breakdown))

    return {
        "grief_index": round(total_grief_index / games_count, 1),
        "raw_grief_index": round(total_grief_index, 1),
        "games_analyzed": games_count,
        "queue": "Ranked Solo/Duo",
        "games": per_game_breakdown
    }





# -----------------------------
# Core per-game logic
# -----------------------------

def evaluate_single_game(match, player_puuid):
    info = match["info"]
    duration_minutes = info["gameDuration"] / 60

    participants = info["participants"]
    player = next(p for p in participants if p["puuid"] == player_puuid)
    team_id = player["teamId"]

    team = [p for p in participants if p["teamId"] == team_id]
    teammates = [p for p in team if p["puuid"] != player_puuid]

    # -----------------------------
    # AFK / Leaver Detection
    # -----------------------------

    afk_penalty = 0
    afk_events = []

    for tm in teammates:
        afk, afk_type = is_afk_or_leaver(tm, info["gameDuration"])
        if afk:
            if afk_type == "early":
                afk_penalty += AFK_EARLY_PENALTY
            elif afk_type == "mid":
                afk_penalty += AFK_MID_PENALTY
            else:
                afk_penalty += AFK_LATE_PENALTY

            afk_events.append({
                "summonerName": tm.get("summonerName"),
                "type": afk_type
            })

    team_collapse = compute_team_collapse(team, player)

    # -----------------------------
    # Low Damage Grief
    # -----------------------------

    low_damage_grief = compute_low_damage_grief(player, team, info)

    # -----------------------------
    # Deaths Per Minute
    # -----------------------------

    team_deaths = sum(p["deaths"] for p in team)
    team_dpm = team_deaths / duration_minutes
    team_avg_dpm = mean(p["deaths"] / duration_minutes for p in team)
    player_dpm = player["deaths"] / duration_minutes

    # -----------------------------
    # Objective Participation Score
    # -----------------------------

    def ops(p):
        return (
            p.get("dragonTakedowns", 0)
            + p.get("baronTakedowns", 0)
            + p.get("turretTakedowns", 0)
            + p.get("inhibitorTakedowns", 0)
            + p.get("riftHeraldTakedowns", 0)
        )

    player_ops = ops(player)
    team_ops_avg = mean(ops(p) for p in team)

    # -----------------------------
    # Team Death Burden
    # -----------------------------

    team_death_burden = max(0, team_dpm - BASELINE_TEAM_DPM) * TDB_WEIGHT

    # -----------------------------
    # Teammate Death Outliers
    # -----------------------------

    death_outliers = 0
    for tm in teammates:
        tm_dpm = tm["deaths"] / duration_minutes
        if tm_dpm > team_avg_dpm * 1.6:
            death_outliers += (tm_dpm - team_avg_dpm) * TDO_WEIGHT

    # -----------------------------
    # Player Relative Survival Bonus
    # -----------------------------

    prpb = 0
    dpm_delta = team_avg_dpm - player_dpm
    if dpm_delta > 0:
        prpb = dpm_delta * PRPB_WEIGHT

    # -----------------------------
    # Player Clean Early Bonus
    # -----------------------------

    clean_early_bonus = 0
    team_deaths = sum(p["deaths"] for p in team)

    if player["deaths"] <= 2 and team_deaths >= TEAM_DEATH_COLLAPSE_MIN:
        clean_early_bonus = PLAYER_CLEAN_EARLY_BONUS

    # -----------------------------
    # Objective Disparity
    # -----------------------------

    od = 0
    if team_ops_avg > 0:
        objective_ratio = player_ops / team_ops_avg
        od = max(0, min((objective_ratio - 1.0) * OD_WEIGHT, 40))

    # -----------------------------
    # Win / Loss Amplifier
    # -----------------------------

    win = player["win"]
    amplifier = LOSS_AMPLIFIER if not win else WIN_AMPLIFIER

    # -----------------------------
    # Vision / Warding Grief
    # -----------------------------

    vision_grief = compute_vision_grief(
        player, team, info, prpb, win
    )
    
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
    ) * amplifier




    # -----------------------------
    # Boosted Penalty
    # -----------------------------

    boosted_penalty = 0
    if (
        win
        and player_dpm > team_avg_dpm
        and player_ops < team_ops_avg
    ):
        boosted_penalty = -((player_dpm - team_avg_dpm) * BOOSTED_WEIGHT)

    # -----------------------------
    # Hard Carry Bonus
    # -----------------------------

    hard_carry_bonus = compute_hard_carry(player, team, win)

        # -----------------------------
    # Final Grief Points
    # -----------------------------

    game_grief_points = (
    positive_score
    + boosted_penalty
    + hard_carry_bonus
    )

    game_grief_points = max(game_grief_points, MIN_GAME_SCORE)

    return {
        "game_id": match["metadata"]["matchId"],
        "queue_id": info.get("queueId"),
        "win": win,
        "game_grief_points": round(game_grief_points, 2),

        # ---- PLAYER CONTEXT (FIXES ?/?/?) ----
        "champion": player.get("championName"),
        "kills": player.get("kills"),
        "deaths": player.get("deaths"),
        "assists": player.get("assists"),
        "duration_min": round(duration_minutes, 1),
        "start_time_ms": info.get("gameStartTimestamp"),

        # ---- RATE STATS ----
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
        }
    }

