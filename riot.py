import time
import requests
import urllib.parse
import asyncio
update_lock = asyncio.Lock()
from config import RIOT_API_KEY, REGION, PLATFORM

import time

DEFAULT_TIMEOUT = 10


# --------------------
# Configuration
# --------------------
BASE_HEADERS = {"X-Riot-Token": RIOT_API_KEY}
DEFAULT_TIMEOUT = 10

# Keep under 20 req/sec (dev limit). 0.06s ~= 16.6 req/sec
MATCH_DETAIL_SLEEP_SEC = 0.06

# Simple in-memory caches (reset when bot restarts)
_MATCH_CACHE = {}          # match_id -> match json
_DDRAGON_ID_TO_NAME = None # champId(int) -> champName(str)

# --------------------
# Core HTTP helpers
# --------------------
def _get(url: str, headers: dict | None = None):
    h = headers if headers is not None else BASE_HEADERS
    r = requests.get(url, headers=h, timeout=DEFAULT_TIMEOUT)
    return _handle_response(r)

def _handle_response(r):
    # Make errors actionable (show URL + body)
    if r.status_code in (400, 401, 403, 404, 429):
        raise RuntimeError(
            f"HTTP {r.status_code} {r.request.method} {r.url} -> {r.text} "
            f"(Retry-After={r.headers.get('Retry-After')})"
        )
    r.raise_for_status()
    return r.json()

def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")

# --------------------
# Riot Account (Riot ID -> PUUID) [REGION routing: americas]
# --------------------
def get_account_by_riot_id(game_name: str, tag_line: str):
    url = (
        f"https://{REGION}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{_quote(game_name)}/{_quote(tag_line)}"
    )
    return _get(url)

# --------------------
# Summoner (PUUID -> level) [PLATFORM routing: na1]
# NOTE: some environments omit encryptedSummonerId ("id") from by-puuid.
# --------------------
def get_summoner_by_puuid(puuid: str):
    url = f"https://{PLATFORM}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
    return _get(url)

def get_summoner_by_name(game_name: str):
    # May 403 depending on key/environment. Used only for mastery fallback.
    url = f"https://{PLATFORM}.api.riotgames.com/lol/summoner/v4/summoners/by-name/{_quote(game_name)}"
    return _get(url)

# --------------------
# Ranked entries (by PUUID) [PLATFORM routing: na1]
# --------------------
def get_league_entries_by_puuid(puuid: str):
    url = f"https://{PLATFORM}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
    return _get(url)

# --------------------
# High-level profile used by bot
# --------------------
def get_player_profile(game_name: str, tag_line: str):
    """
    Returns:
      game_name, tag_line, puuid, summoner_level, ranked_entries
    """
    account = get_account_by_riot_id(game_name, tag_line)
    puuid = account["puuid"]

    summoner = get_summoner_by_puuid(puuid)
    encrypted_id = summoner["id"]

    ranked_entries = get_league_entries_by_puuid(puuid)

    return {
        "game_name": account.get("gameName", game_name),
        "tag_line": account.get("tagLine", tag_line),
        "puuid": puuid,
        "summoner_level": summoner.get("summonerLevel", 0),
        "ranked_entries": ranked_entries or [],
    }

# --------------------
# Match-V5 [REGION routing: americas]
# --------------------
def get_match_ids_by_puuid(puuid: str, count: int = 20, queue: int | None = None, start: int = 0):
    q = f"&queue={queue}" if queue is not None else ""
    url = (
        f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
        f"?start={start}&count={count}{q}"
    )
    return _get(url)

def get_match(match_id: str, max_retries: int = 6):
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}"

    for attempt in range(max_retries):
        r = requests.get(url, headers=BASE_HEADERS, timeout=DEFAULT_TIMEOUT)

        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            wait = int(ra) if (ra and ra.isdigit()) else 2
            time.sleep(wait)
            continue

        return _handle_response(r)

    raise RuntimeError(f"HTTP 429 too many retries for match {match_id}")

def compute_recent_kda(puuid: str, count: int = 20):
    match_ids = get_match_ids_by_puuid(puuid, count=count)
    kills = deaths = assists = 0

    for mid in match_ids:
        m = get_match(mid)
        parts = m.get("info", {}).get("participants", [])
        me = next((p for p in parts if p.get("puuid") == puuid), None)
        if not me:
            continue

        kills += int(me.get("kills", 0))
        deaths += int(me.get("deaths", 0))
        assists += int(me.get("assists", 0))

        # throttle detail loop to reduce 20/sec bursts
        time.sleep(MATCH_DETAIL_SLEEP_SEC)

    kda = (kills + assists) / max(1, deaths)
    return {"kills": kills, "deaths": deaths, "assists": assists, "kda": kda, "games": len(match_ids)}

def solo_top_champs_wl(puuid: str, match_count: int = 30, top: int = 5, solo_queue: int = 420):
    match_ids = get_match_ids_by_puuid(puuid, count=match_count, queue=solo_queue)

    stats = {}  # champName -> {games, wins, losses}
    for mid in match_ids:
        m = get_match(mid)
        parts = m.get("info", {}).get("participants", [])
        me = next((p for p in parts if p.get("puuid") == puuid), None)
        if not me:
            continue

        champ = me.get("championName", "Unknown")
        win = bool(me.get("win", False))

        if champ not in stats:
            stats[champ] = {"games": 0, "wins": 0, "losses": 0}

        stats[champ]["games"] += 1
        stats[champ]["wins"] += 1 if win else 0
        stats[champ]["losses"] += 0 if win else 1

        time.sleep(MATCH_DETAIL_SLEEP_SEC)

    top_champs = sorted(stats.items(), key=lambda kv: kv[1]["games"], reverse=True)[:top]
    out = []
    for champ, s in top_champs:
        games = s["games"]
        wins = s["wins"]
        losses = s["losses"]
        wr = (wins / games * 100.0) if games else 0.0
        out.append({"champion": champ, "games": games, "wins": wins, "losses": losses, "wr": wr})
    return out

# --------------------
# Data Dragon mapping (championId -> champion name)
# --------------------
def _load_ddragon_champion_id_map():
    global _DDRAGON_ID_TO_NAME
    if _DDRAGON_ID_TO_NAME is not None:
        return _DDRAGON_ID_TO_NAME

    versions = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=DEFAULT_TIMEOUT).json()
    v = versions[0]
    champ_json = requests.get(
        f"https://ddragon.leagueoflegends.com/cdn/{v}/data/en_US/champion.json",
        timeout=DEFAULT_TIMEOUT
    ).json()

    mapping = {}
    for champ_name, champ_info in champ_json["data"].items():
        mapping[int(champ_info["key"])] = champ_name

    _DDRAGON_ID_TO_NAME = mapping
    return mapping

# --------------------
# Mastery (Top N)
# IMPORTANT: Mastery endpoint requires encryptedSummonerId ("id").
# In your environment, summoner-by-puuid often omits "id" and summoner-by-name may 403.
# We implement: try by-name, otherwise raise a clear error.
# --------------------
def get_top_mastery_by_riot_id(game_name: str, tag_line: str, top: int = 5):
    """
    Returns list of {champion, level, points}
    Uses Riot ID to resolve canonical gameName, then summoner-by-name to obtain encryptedSummonerId.
    If summoner-by-name is forbidden (403), mastery cannot be fetched with current access.
    """
    account = get_account_by_riot_id(game_name, tag_line)
    riot_game_name = account.get("gameName", game_name)

    # Get encryptedSummonerId ("id") for mastery
    summoner = get_summoner_by_name(riot_game_name)  # may 403 in your environment
    summoner_id = summoner.get("id")
    if not summoner_id:
        raise RuntimeError(f"Summoner 'id' missing from by-name response: {summoner}")

    url = (
        f"https://{PLATFORM}.api.riotgames.com/lol/champion-mastery/v4/"
        f"champion-masteries/by-summoner/{summoner_id}"
    )
    mastery_list = _get(url)

    id_to_name = _load_ddragon_champion_id_map()
    out = []
    for m in mastery_list[:top]:
        champ_id = int(m.get("championId", 0))
        out.append({
            "champion": id_to_name.get(champ_id, f"ChampionId {champ_id}"),
            "level": int(m.get("championLevel", 0)),
            "points": int(m.get("championPoints", 0)),
        })
    return out
    
def get_active_game(encrypted_summoner_id: str):
    url = f"https://{PLATFORM}.api.riotgames.com/lol/spectator/v4/active-games/by-summoner/{encrypted_summoner_id}"
    return _get(url)
