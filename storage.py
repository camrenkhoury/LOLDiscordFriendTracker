import json
import os
from datetime import datetime, timezone

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

# -------------------------------------------------
# Helpers
# -------------------------------------------------

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

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

# -------------------------------------------------
# Save persistent data
# -------------------------------------------------

def save_data(data: dict) -> None:
    """
    Atomically writes league.json.
    """
    tmp_path = DATA_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
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

    existing = data["players"].get(riot_id, {})

    data["players"][riot_id] = {
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

    # Ensure match index always exists
    data["player_match_index"].setdefault(riot_id, [])
