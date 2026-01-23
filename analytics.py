from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from itertools import combinations

LOCAL_TZ = ZoneInfo("America/New_York")

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

def compute_top_duos(data, queue_id=420, min_games=5):
    duo = defaultdict(lambda: {"wins":0, "games":0})

    for m in data["matches"].values():
        info = m["info"]
        if info["queueId"] != queue_id:
            continue

        teams = defaultdict(list)
        for p in info["participants"]:
            for rid, pl in data["players"].items():
                if p["puuid"] == pl["puuid"]:
                    teams[p["teamId"]].append(rid)

        for team, members in teams.items():
            for a, b in combinations(sorted(members), 2):
                key = f"{a},{b}"
                duo[key]["games"] += 1
                if info["teams"][0 if team == 100 else 1]["win"]:
                    duo[key]["wins"] += 1

    out = []
    for k, v in duo.items():
        if v["games"] >= min_games:
            wr = v["wins"] / v["games"] * 100
            out.append((k, v["wins"], v["games"], wr))

    return sorted(out, key=lambda x: x[3], reverse=True)[:10]
