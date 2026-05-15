from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from itertools import combinations

LOCAL_TZ = ZoneInfo("America/New_York")
ROLE_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

def iter_matches(data, queue_id=None, start=None, end=None):
    for m in data.get("matches", {}).values():
        info = m.get("info", {})
        if queue_id is not None and info.get("queueId") != queue_id:
            continue

        ts = info.get("gameStartTimestamp")
        if not ts:
            continue

        t = datetime.fromtimestamp(ts / 1000, tz=LOCAL_TZ)
        if start and t < start:
            continue
        if end and t >= end:
            continue

        yield m

def _match_start_local(match):
    info = match.get("info", {})
    ts = info.get("gameStartTimestamp") or info.get("gameCreation")
    if not ts:
        return None
    return datetime.fromtimestamp(ts / 1000, tz=LOCAL_TZ)


def tracked_puuid_map(data):
    return {
        player.get("puuid"): riot_id
        for riot_id, player in data.get("players", {}).items()
        if player.get("puuid")
    }


def _duration_minutes(match):
    seconds = int(match.get("info", {}).get("gameDuration", 0) or 0)
    return max(1.0, seconds / 60)


def _participant(match, puuid):
    return next(
        (
            p
            for p in match.get("info", {}).get("participants", [])
            if p.get("puuid") == puuid
        ),
        None,
    )


def _team_participants(match, participant):
    team_id = participant.get("teamId")
    return [
        p
        for p in match.get("info", {}).get("participants", [])
        if p.get("teamId") == team_id
    ]


def participant_metrics(match, participant):
    info = match.get("info", {})
    duration = _duration_minutes(match)
    team = _team_participants(match, participant)

    kills = int(participant.get("kills", 0) or 0)
    deaths = int(participant.get("deaths", 0) or 0)
    assists = int(participant.get("assists", 0) or 0)
    cs = int(participant.get("totalMinionsKilled", 0) or 0) + int(
        participant.get("neutralMinionsKilled", 0) or 0
    )
    damage = int(participant.get("totalDamageDealtToChampions", 0) or 0)
    vision = int(participant.get("visionScore", 0) or 0)
    team_kills = sum(int(p.get("kills", 0) or 0) for p in team)
    team_damage = sum(int(p.get("totalDamageDealtToChampions", 0) or 0) for p in team)

    return {
        "match_id": match.get("metadata", {}).get("matchId"),
        "queue_id": info.get("queueId"),
        "start": _match_start_local(match),
        "duration_min": duration,
        "win": bool(participant.get("win", False)),
        "champion": participant.get("championName") or "Unknown",
        "role": participant.get("teamPosition") or participant.get("individualPosition") or "Unknown",
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "kda": (kills + assists) / max(1, deaths),
        "cs": cs,
        "cspm": cs / duration,
        "damage": damage,
        "damage_per_min": damage / duration,
        "damage_share": (damage / team_damage * 100) if team_damage else None,
        "kill_participation": ((kills + assists) / team_kills * 100) if team_kills else None,
        "vision": vision,
        "vision_per_game": vision,
    }


def performance_score(metrics):
    return (
        metrics["kills"] * 2.0
        + metrics["assists"] * 1.2
        - metrics["deaths"] * 1.6
        + metrics["kda"] * 2.0
        + metrics["cspm"] * 0.7
        + (metrics["damage_share"] or 0) * 0.35
        + (metrics["kill_participation"] or 0) * 0.18
        + metrics["vision"] * 0.08
    )


def _match_in_window(match, start=None, end=None):
    if not start and not end:
        return True
    t = _match_start_local(match)
    if t is None:
        return False
    if start and t < start:
        return False
    if end and t >= end:
        return False
    return True


def sorted_player_matches(data, riot_id, queue_ids=None, champion=None, start=None, end=None):
    player = data.get("players", {}).get(riot_id, {})
    puuid = player.get("puuid")
    if not puuid:
        return []

    queue_ids = set(queue_ids) if queue_ids is not None else None
    champion_key = champion.casefold() if champion else None
    matches = data.get("matches", {})
    out = []

    for match_id in data.get("player_match_index", {}).get(riot_id, []):
        match = matches.get(match_id)
        if not match or not _match_in_window(match, start, end):
            continue

        info = match.get("info", {})
        if queue_ids is not None and info.get("queueId") not in queue_ids:
            continue

        participant = _participant(match, puuid)
        if not participant:
            continue
        if champion_key and (participant.get("championName") or "").casefold() != champion_key:
            continue

        out.append((match, participant, participant_metrics(match, participant)))

    out.sort(
        key=lambda item: item[0].get("info", {}).get("gameStartTimestamp")
        or item[0].get("info", {}).get("gameCreation")
        or 0,
        reverse=True,
    )
    return out


def tracked_participants_for_match(data, match):
    puuid_to_riot = tracked_puuid_map(data)
    out = []
    for participant in match.get("info", {}).get("participants", []):
        riot_id = puuid_to_riot.get(participant.get("puuid"))
        if riot_id:
            out.append((riot_id, participant, participant_metrics(match, participant)))
    return out


def build_match_card(data, riot_id, count=1):
    matches = sorted_player_matches(data, riot_id)
    if not matches:
        return None

    index = max(0, min(len(matches) - 1, count - 1))
    match, participant, metrics = matches[index]
    tracked = tracked_participants_for_match(data, match)
    same_team = [
        (rid, p, m)
        for rid, p, m in tracked
        if p.get("teamId") == participant.get("teamId")
    ]
    best = max(same_team, key=lambda item: performance_score(item[2]), default=None)
    award = None
    if best and best[0] == riot_id and len(same_team) >= 2:
        award = "MVP" if metrics["win"] else "ACE"

    teammates = [
        rid
        for rid, p, _ in same_team
        if rid != riot_id
    ]

    return {
        "match": match,
        "participant": participant,
        "metrics": metrics,
        "teammates": teammates,
        "tracked_count": len(tracked),
        "award": award,
    }


def summarize_player(data, riot_id, queue_ids=None, start=None, end=None, limit=None):
    rows = sorted_player_matches(data, riot_id, queue_ids=queue_ids, start=start, end=end)
    if limit:
        rows = rows[:limit]
    if not rows:
        return None

    totals = defaultdict(float)
    champs = defaultdict(lambda: {"games": 0, "wins": 0})
    queues = defaultdict(lambda: {"games": 0, "wins": 0})
    wins = 0

    for _, _, metrics in rows:
        wins += 1 if metrics["win"] else 0
        for key in ("kills", "deaths", "assists", "cs", "damage", "vision", "duration_min"):
            totals[key] += metrics[key]
        champs[metrics["champion"]]["games"] += 1
        champs[metrics["champion"]]["wins"] += 1 if metrics["win"] else 0
        queues[metrics["queue_id"]]["games"] += 1
        queues[metrics["queue_id"]]["wins"] += 1 if metrics["win"] else 0

    games = len(rows)
    top_champs = sorted(champs.items(), key=lambda kv: (kv[1]["games"], kv[1]["wins"]), reverse=True)[:3]
    best_queue = max(
        queues.items(),
        key=lambda kv: ((kv[1]["wins"] / kv[1]["games"]) if kv[1]["games"] else 0, kv[1]["games"]),
    )[0]

    recent = rows[:10]
    recent_wins = sum(1 for _, _, m in recent if m["win"])
    return {
        "games": games,
        "wins": wins,
        "losses": games - wins,
        "wr": wins / games * 100 if games else 0.0,
        "kda": (totals["kills"] + totals["assists"]) / max(1, totals["deaths"]),
        "cspm": totals["cs"] / max(1, totals["duration_min"]),
        "damage_per_min": totals["damage"] / max(1, totals["duration_min"]),
        "vision_per_game": totals["vision"] / games,
        "top_champs": [
            {
                "champion": champ,
                "games": rec["games"],
                "wr": rec["wins"] / rec["games"] * 100 if rec["games"] else 0.0,
            }
            for champ, rec in top_champs
        ],
        "best_queue": best_queue,
        "recent_form": f"{recent_wins}-{len(recent) - recent_wins}",
        "rows": rows,
    }


def compare_players(data, riot_a, riot_b, queue_ids=None):
    a = summarize_player(data, riot_a, queue_ids=queue_ids)
    b = summarize_player(data, riot_b, queue_ids=queue_ids)
    if not a or not b:
        return None

    puuid_a = data.get("players", {}).get(riot_a, {}).get("puuid")
    puuid_b = data.get("players", {}).get(riot_b, {}).get("puuid")
    together_games = together_wins_a = same_team_games = 0
    for match in data.get("matches", {}).values():
        pa = _participant(match, puuid_a)
        pb = _participant(match, puuid_b)
        if not pa or not pb:
            continue
        if queue_ids is not None and match.get("info", {}).get("queueId") not in queue_ids:
            continue
        together_games += 1
        if pa.get("teamId") == pb.get("teamId"):
            same_team_games += 1
        if pa.get("win"):
            together_wins_a += 1

    return {
        "a": a,
        "b": b,
        "together_games": together_games,
        "same_team_games": same_team_games,
        "a_h2h_wins": together_wins_a,
    }


def champion_deep_dive(data, riot_id, champion, queue_ids=None):
    rows = sorted_player_matches(data, riot_id, queue_ids=queue_ids, champion=champion)
    if not rows:
        return None

    summary = summarize_player(data, riot_id, queue_ids=queue_ids)
    champ_summary = summarize_player(data, riot_id, queue_ids=queue_ids, limit=None)
    metrics_rows = [(match, part, metrics) for match, part, metrics in rows]
    games = len(metrics_rows)
    wins = sum(1 for _, _, m in metrics_rows if m["win"])
    duration = sum(m["duration_min"] for _, _, m in metrics_rows)
    kills = sum(m["kills"] for _, _, m in metrics_rows)
    deaths = sum(m["deaths"] for _, _, m in metrics_rows)
    assists = sum(m["assists"] for _, _, m in metrics_rows)
    damage = sum(m["damage"] for _, _, m in metrics_rows)
    cs = sum(m["cs"] for _, _, m in metrics_rows)

    best = max(metrics_rows, key=lambda item: performance_score(item[2]))
    worst = min(metrics_rows, key=lambda item: performance_score(item[2]))
    recent = metrics_rows[:5]
    recent_wins = sum(1 for _, _, m in recent if m["win"])

    teammate_counts = defaultdict(int)
    puuid_to_riot = tracked_puuid_map(data)
    player_puuid = data.get("players", {}).get(riot_id, {}).get("puuid")
    for match, participant, _ in metrics_rows:
        for other in match.get("info", {}).get("participants", []):
            if other.get("teamId") != participant.get("teamId"):
                continue
            if other.get("puuid") == player_puuid:
                continue
            teammate = puuid_to_riot.get(other.get("puuid"))
            if teammate:
                teammate_counts[teammate] += 1

    teammates = sorted(teammate_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]

    return {
        "champion": rows[0][2]["champion"],
        "games": games,
        "wins": wins,
        "losses": games - wins,
        "wr": wins / games * 100 if games else 0.0,
        "kda": (kills + assists) / max(1, deaths),
        "cspm": cs / max(1, duration),
        "damage_per_min": damage / max(1, duration),
        "best": best[2],
        "worst": worst[2],
        "recent_form": f"{recent_wins}-{len(recent) - recent_wins}",
        "teammates": teammates,
    }


def group_awards(data, start=None, end=None):
    tracked = tracked_puuid_map(data)
    player_totals = defaultdict(lambda: defaultdict(float))
    champion_totals = defaultdict(lambda: defaultdict(float))
    duo = defaultdict(lambda: {"wins": 0, "games": 0})
    games_seen = set()

    for match in data.get("matches", {}).values():
        if not _match_in_window(match, start, end):
            continue

        match_id = match.get("metadata", {}).get("matchId")
        tracked_rows = tracked_participants_for_match(data, match)
        if not tracked_rows:
            continue
        games_seen.add(match_id or id(match))

        teams = defaultdict(list)
        for riot_id, participant, metrics in tracked_rows:
            rec = player_totals[riot_id]
            rec["games"] += 1
            rec["wins"] += 1 if metrics["win"] else 0
            rec["kills"] += metrics["kills"]
            rec["deaths"] += metrics["deaths"]
            rec["assists"] += metrics["assists"]
            rec["cs"] += metrics["cs"]
            rec["damage"] += metrics["damage"]
            rec["vision"] += metrics["vision"]
            rec["duration"] += metrics["duration_min"]
            rec["score"] += performance_score(metrics)
            rec["aram_games"] += 1 if metrics["queue_id"] in {450, 2400} else 0

            champ = champion_totals[metrics["champion"]]
            champ["games"] += 1
            champ["wins"] += 1 if metrics["win"] else 0
            champ["kills"] += metrics["kills"]
            champ["deaths"] += metrics["deaths"]
            champ["assists"] += metrics["assists"]

            teams[participant.get("teamId")].append((riot_id, metrics))

        for members in teams.values():
            names = sorted({riot_id for riot_id, _ in members})
            for a, b in combinations(names, 2):
                key = tuple(sorted((a, b)))
                duo[key]["games"] += 1
                sample_metrics = next(m for rid, m in members if rid == a)
                if sample_metrics["win"]:
                    duo[key]["wins"] += 1

    def player_best(label, key_fn):
        if not player_totals:
            return None
        name, rec = max(player_totals.items(), key=lambda kv: key_fn(kv[1]))
        return {"label": label, "player": name, "record": rec}

    awards = []
    awards.append(player_best("Carry King", lambda r: r["score"] / max(1, r["games"])))
    awards.append(player_best("CS Goblin", lambda r: r["cs"] / max(1, r["duration"])))
    awards.append(player_best("Vision Warden", lambda r: r["vision"] / max(1, r["games"])))
    awards.append(player_best("Damage Dealer", lambda r: r["damage"] / max(1, r["duration"])))
    awards.append(player_best("Survivalist", lambda r: -r["deaths"] / max(1, r["games"])))
    awards.append(player_best("Glass Cannon", lambda r: (r["damage"] / max(1, r["duration"])) - (r["deaths"] * 10 / max(1, r["games"]))))
    awards.append(player_best("ARAM Menace", lambda r: r["aram_games"]))
    awards.append(player_best("Tilt Resistant", lambda r: r["games"]))

    best_duo = None
    qualified_duos = [(pair, rec) for pair, rec in duo.items() if rec["games"] >= 2]
    if qualified_duos:
        pair, rec = max(
            qualified_duos,
            key=lambda item: (item[1]["wins"] / max(1, item[1]["games"]), item[1]["games"]),
        )
        best_duo = {
            "label": "Duo Diff",
            "player": " + ".join(pair),
            "record": rec,
        }

    champion_rows = []
    for champ, rec in champion_totals.items():
        games = rec["games"]
        if games:
            champion_rows.append(
                {
                    "champion": champ,
                    "games": games,
                    "wins": rec["wins"],
                    "wr": rec["wins"] / games * 100,
                    "kda": (rec["kills"] + rec["assists"]) / max(1, rec["deaths"]),
                }
            )
    champion_rows.sort(key=lambda r: (r["games"], r["wr"]), reverse=True)
    high_wr_champs = [r for r in champion_rows if r["games"] >= 3]
    high_wr_champs.sort(key=lambda r: (r["wr"], r["games"]), reverse=True)

    return {
        "games": len(games_seen),
        "players": player_totals,
        "awards": [a for a in awards if a],
        "best_duo": best_duo,
        "most_played_champ": champion_rows[0] if champion_rows else None,
        "highest_wr_champ": high_wr_champs[0] if high_wr_champs else None,
    }


def compute_top_duos(data, queue_id=420, min_games=5, limit=10, start=None, end=None):
    """
    Returns top same-team tracked duos for a queue.

    Result items are dictionaries so Discord commands and future renderers can
    address fields by name instead of relying on tuple positions.
    """
    puuid_to_riot = {
        player.get("puuid"): riot_id
        for riot_id, player in data.get("players", {}).items()
        if player.get("puuid")
    }
    duo = defaultdict(lambda: {"wins": 0, "games": 0})

    for match in data.get("matches", {}).values():
        info = match.get("info", {})
        if info.get("queueId") != queue_id:
            continue

        if start or end:
            t = _match_start_local(match)
            if t is None:
                continue
            if start and t < start:
                continue
            if end and t >= end:
                continue

        teams = defaultdict(list)
        team_win = {}
        for participant in info.get("participants", []):
            riot_id = puuid_to_riot.get(participant.get("puuid"))
            if not riot_id:
                continue

            team_id = participant.get("teamId")
            if team_id is None:
                continue

            teams[team_id].append(riot_id)
            team_win[team_id] = bool(participant.get("win", False))

        for team_id, members in teams.items():
            unique_members = sorted(set(members))
            for a, b in combinations(unique_members, 2):
                key = tuple(sorted((a, b)))
                duo[key]["games"] += 1
                if team_win.get(team_id, False):
                    duo[key]["wins"] += 1

    out = []
    for players, stats in duo.items():
        games = stats["games"]
        if games < min_games:
            continue

        wins = stats["wins"]
        losses = games - wins
        wr = wins / games * 100 if games else 0.0
        out.append(
            {
                "duo": " + ".join(players),
                "players": players,
                "wins": wins,
                "losses": losses,
                "games": games,
                "wr": wr,
            }
        )

    out.sort(key=lambda r: (r["wr"], r["games"], r["wins"]), reverse=True)
    return out[:limit]


def compute_top_champions(data, queue_ids=None, min_games=3, limit=10, start=None, end=None):
    """
    Aggregates champion performance for tracked players from cached matches.
    Counts each tracked player participant once per match.
    """
    queue_ids = set(queue_ids) if queue_ids is not None else None
    tracked_puuids = {
        player.get("puuid")
        for player in data.get("players", {}).values()
        if player.get("puuid")
    }
    stats = defaultdict(
        lambda: {
            "games": 0,
            "wins": 0,
            "losses": 0,
            "kills": 0,
            "deaths": 0,
            "assists": 0,
        }
    )

    for match in data.get("matches", {}).values():
        info = match.get("info", {})
        qid = info.get("queueId")
        if queue_ids is not None and qid not in queue_ids:
            continue

        if start or end:
            t = _match_start_local(match)
            if t is None:
                continue
            if start and t < start:
                continue
            if end and t >= end:
                continue

        for participant in info.get("participants", []):
            if participant.get("puuid") not in tracked_puuids:
                continue

            champion = participant.get("championName") or "Unknown"
            rec = stats[champion]
            rec["games"] += 1
            if participant.get("win"):
                rec["wins"] += 1
            else:
                rec["losses"] += 1
            rec["kills"] += int(participant.get("kills", 0) or 0)
            rec["deaths"] += int(participant.get("deaths", 0) or 0)
            rec["assists"] += int(participant.get("assists", 0) or 0)

    out = []
    for champion, rec in stats.items():
        games = rec["games"]
        if games < min_games:
            continue

        wins = rec["wins"]
        wr = wins / games * 100 if games else 0.0
        kda = (rec["kills"] + rec["assists"]) / max(1, rec["deaths"])
        out.append(
            {
                "champion": champion,
                "games": games,
                "wins": wins,
                "losses": rec["losses"],
                "wr": wr,
                "kills": rec["kills"],
                "deaths": rec["deaths"],
                "assists": rec["assists"],
                "kda": kda,
            }
        )

    out.sort(key=lambda r: (r["wr"], r["games"], r["kda"]), reverse=True)
    return out[:limit]
