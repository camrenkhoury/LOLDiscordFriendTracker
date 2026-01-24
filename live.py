from riot import get_active_game
from collections import defaultdict

QUEUE_NAMES = {
    420: "Solo/Duo",
    440: "Flex",
    450: "ARAM",
}

def get_live_games(data):
    live = []
    for riot_id, p in data["players"].items():
        enc = p.get("encrypted_id")
        if not enc:
            print(f"[LIVE] Missing encrypted_id for {riot_id}")
            continue

        try:
            game = get_active_game(enc)
            print(f"[LIVE] {riot_id} IS IN GAME")
            live.append((riot_id, game))
        except Exception as e:
            print(f"[LIVE] {riot_id} not in game: {e}")

    return live



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
