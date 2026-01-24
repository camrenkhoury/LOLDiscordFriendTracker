from riot import get_active_game
from collections import defaultdict

QUEUE_NAMES = {
    420: "Solo/Duo",
    440: "Flex",
    450: "ARAM",
}

def get_live_games(data):
    """
    Returns grouped live games involving tracked players.
    """
    games = {}

    for riot_id, p in data.get("players", {}).items():
        enc_id = p.get("encrypted_summoner_id")
        if not enc_id:
            continue

        try:
            g = get_active_game(enc_id)
        except Exception:
            continue  # not in game or spectator unavailable

        gid = g["gameId"]
        games.setdefault(gid, {
            "queueId": g.get("gameQueueConfigId"),
            "teams": defaultdict(list),
            "length": g.get("gameLength", 0),
        })

        for part in g.get("participants", []):
            if part.get("summonerId") == enc_id:
                team = part.get("teamId")
                games[gid]["teams"][team].append(riot_id)

    return list(games.values())


def format_live_games(games):
    lines = []
    for g in games:
        qname = QUEUE_NAMES.get(g["queueId"], f"Queue {g['queueId']}")
        mins = g["length"] // 60
        secs = g["length"] % 60

        lines.append(f"{qname} — {mins}:{secs:02d}")
        for team, players in g["teams"].items():
            if players:
                lines.append(f"  Team {team}: {', '.join(players)}")
        lines.append("")

    return lines
