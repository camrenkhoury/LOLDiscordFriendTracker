import json
import os
from datetime import datetime, timezone

DATA_FILE = "league.json"

def load_data():
    if not os.path.exists(DATA_FILE):
        return {
            "season": 1,
            "players": {},
            "matches": {},
            "player_match_index": {},
            "last_update_utc": None
        }
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()

def upsert_player(data, riot_id, game_name, tag_line, puuid, encrypted_summoner_id=None):
    entry = data["players"].get(riot_id, {})
    data["players"][riot_id] = {
        "game_name": game_name,
        "tag_line": tag_line,
        "puuid": puuid,
        "encrypted_summoner_id": encrypted_summoner_id or entry.get("encrypted_summoner_id"),
        "added_at": entry.get("added_at") or now_utc_iso()
    }
    data["player_match_index"].setdefault(riot_id, [])


