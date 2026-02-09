"""
Rank-Normalization Baselines for Grief Tracker
Derived from public solo/duo ranked stats (vision, deaths, damage, objective rates).
Values are approximations intended for normalization, not exact API data.
"""

# Vision expectations: avg vision score per minute by rank
VISION_PER_MIN = {
    "IRON": 0.45,
    "BRONZE": 0.50,
    "SILVER": 0.55,
    "GOLD": 0.65,
    "PLATINUM": 0.75,
    "DIAMOND": 0.85,
    "MASTER": 0.90,
    "GRANDMASTER": 0.90,
    "CHALLENGER": 0.95,
}

# Team deaths per minute: general aggression / death rate by rank
TEAM_DPM = {
    "IRON": 1.20,
    "BRONZE": 1.15,
    "SILVER": 1.05,
    "GOLD": 0.95,
    "PLATINUM": 0.85,
    "DIAMOND": 0.75,
    "MASTER": 0.70,
    "GRANDMASTER": 0.70,
    "CHALLENGER": 0.68,
}

# Objective participation: fraction of objectives by rank (dragon, baron)
# Higher rank = more consistent team objective play
OBJ_PARTICIPATION = {
    "IRON": 0.25,
    "BRONZE": 0.30,
    "SILVER": 0.35,
    "GOLD": 0.40,
    "PLATINUM": 0.45,
    "DIAMOND": 0.50,
    "MASTER": 0.55,
    "GRANDMASTER": 0.60,
    "CHALLENGER": 0.60,
}

# Damage share (champion damage to champions / total team)
# Typical non-support ranges; supports normally do lower damage share
DAMAGE_SHARE = {
    "IRON": 0.18,
    "BRONZE": 0.20,
    "SILVER": 0.22,
    "GOLD": 0.24,
    "PLATINUM": 0.26,
    "DIAMOND": 0.28,
    "MASTER": 0.30,
    "GRANDMASTER": 0.32,
    "CHALLENGER": 0.34,
}

# Expected CS per minute by role and rank
CS_PER_MIN = {
    "IRON": 5.0,
    "BRONZE": 5.5,
    "SILVER": 6.0,
    "GOLD": 6.5,
    "PLATINUM": 7.0,
    "DIAMOND": 7.5,
    "MASTER": 8.0,
    "GRANDMASTER": 8.0,
    "CHALLENGER": 8.5,
}

# Support vision baseline: expected wards placed / minute
SUPPORT_VISION_PER_MIN = {
    "IRON": 0.8,
    "BRONZE": 0.9,
    "SILVER": 1.0,
    "GOLD": 1.2,
    "PLATINUM": 1.4,
    "DIAMOND": 1.6,
    "MASTER": 1.8,
    "GRANDMASTER": 1.8,
    "CHALLENGER": 2.0,
}

# Expected kill participation per rank (teammate kills you are involved in)
KILL_PARTICIPATION = {
    "IRON": 0.40,
    "BRONZE": 0.45,
    "SILVER": 0.50,
    "GOLD": 0.55,
    "PLATINUM": 0.60,
    "DIAMOND": 0.65,
    "MASTER": 0.65,
    "GRANDMASTER": 0.65,
    "CHALLENGER": 0.70,
}

# Generic baseline for deaths per minute per team
# Used for collapse thresholds, etc.
DEATH_RATE = {
    "IRON": 1.15,
    "BRONZE": 1.10,
    "SILVER": 1.00,
    "GOLD": 0.90,
    "PLATINUM": 0.80,
    "DIAMOND": 0.70,
    "MASTER": 0.65,
    "GRANDMASTER": 0.65,
    "CHALLENGER": 0.60,
}

# Minimum game duration expectations (seconds)
MIN_GAME_DURATION = 900  # 15 min
AVG_GAME_DURATION = {
    "IRON": 1500,      # 25 minutes
    "BRONZE": 1600,
    "SILVER": 1700,
    "GOLD": 1800,
    "PLATINUM": 1900,
    "DIAMOND": 2000,
    "MASTER": 2000,
    "GRANDMASTER": 2000,
    "CHALLENGER": 2000,
}

# Aggression baseline: kills per game per team
KILLS_PER_GAME = {
    "IRON": 25,
    "BRONZE": 24,
    "SILVER": 22,
    "GOLD": 20,
    "PLATINUM": 18,
    "DIAMOND": 16,
    "MASTER": 14,
    "GRANDMASTER": 14,
    "CHALLENGER": 12,
}

# Death outlier threshold multipliers
# How far above team average a teammate must be to count as an outlier
OUTLIER_MULTIPLIER = {
    "IRON": 1.5,
    "BRONZE": 1.6,
    "SILVER": 1.7,
    "GOLD": 1.8,
    "PLATINUM": 1.9,
    "DIAMOND": 2.0,
    "MASTER": 2.0,
    "GRANDMASTER": 2.1,
    "CHALLENGER": 2.2,
}
