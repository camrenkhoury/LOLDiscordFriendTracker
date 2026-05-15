from riot import get_active_game

QUEUE_NAMES = {
    400: "Draft Pick",
    420: "Solo/Duo",
    440: "Flex",
    450: "ARAM",
    1700: "Arena",
    2400: "ARAM",
}


def get_live_games(data):
    live_by_game = {}

    for riot_id, player in data.get("players", {}).items():
        puuid = player.get("puuid")
        if not puuid:
            print(f"[LIVE] Missing puuid for {riot_id}")
            continue

        try:
            game = get_active_game(puuid)
            if game is None:
                print(f"[LIVE] {riot_id} not in game")
                continue

            print(f"[LIVE] {riot_id} IS IN GAME")
            game_id = game.get("gameId") or f"{riot_id}:{game.get('gameStartTime', '')}"
            entry = live_by_game.setdefault(game_id, {"game": game, "players": []})
            entry["players"].append(riot_id)
        except Exception as e:
            print(f"[LIVE] Error checking {riot_id}: {e}")

    return list(live_by_game.values())


def format_live_games(games):
    lines = []
    for entry in games:
        if isinstance(entry, tuple):  # Backward compatibility for older callers/tests.
            players = [entry[0]]
            game = entry[1]
        else:
            players = sorted(entry.get("players", []))
            game = entry.get("game", {})

        qid = int(game.get("gameQueueConfigId", 0) or 0)
        qname = QUEUE_NAMES.get(qid, f"Queue {qid}")

        length = int(game.get("gameLength", 0) or 0)
        mins = length // 60
        secs = length % 60

        names = ", ".join(players) if players else "Tracked player"
        lines.append(f"{names} - {qname} - {mins}:{secs:02d}")

    return lines
