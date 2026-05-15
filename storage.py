import json
import os
from datetime import datetime, timezone
from typing import Any

# -------------------------------------------------
# Storage configuration
# -------------------------------------------------
# Always store league.json next to this file.
# This prevents:
# - systemd vs shell CWD mismatches
# - multiple JSON files being silently created
# - "data disappearing" after restarts
# -------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "league.json")

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv(os.path.join(BASE_DIR, ".env"))

PRETTY_JSON = os.getenv("STORAGE_PRETTY_JSON", "false").lower() in {"1", "true", "yes", "on"}

# -------------------------------------------------
# Helpers
# -------------------------------------------------

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_riot_id(riot_id: str) -> tuple[str, str]:
    riot_id = (riot_id or "").strip()
    if "#" not in riot_id:
        raise ValueError("Riot ID must use Name#TAG format")
    game_name, tag_line = riot_id.split("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    if not game_name or not tag_line:
        raise ValueError("Riot ID must include both game name and tag line")
    return game_name, tag_line


def canonical_riot_id(game_name: str, tag_line: str) -> str:
    return f"{game_name.strip()}#{tag_line.strip()}"


def normalize_riot_id(riot_id: str) -> str:
    game_name, tag_line = parse_riot_id(riot_id)
    return canonical_riot_id(game_name, tag_line).casefold()

# -------------------------------------------------
# Load + normalize persistent data
# -------------------------------------------------

def load_data() -> dict:
    """
    Loads league.json and enforces a complete schema.
    Never deletes existing data.
    Never overwrites populated fields.
    Safe to call on every command.
    """
    if not os.path.exists(DATA_FILE):
        data = {}
    else:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            # Hard failure here is intentional: corrupted JSON must be fixed manually
            raise RuntimeError(f"Failed to load {DATA_FILE}: {e}")

    # ---- schema normalization (NON-DESTRUCTIVE) ----
    data.setdefault("season", 1)
    data.setdefault("schema_version", 2)
    data.setdefault("players", {})
    data.setdefault("matches", {})
    data.setdefault("player_match_index", {})
    data.setdefault("last_update_utc", None)

    # Defensive type guarantees (protect against old/broken JSON)
    if not isinstance(data["players"], dict):
        data["players"] = {}
    if not isinstance(data["matches"], dict):
        data["matches"] = {}
    if not isinstance(data["player_match_index"], dict):
        data["player_match_index"] = {}

    return data


def resolve_player_key(data: dict, riot_id: str) -> str | None:
    """
    Finds the stored Riot ID key using exact or case-insensitive matching.
    This lets commands work even when users type a different Riot ID casing.
    """
    if not riot_id:
        return None

    players = data.get("players", {})
    if riot_id in players:
        return riot_id

    try:
        target = normalize_riot_id(riot_id)
    except ValueError:
        return None

    for key, player in players.items():
        if key.casefold() == target:
            return key

        game_name = player.get("game_name")
        tag_line = player.get("tag_line")
        if game_name and tag_line:
            if canonical_riot_id(game_name, tag_line).casefold() == target:
                return key

    return None


def get_player_matches(data: dict, riot_id: str) -> list[dict[str, Any]]:
    match_ids = data.get("player_match_index", {}).get(riot_id, [])
    matches = data.get("matches", {})
    return [matches[mid] for mid in match_ids if mid in matches]


def cache_health(data: dict) -> dict[str, Any]:
    players = data.get("players", {})
    matches = data.get("matches", {})
    index = data.get("player_match_index", {})

    indexed_refs = 0
    missing_details = 0
    duplicate_refs = 0
    orphaned_players = 0

    for riot_id, player in players.items():
        if not player.get("puuid"):
            orphaned_players += 1

        ids = index.get(riot_id, [])
        indexed_refs += len(ids)
        duplicate_refs += len(ids) - len(set(ids))
        missing_details += sum(1 for mid in ids if mid not in matches)

    queue_counts: dict[int | str, int] = {}
    for match in matches.values():
        qid = match.get("info", {}).get("queueId", "unknown")
        queue_counts[qid] = queue_counts.get(qid, 0) + 1

    return {
        "players": len(players),
        "matches": len(matches),
        "indexed_refs": indexed_refs,
        "missing_details": missing_details,
        "duplicate_refs": duplicate_refs,
        "orphaned_players": orphaned_players,
        "last_update_utc": data.get("last_update_utc"),
        "queue_counts": queue_counts,
    }


def repair_cache_indexes(data: dict, prune_missing_details: bool = False) -> dict[str, int]:
    """
    Deduplicates player match indexes and removes indexes for deleted players.
    If prune_missing_details is True, index refs without a stored match payload are removed.
    """
    players = data.get("players", {})
    matches = data.get("matches", {})
    index = data.setdefault("player_match_index", {})

    removed_player_indexes = 0
    removed_duplicates = 0
    removed_missing_details = 0

    for riot_id in list(index.keys()):
        if riot_id not in players:
            index.pop(riot_id, None)
            removed_player_indexes += 1

    for riot_id in players:
        seen = set()
        repaired = []
        for match_id in index.get(riot_id, []):
            if match_id in seen:
                removed_duplicates += 1
                continue
            if prune_missing_details and match_id not in matches:
                removed_missing_details += 1
                continue
            seen.add(match_id)
            repaired.append(match_id)
        index[riot_id] = repaired

    return {
        "removed_player_indexes": removed_player_indexes,
        "removed_duplicates": removed_duplicates,
        "removed_missing_details": removed_missing_details,
    }

# -------------------------------------------------
# Save persistent data
# -------------------------------------------------

def save_data(data: dict) -> None:
    """
    Atomically writes league.json.
    """
    tmp_path = DATA_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        if PRETTY_JSON:
            json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, DATA_FILE)

# -------------------------------------------------
# Player management
# -------------------------------------------------

def upsert_player(
    data: dict,
    riot_id: str,
    game_name: str,
    tag_line: str,
    puuid: str,
    encrypted_summoner_id: str | None = None,
) -> None:
    """
    Inserts or updates a player entry.
    PUUID is the canonical identifier.
    This function is deterministic and idempotent.
    """
    if not puuid:
        raise ValueError("puuid is required to add player")

    riot_key = canonical_riot_id(game_name, tag_line)
    existing_key = None
    for key, player in data["players"].items():
        if key.casefold() == riot_key.casefold() or player.get("puuid") == puuid:
            existing_key = key
            break

    existing = data["players"].get(existing_key or riot_id, {})

    if existing_key and existing_key != riot_key:
        data["players"].pop(existing_key, None)
        old_index = data["player_match_index"].pop(existing_key, [])
        data["player_match_index"].setdefault(riot_key, old_index)

    updated = dict(existing)
    updated.update(
        {
            "game_name": game_name,
            "tag_line": tag_line,
            "puuid": puuid,
            "encrypted_summoner_id": (
                encrypted_summoner_id
                if encrypted_summoner_id is not None
                else existing.get("encrypted_summoner_id")
            ),
            "added_at": existing.get("added_at") or now_utc_iso(),
        }
    )

    data["players"][riot_key] = updated

    # Ensure match index always exists
    data["player_match_index"].setdefault(riot_key, [])
