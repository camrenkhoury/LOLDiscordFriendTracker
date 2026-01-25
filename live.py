from riot import get_active_game

QUEUE_NAMES = {
    400: "Draft Pick",
    420: "Solo/Duo",
    440: "Flex",
    450: "ARAM",
    2400: "ARAM",
    1700: "CLASH"
}

def get_live_games(data):
    live = []
    for riot_id, p in data.get("players", {}).items():
        puuid = p.get("puuid")
        if not puuid:
            print(f"[LIVE] Missing puuid for {riot_id}")
            continue

        try:
            game = get_active_game(puuid)
            if game is None:
                print(f"[LIVE] {riot_id} not in game")
                continue
            print(f"[LIVE] {riot_id} IS IN GAME")
            live.append((riot_id, game))
        except Exception as e:
            print(f"[LIVE] Error checking {riot_id}: {e}")

    return live

def format_live_games(games):
    lines = []
    for riot_id, g in games:
        # spectator-v5 uses gameQueueConfigId + gameLength
        qid = int(g.get("gameQueueConfigId", 0) or 0)
        qname = QUEUE_NAMES.get(qid, f"Queue {qid}")

        length = int(g.get("gameLength", 0) or 0)
        mins = length // 60
        secs = length % 60

        lines.append(f"{riot_id} — {qname} — {mins}:{secs:02d}")
    return lines
