from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Tuple

from riot import get_active_game  # now expects PUUID and uses spectator-v5

QUEUE_NAMES = {
    420: "Solo/Duo",
    440: "Flex",
    450: "ARAM",
}

# Add any special ARAM/event queues you want to bucket as ARAM:
ARAM_LIKE_QUEUES = {450}  # you can extend (e.g., 1300, etc.) once you confirm queueIds


def _queue_name(queue_id: int) -> str:
    if queue_id in ARAM_LIKE_QUEUES:
        return "ARAM"
    return QUEUE_NAMES.get(queue_id, f"Queue {queue_id}")


def get_live_games(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Returns a list of "live game group" dicts, grouped by gameId:
      [
        {
          "gameId": 123,
          "queueId": 420,
          "queueName": "Solo/Duo",
          "gameLengthSec": 615,
          "teams": {100: ["A#TAG", "B#TAG"], 200: ["C#TAG"]},
        },
        ...
      ]
    """
    players = (data or {}).get("players", {})
    if not players:
        return []

    grouped: Dict[int, Dict[str, Any]] = {}

    for riot_id, p in players.items():
        puuid = p.get("puuid")
        if not puuid:
            # If you ever see this, your league.json is missing puuid for that player
            print(f"[LIVE] Missing puuid for {riot_id}")
            continue

        try:
            game = get_active_game(puuid)  # returns None if not in game; dict if in game
        except Exception as e:
            print(f"[LIVE] Error checking {riot_id}: {e}")
            continue

        if not game:
            continue

        game_id = int(game.get("gameId", 0) or 0)
        if game_id <= 0:
            continue

        queue_id = int(game.get("gameQueueConfigId", 0) or 0)
        length_sec = int(game.get("gameLength", 0) or 0)

        # Determine team for this puuid from participant list
        team_id = None
        for part in game.get("participants", []):
            if part.get("puuid") == puuid:
                team_id = int(part.get("teamId", 0) or 0)
                break

        if game_id not in grouped:
            grouped[game_id] = {
                "gameId": game_id,
                "queueId": queue_id,
                "queueName": _queue_name(queue_id),
                "gameLengthSec": length_sec,
                "teams": {100: [], 200: []},
            }

        if team_id in (100, 200):
            grouped[game_id]["teams"][team_id].append(riot_id)
        else:
            # Fallback bucket if teamId is missing/unexpected
            grouped[game_id]["teams"].setdefault(0, []).append(riot_id)

    # Sort games by longest-running first (optional, looks nicer)
    out = list(grouped.values())
    out.sort(key=lambda g: g.get("gameLengthSec", 0), reverse=True)
    return out


def format_live_games(live_games: List[Dict[str, Any]]) -> List[str]:
    """
    Input must be the output of get_live_games().
    Returns a list of lines suitable for a Discord code block.
    """
    lines: List[str] = []

    for g in live_games:
        qname = g.get("queueName") or _queue_name(int(g.get("queueId", 0) or 0))
        length = int(g.get("gameLengthSec", 0) or 0)
        mins = length // 60
        secs = length % 60

        lines.append(f"{qname} — {mins}:{secs:02d}")

        teams = g.get("teams", {})
        for team_id in (100, 200, 0):
            players = teams.get(team_id, [])
            if not players:
                continue
            label = "Team 100" if team_id == 100 else "Team 200" if team_id == 200 else "Team ?"
            lines.append(f"  {label}: {', '.join(players)}")

        lines.append("")  # blank line between games

    # Remove trailing blank if present
    if lines and lines[-1] == "":
        lines.pop()

    return lines
