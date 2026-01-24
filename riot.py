# riot.py
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests
from config import RIOT_API_KEY, PLATFORM as DEFAULT_PLATFORM

DEFAULT_TIMEOUT = 10

# --------------------
# Configuration
# --------------------
BASE_HEADERS = {"X-Riot-Token": RIOT_API_KEY}

# Keep under burst limits when looping match details
MATCH_DETAIL_SLEEP_SEC = 0.06

# Simple in-memory caches (reset when bot restarts)
_MATCH_CACHE: Dict[str, Dict[str, Any]] = {}         # match_id -> match json
_DDRAGON_ID_TO_NAME: Optional[Dict[int, str]] = None # champId -> champName
_PUUID_TO_PLATFORM: Dict[str, str] = {}              # puuid -> "na1"/"euw1"/...
_PUUID_TO_ROUTING: Dict[str, str] = {}               # puuid -> "americas"/"europe"/"asia"


# --------------------
# Core HTTP helpers
# --------------------
def _handle_response(r: requests.Response) -> Any:
    # Make errors actionable (show URL + body)
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
    GET with basic 429 retry. Raises on other non-2xx statuses.
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
    # Compatibility wrapper (older code expects _get)
    return _request_with_retry(url)


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _routing_regions_to_try() -> List[str]:
    # Riot routing hosts for account + match endpoints
    return ["americas", "europe", "asia"]


# --------------------
# Riot Account (Riot ID <-> PUUID) [ROUTING: americas/europe/asia]
# --------------------
def get_account_by_riot_id(game_name: str, tag_line: str) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    for routing in _routing_regions_to_try():
        url = (
            f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
            f"{_quote(game_name)}/{_quote(tag_line)}"
        )
        try:
            data = _request_with_retry(url)
            if isinstance(data, dict) and "puuid" in data:
                _PUUID_TO_ROUTING[data["puuid"]] = routing
                return data
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Failed to resolve Riot ID {game_name}#{tag_line}. Last error: {last_err}")


def get_account_by_puuid(puuid: str) -> Dict[str, Any]:
    routing = _PUUID_TO_ROUTING.get(puuid)
    regions = [routing] if routing else []
    regions += [r for r in _routing_regions_to_try() if r != routing]

    last_err: Optional[Exception] = None
    for routing in regions:
        url = f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}"
        try:
            data = _request_with_retry(url)
            if isinstance(data, dict) and data.get("puuid") == puuid:
                _PUUID_TO_ROUTING[puuid] = routing
                return data
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Failed to resolve account by PUUID. Last error: {last_err}")


# --------------------
# Platform lookup (PUUID -> platform shard like na1/br1/euw1)
# Uses: /riot/account/v1/region/by-game/lol/by-puuid/{puuid}
# --------------------
def get_platform_by_puuid_lol(puuid: str) -> str:
    if puuid in _PUUID_TO_PLATFORM:
        return _PUUID_TO_PLATFORM[puuid]

    routing = _PUUID_TO_ROUTING.get(puuid)
    regions = [routing] if routing else []
    regions += [r for r in _routing_regions_to_try() if r != routing]

    last_err: Optional[Exception] = None
    for routing in regions:
        url = f"https://{routing}.api.riotgames.com/riot/account/v1/region/by-game/lol/by-puuid/{puuid}"
        try:
            resp = _request_with_retry(url)

            # NEW: endpoint may return dict OR string
            if isinstance(resp, dict):
                platform = str(resp.get("region", "")).strip().lower()
            else:
                platform = str(resp).strip().strip('"').lower()

            if platform:
                _PUUID_TO_PLATFORM[puuid] = platform
                _PUUID_TO_ROUTING[puuid] = routing
                return platform

        except Exception as e:
            last_err = e

    raise RuntimeError(f"Failed to determine platform for PUUID. Last error: {last_err}")


# --------------------
# Summoner + League [PLATFORM: na1/euw1/...]
# --------------------
def get_summoner_by_puuid(puuid: str, platform: Optional[str] = None) -> Dict[str, Any]:
    plat = platform

    # If caller accidentally passes the dict from region-by-puuid, extract region
    if isinstance(plat, dict):
        plat = plat.get("region") or plat.get("platform")

    if not plat:
        try:
            plat = get_platform_by_puuid_lol(puuid)
        except Exception:
            plat = DEFAULT_PLATFORM

    if not isinstance(plat, str) or not plat:
        raise RuntimeError(f"Invalid platform value for summoner lookup: {plat!r}")

    url = f"https://{plat}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
    data = _request_with_retry(url)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response for summoner-by-puuid: {data!r}")
    return data


def get_league_entries_by_puuid(puuid: str) -> List[Dict[str, Any]]:
    platform = get_platform_by_puuid_lol(puuid)
    url = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
    data = _request_with_retry(url)
    return data if isinstance(data, list) else []


# --------------------
# High-level profile used by bot
# --------------------
def get_player_profile(game_name: str, tag_line: str) -> Dict[str, Any]:
    """
    Returns:
      game_name, tag_line, puuid, summoner_level, ranked_entries
    """
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
# Match-V5 [ROUTING: americas/europe/asia]
# --------------------
def _match_routing_for_puuid(puuid: str) -> List[str]:
    routing = _PUUID_TO_ROUTING.get(puuid)
    if routing:
        return [routing] + [r for r in _routing_regions_to_try() if r != routing]
    return _routing_regions_to_try()


def get_match_ids_by_puuid(
    puuid: str,
    count: int = 20,
    queue: Optional[int] = None,
    start: int = 0,
) -> List[str]:
    q = f"&queue={queue}" if queue is not None else ""
    last_err: Optional[Exception] = None

    for routing in _match_routing_for_puuid(puuid):
        url = (
            f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
            f"?start={start}&count={count}{q}"
        )
        try:
            data = _request_with_retry(url)
            if isinstance(data, list):
                _PUUID_TO_ROUTING[puuid] = routing
                return data
        except Exception as e:
            last_err = e

    raise RuntimeError(f"Failed to fetch match IDs for PUUID. Last error: {last_err}")


def get_match(match_id: str, max_retries: int = 6) -> Dict[str, Any]:
    if match_id in _MATCH_CACHE:
        return _MATCH_CACHE[match_id]

    last_err: Optional[Exception] = None
    for routing in _routing_regions_to_try():
        url = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        try:
            data = _request_with_retry(url, max_retries=max_retries)
            if isinstance(data, dict):
                _MATCH_CACHE[match_id] = data
                return data
        except Exception as e:
            last_err = e

    raise RuntimeError(f"Failed to fetch match {match_id}. Last error: {last_err}")


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


def solo_top_champs_wl(
    puuid: str,
    match_count: int = 30,
    top: int = 5,
    solo_queue: int = 420,
) -> List[Dict[str, Any]]:
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

    versions = requests.get(
        "https://ddragon.leagueoflegends.com/api/versions.json",
        timeout=DEFAULT_TIMEOUT,
    ).json()
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
# Mastery (Top N) - by-puuid endpoint
# --------------------
def get_top_mastery_by_puuid(puuid: str, top: int = 5) -> List[Dict[str, Any]]:
    platform = get_platform_by_puuid_lol(puuid)
    url = (
        f"https://{platform}.api.riotgames.com/lol/champion-mastery/v4/"
        f"champion-masteries/by-puuid/{puuid}"
    )
    mastery_list = _request_with_retry(url)
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
# Spectator (LIVE GAMES) - Spectator v5 (PUUID)
# --------------------
def get_active_game(puuid: str) -> Optional[Dict[str, Any]]:
    """
    GET /lol/spectator/v5/active-games/by-summoner/{encryptedPUUID}
    Riot labels it encryptedPUUID; in practice you pass the player's PUUID.
    Returns None if not in game (404).
    """
    platform = get_platform_by_puuid_lol(puuid)
    url = f"https://{platform}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"

    r = requests.get(url, headers=BASE_HEADERS, timeout=DEFAULT_TIMEOUT)
    if r.status_code == 404:
        return None
    return _handle_response(r)
