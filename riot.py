# riot.py
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests
from config import RIOT_API_KEY, REGION, PLATFORM

DEFAULT_TIMEOUT = 10

BASE_HEADERS = {"X-Riot-Token": RIOT_API_KEY}

# Keep under burst limits when looping match details
MATCH_DETAIL_SLEEP_SEC = 0.06

# Simple in-memory caches (reset when bot restarts)
_MATCH_CACHE: Dict[str, Dict[str, Any]] = {}          # match_id -> match json
_DDRAGON_ID_TO_NAME: Optional[Dict[int, str]] = None  # champId -> champName


# --------------------
# Core HTTP helpers
# --------------------
def _handle_response(r: requests.Response) -> Any:
    if r.status_code in (400, 401, 403, 404, 429):
        raise RuntimeError(
            f"HTTP {r.status_code} {r.request.method} {r.url} -> {r.text} "
            f"(Retry-After={r.headers.get('Retry-After')})"
        )
    r.raise_for_status()

    ct = (r.headers.get("Content-Type") or "").lower()
    if "application/json" in ct:
        return r.json()
    return r.text


def _request_with_retry(url: str, max_retries: int = 6) -> Any:
    """
    GET with basic 429 retry. Raises on other non-2xx statuses (via _handle_response).
    """
    for _ in range(max_retries):
        r = requests.get(url, headers=BASE_HEADERS, timeout=DEFAULT_TIMEOUT)
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            wait = int(ra) if (ra and ra.isdigit()) else 2
            time.sleep(wait)
            continue
        return _handle_response(r)
    raise RuntimeError(f"HTTP 429 too many retries for {url}")


def _get(url: str) -> Any:
    return _request_with_retry(url)


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


# --------------------
# Routing/platform helpers
# --------------------
def _routing_host() -> str:
    # Your config REGION is the correct routing host for NA accounts/match: "americas"
    return REGION


def _platform_host() -> str:
    # Your config PLATFORM is the correct platform host for NA LoL: "na1"
    return PLATFORM.lower()


# --------------------
# Account-V1 (Riot ID -> PUUID) [ROUTING host]
# --------------------
def get_account_by_riot_id(game_name: str, tag_line: str) -> Dict[str, Any]:
    url = (
        f"https://{_routing_host()}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{_quote(game_name)}/{_quote(tag_line)}"
    )
    data = _get(url)
    if not isinstance(data, dict) or "puuid" not in data:
        raise RuntimeError(f"Unexpected account response: {data!r}")
    return data


def get_account_by_puuid(puuid: str) -> Dict[str, Any]:
    url = f"https://{_routing_host()}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}"
    data = _get(url)
    if not isinstance(data, dict) or data.get("puuid") != puuid:
        raise RuntimeError(f"Unexpected account-by-puuid response: {data!r}")
    return data


# --------------------
# Summoner-V4 [PLATFORM host]
# --------------------
def get_summoner_by_puuid(puuid: str) -> Dict[str, Any]:
    url = f"https://{_platform_host()}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
    data = _get(url)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected summoner response: {data!r}")
    return data


# --------------------
# League-V4 [PLATFORM host]
# --------------------
def get_league_entries_by_puuid(puuid: str) -> List[Dict[str, Any]]:
    url = f"https://{_platform_host()}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
    data = _get(url)
    return data if isinstance(data, list) else []


# --------------------
# High-level profile used by bot
# --------------------
def get_player_profile(game_name: str, tag_line: str) -> Dict[str, Any]:
    account = get_account_by_riot_id(game_name, tag_line)
    puuid = account["puuid"]

    summoner = get_summoner_by_puuid(puuid)
    ranked_entries = get_league_entries_by_puuid(puuid)

    return {
        "game_name": account.get("gameName", game_name),
        "tag_line": account.get("tagLine", tag_line),
        "puuid": puuid,
        "summoner_level": int(summoner.get("summonerLevel", 0) or 0),
        "ranked_entries": ranked_entries or [],
    }


# --------------------
# Match-V5 [ROUTING host]
# --------------------
def get_match_ids_by_puuid(
    puuid: str,
    count: int = 20,
    queue: Optional[int] = None,
    start: int = 0,
) -> List[str]:
    q = f"&queue={queue}" if queue is not None else ""
    url = (
        f"https://{_routing_host()}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
        f"?start={start}&count={count}{q}"
    )
    data = _get(url)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected match-id list response: {data!r}")
    return data


def get_match(match_id: str, max_retries: int = 6) -> Dict[str, Any]:
    if match_id in _MATCH_CACHE:
        return _MATCH_CACHE[match_id]

    url = f"https://{_routing_host()}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    data = _request_with_retry(url, max_retries=max_retries)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected match response: {data!r}")

    _MATCH_CACHE[match_id] = data
    return data


def compute_recent_kda(puuid: str, count: int = 20) -> Dict[str, Any]:
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

        time.sleep(MATCH_DETAIL_SLEEP_SEC)

    kda = (kills + assists) / max(1, deaths)
    return {"kills": kills, "deaths": deaths, "assists": assists, "kda": kda, "games": len(match_ids)}


def solo_top_champs_wl(puuid: str, match_count: int = 30, top: int = 5, solo_queue: int = 420) -> List[Dict[str, Any]]:
    match_ids = get_match_ids_by_puuid(puuid, count=match_count, queue=solo_queue)

    stats: Dict[str, Dict[str, int]] = {}
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
    out: List[Dict[str, Any]] = []
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
def _load_ddragon_champion_id_map() -> Dict[int, str]:
    global _DDRAGON_ID_TO_NAME
    if _DDRAGON_ID_TO_NAME is not None:
        return _DDRAGON_ID_TO_NAME

    versions = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=DEFAULT_TIMEOUT).json()
    v = versions[0]
    champ_json = requests.get(
        f"https://ddragon.leagueoflegends.com/cdn/{v}/data/en_US/champion.json",
        timeout=DEFAULT_TIMEOUT,
    ).json()

    mapping: Dict[int, str] = {}
    for champ_name, champ_info in champ_json["data"].items():
        mapping[int(champ_info["key"])] = champ_name

    _DDRAGON_ID_TO_NAME = mapping
    return mapping


# --------------------
# Mastery-V4 by-puuid (no summonerId needed)
# --------------------
def get_top_mastery_by_puuid(puuid: str, top: int = 5) -> List[Dict[str, Any]]:
    url = (
        f"https://{_platform_host()}.api.riotgames.com/lol/champion-mastery/v4/"
        f"champion-masteries/by-puuid/{puuid}"
    )
    mastery_list = _get(url)
    if not isinstance(mastery_list, list):
        return []

    id_to_name = _load_ddragon_champion_id_map()
    out: List[Dict[str, Any]] = []
    for m in mastery_list[:top]:
        champ_id = int(m.get("championId", 0) or 0)
        out.append(
            {
                "champion": id_to_name.get(champ_id, f"ChampionId {champ_id}"),
                "level": int(m.get("championLevel", 0) or 0),
                "points": int(m.get("championPoints", 0) or 0),
            }
        )
    return out


def get_top_mastery_by_riot_id(game_name: str, tag_line: str, top: int = 5) -> List[Dict[str, Any]]:
    account = get_account_by_riot_id(game_name, tag_line)
    return get_top_mastery_by_puuid(account["puuid"], top=top)


# --------------------
# Spectator-V5 (LIVE): by-summoner/{encryptedPUUID} => pass PUUID
# --------------------
def get_active_game(puuid: str) -> Optional[Dict[str, Any]]:
    """
    Returns:
      - dict if in game
      - None if not in game (404)
    """
    url = f"https://{_platform_host()}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
    r = requests.get(url, headers=BASE_HEADERS, timeout=DEFAULT_TIMEOUT)
    if r.status_code == 404:
        return None
    return _handle_response(r)
